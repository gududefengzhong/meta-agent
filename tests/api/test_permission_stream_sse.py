"""API tests for ``GET /v1/tasks/{id}/permissions/stream`` (Phase δ-1).

Drives the endpoint with an :class:`InMemoryPermissionGate` + a
background coroutine that simulates the worker calling
``gate.request`` mid-task. Asserts the wire shape (SSE
``event: permission.prompt`` frames) + 404 / 503 / terminal-state
paths.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from meta_agent.api.app import create_app
from meta_agent.api.deps import (
    get_db_pool,
    get_permission_gate,
    get_request_ctx,
    get_task_repo,
    get_token_validator,
)
from meta_agent.api.routers import tasks as tasks_router
from meta_agent.core.domain.permission import PermissionDecision, PermissionPrompt
from meta_agent.core.domain.task import (
    BudgetPolicy,
    PermissionMode,
    Task,
    TaskState,
    TaskType,
)
from meta_agent.core.ports.auth import Principal, TokenValidator
from meta_agent.core.ports.permission_gate import (
    PermissionGate,
    PermissionGateError,
)
from meta_agent.infra.permission.in_memory import InMemoryPermissionGate
from meta_agent.infra.security.context import RequestContext
from tests.worker._fakes import FakeTaskRepo

_TENANT = "tenant-test"
_PRINCIPAL = "user-test"
_BEARER = {"Authorization": "Bearer tok-test"}


class _StubTokenValidator(TokenValidator):
    async def validate(self, token: str) -> Principal | None:
        if token == "tok-test":
            return Principal(tenant_id=_TENANT, principal_id=_PRINCIPAL)
        return None


class _FakeDbPool:
    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[object]:
        yield object()


def _fixed_ctx() -> RequestContext:
    return RequestContext(
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id="trace-fixed",
        request_id="req-fixed",
    )


def _task(state: TaskState = TaskState.RUNNING) -> Task:
    return Task(
        task_id="task-1",
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id="trace-fixed",
        idempotency_key="idem-1",
        task_type=TaskType.SYSTEM_SHELL_AGENT,
        graph_id=None,
        state=state,
        permission_mode=PermissionMode.APPROVE_EACH_TOOL,
        budget_policy=BudgetPolicy.NONE,
        input_payload={},
        created_at=datetime(2026, 6, 23, tzinfo=UTC),
        updated_at=datetime(2026, 6, 23, tzinfo=UTC),
    )


def _make_app(*, task_repo: FakeTaskRepo, gate: PermissionGate) -> Any:
    app = create_app(lifespan=None)
    app.dependency_overrides[get_task_repo] = lambda: task_repo
    app.dependency_overrides[get_permission_gate] = lambda: gate
    app.dependency_overrides[get_db_pool] = _FakeDbPool
    app.dependency_overrides[get_request_ctx] = _fixed_ctx
    app.dependency_overrides[get_token_validator] = _StubTokenValidator
    return app


@pytest.fixture(autouse=True)
def _fast_sse_intervals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks_router, "_LLM_STREAM_CHUNK_WAIT_S", 0.02)
    monkeypatch.setattr(tasks_router, "_LLM_STREAM_TERMINAL_GRACE_S", 0.05)
    monkeypatch.setattr(tasks_router, "_SSE_HEARTBEAT_INTERVAL_S", 100.0)
    monkeypatch.setattr(tasks_router, "_SSE_MAX_DURATION_S", 1.0)


def _prompt(prompt_id: str = "prm-api-1") -> PermissionPrompt:
    return PermissionPrompt(
        prompt_id=prompt_id,
        tenant_id=_TENANT,
        task_id="task-1",
        tool_name="shell",
        summary="run shell",
        payload={"cmd": "ls"},
        created_at=datetime(2026, 6, 23, tzinfo=UTC),
    )


async def test_missing_task_returns_404() -> None:
    gate = InMemoryPermissionGate()
    app = _make_app(task_repo=FakeTaskRepo(), gate=gate)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/tasks/unknown/permissions/stream", headers=_BEARER)
    assert response.status_code == 404


async def test_prompts_are_relayed_then_terminal_state_closes_stream() -> None:
    gate = InMemoryPermissionGate()
    task_repo = FakeTaskRepo()
    await task_repo.upsert(_task(state=TaskState.RUNNING))

    async def producer() -> None:
        # Wait briefly so the API subscriber is registered.
        await asyncio.sleep(0.05)

        async def auto_decide() -> None:
            # Resolve the worker's request after a beat so its
            # request() doesn't time out and pollute the test.
            await asyncio.sleep(0.05)
            await gate.deliver(
                PermissionDecision(
                    prompt_id="prm-api-1",
                    allow=True,
                    reason=None,
                    decided_at=datetime(2026, 6, 23, tzinfo=UTC),
                )
            )

        decider = asyncio.create_task(auto_decide())
        try:
            await gate.request(_prompt(), timeout_seconds=2.0)
        finally:
            await decider

        # Flip the task terminal so the SSE loop closes.
        await task_repo.update_state(
            _TENANT,
            "task-1",
            TaskState.SUCCEEDED,
            datetime(2026, 6, 23, 12, 0, 5, tzinfo=UTC),
        )

    app = _make_app(task_repo=task_repo, gate=gate)
    producer_task = asyncio.create_task(producer())
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", timeout=5.0
        ) as client:
            response = await client.get("/v1/tasks/task-1/permissions/stream", headers=_BEARER)
    finally:
        await producer_task

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert "event: permission.prompt" in body
    # Decode the JSON payload to confirm the prompt fields survived
    # the wire round-trip.
    data_lines = [
        line for line in body.splitlines() if line.startswith("data: ") and "prompt_id" in line
    ]
    decoded = json.loads(data_lines[0][len("data: ") :])
    assert decoded["prompt_id"] == "prm-api-1"
    assert decoded["tool_name"] == "shell"
    assert "event: task.terminal" in body
    assert '"state": "succeeded"' in body


async def test_subscribe_failure_returns_503() -> None:
    class _BrokenGate(PermissionGate):
        async def request(
            self, prompt: PermissionPrompt, *, timeout_seconds: float
        ) -> PermissionDecision:
            raise AssertionError("not used by endpoint")

        async def deliver(self, decision: PermissionDecision) -> None:
            return None

        async def subscribe_prompts(self, *, tenant_id: str, task_id: str):  # type: ignore[no-untyped-def]
            raise PermissionGateError("redis down")

        async def close(self) -> None:
            return None

    task_repo = FakeTaskRepo()
    await task_repo.upsert(_task(state=TaskState.RUNNING))
    app = _make_app(task_repo=task_repo, gate=_BrokenGate())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/tasks/task-1/permissions/stream", headers=_BEARER)
    assert response.status_code == 503


async def test_unauthorized_without_bearer() -> None:
    gate = InMemoryPermissionGate()
    task_repo = FakeTaskRepo()
    await task_repo.upsert(_task(state=TaskState.RUNNING))
    app = _make_app(task_repo=task_repo, gate=gate)
    del app.dependency_overrides[get_request_ctx]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/tasks/task-1/permissions/stream")
    assert response.status_code == 401


async def test_stream_closes_at_max_duration_when_no_prompts_and_no_terminal() -> None:
    gate = InMemoryPermissionGate()
    task_repo = FakeTaskRepo()
    await task_repo.upsert(_task(state=TaskState.RUNNING))
    app = _make_app(task_repo=task_repo, gate=gate)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", timeout=5.0
    ) as client:
        response = await client.get("/v1/tasks/task-1/permissions/stream", headers=_BEARER)
    assert response.status_code == 200
    assert "event: permission.prompt" not in response.text
    assert "event: task.terminal" not in response.text
