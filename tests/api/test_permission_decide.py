"""API tests for ``POST /v1/tasks/{id}/permissions/{prompt_id}/decide`` (δ-1).

Drives the endpoint against a stub :class:`PermissionGate` so we
can assert ownership / state validation + decision routing without
spinning up Redis.
"""

from __future__ import annotations

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


class _RecordingGate(PermissionGate):
    def __init__(self, *, raise_on_deliver: BaseException | None = None) -> None:
        self.delivered: list[PermissionDecision] = []
        self._raise = raise_on_deliver

    async def request(
        self, prompt: PermissionPrompt, *, timeout_seconds: float
    ) -> PermissionDecision:
        raise AssertionError("API endpoint does not call request()")

    async def deliver(self, decision: PermissionDecision) -> None:
        if self._raise is not None:
            raise self._raise
        self.delivered.append(decision)

    async def close(self) -> None:
        return None


def _make_app(*, task_repo: FakeTaskRepo, gate: PermissionGate) -> Any:
    app = create_app(lifespan=None)
    app.dependency_overrides[get_task_repo] = lambda: task_repo
    app.dependency_overrides[get_permission_gate] = lambda: gate
    app.dependency_overrides[get_db_pool] = _FakeDbPool
    app.dependency_overrides[get_request_ctx] = _fixed_ctx
    app.dependency_overrides[get_token_validator] = _StubTokenValidator
    return app


async def test_post_allows_delivery_for_running_task() -> None:
    task_repo = FakeTaskRepo()
    await task_repo.upsert(_task(state=TaskState.RUNNING))
    gate = _RecordingGate()
    app = _make_app(task_repo=task_repo, gate=gate)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/tasks/task-1/permissions/prm-1/decide",
            headers=_BEARER,
            json={"allow": True, "reason": "looks fine"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body == {"prompt_id": "prm-1", "allow": True}
    assert len(gate.delivered) == 1
    decision = gate.delivered[0]
    assert decision.prompt_id == "prm-1"
    assert decision.allow is True
    assert decision.reason == "looks fine"


async def test_post_returns_404_when_task_does_not_exist() -> None:
    gate = _RecordingGate()
    app = _make_app(task_repo=FakeTaskRepo(), gate=gate)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/tasks/unknown/permissions/prm-1/decide",
            headers=_BEARER,
            json={"allow": True},
        )
    assert response.status_code == 404
    assert gate.delivered == []


async def test_post_returns_409_for_terminal_task() -> None:
    task_repo = FakeTaskRepo()
    await task_repo.upsert(_task(state=TaskState.SUCCEEDED))
    gate = _RecordingGate()
    app = _make_app(task_repo=task_repo, gate=gate)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/tasks/task-1/permissions/prm-1/decide",
            headers=_BEARER,
            json={"allow": True},
        )
    assert response.status_code == 409
    assert "terminal state" in response.json()["detail"]
    assert gate.delivered == []


async def test_post_returns_503_when_gate_raises() -> None:
    task_repo = FakeTaskRepo()
    await task_repo.upsert(_task(state=TaskState.RUNNING))
    gate = _RecordingGate(raise_on_deliver=PermissionGateError("redis down"))
    app = _make_app(task_repo=task_repo, gate=gate)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/tasks/task-1/permissions/prm-1/decide",
            headers=_BEARER,
            json={"allow": False, "reason": "no"},
        )
    assert response.status_code == 503


async def test_post_unauthorized_without_bearer() -> None:
    task_repo = FakeTaskRepo()
    await task_repo.upsert(_task(state=TaskState.RUNNING))
    gate = _RecordingGate()
    app = _make_app(task_repo=task_repo, gate=gate)
    del app.dependency_overrides[get_request_ctx]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/tasks/task-1/permissions/prm-1/decide",
            json={"allow": True},
        )
    assert response.status_code == 401
    assert gate.delivered == []


async def test_post_rejects_extra_fields_in_body() -> None:
    task_repo = FakeTaskRepo()
    await task_repo.upsert(_task(state=TaskState.RUNNING))
    gate = _RecordingGate()
    app = _make_app(task_repo=task_repo, gate=gate)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/tasks/task-1/permissions/prm-1/decide",
            headers=_BEARER,
            json={"allow": True, "extra": "nope"},
        )
    assert response.status_code == 422


@pytest.mark.parametrize("allow_value", [True, False])
async def test_post_records_allow_value_verbatim(allow_value: bool) -> None:
    task_repo = FakeTaskRepo()
    await task_repo.upsert(_task(state=TaskState.RUNNING))
    gate = _RecordingGate()
    app = _make_app(task_repo=task_repo, gate=gate)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/tasks/task-1/permissions/prm-x/decide",
            headers=_BEARER,
            json={"allow": allow_value},
        )
    assert response.status_code == 200
    assert gate.delivered[0].allow is allow_value
