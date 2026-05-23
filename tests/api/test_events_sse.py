"""API tests for ``GET /v1/tasks/{id}/events`` (Phase γ-D SSE).

Focus on the wire shape + lifecycle: 404 when the task is missing,
streamed events carry the expected SSE framing, terminal state
closes the stream cleanly. The polling cadence is squashed via
patching so the test does not have to wait the real interval.
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
    get_audit_repo,
    get_db_pool,
    get_request_ctx,
    get_task_repo,
    get_token_validator,
)
from meta_agent.api.routers import tasks as tasks_router
from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.domain.task import (
    BudgetPolicy,
    PermissionMode,
    Task,
    TaskState,
    TaskType,
)
from meta_agent.core.ports.auth import Principal, TokenValidator
from meta_agent.infra.security.context import RequestContext
from tests.worker._fakes import FakeAuditRepo, FakeTaskRepo

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
        task_type=TaskType.SYSTEM_ECHO,
        graph_id=None,
        state=state,
        permission_mode=PermissionMode.AUTO,
        budget_policy=BudgetPolicy.NONE,
        input_payload={},
        created_at=datetime(2026, 6, 23, tzinfo=UTC),
        updated_at=datetime(2026, 6, 23, tzinfo=UTC),
    )


def _audit(action: str, occurred_at: datetime, *, event_id: str) -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        session_id=None,
        task_id="task-1",
        trace_id="trace-fixed",
        action=action,
        payload={"hint": action},
        occurred_at=occurred_at,
    )


def _make_app(*, task_repo: FakeTaskRepo, audit_repo: FakeAuditRepo) -> Any:
    app = create_app(lifespan=None)
    app.dependency_overrides[get_task_repo] = lambda: task_repo
    app.dependency_overrides[get_audit_repo] = lambda: audit_repo
    app.dependency_overrides[get_db_pool] = _FakeDbPool
    app.dependency_overrides[get_request_ctx] = _fixed_ctx
    app.dependency_overrides[get_token_validator] = _StubTokenValidator
    return app


@pytest.fixture(autouse=True)
def _fast_sse_intervals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Squash the SSE poll / heartbeat / max-duration knobs.

    The real defaults (1.5s poll, 15s heartbeat, 30min cap) would make
    the tests dog-slow. Tests need: tight poll so events stream within
    a few hundred ms, heartbeat far enough that it does NOT appear in
    happy-path assertions, and a sub-second hard cap so the
    "task-still-running, no events" branch terminates promptly.
    """

    monkeypatch.setattr(tasks_router, "_SSE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(tasks_router, "_SSE_HEARTBEAT_INTERVAL_S", 100.0)
    monkeypatch.setattr(tasks_router, "_SSE_MAX_DURATION_S", 0.5)


async def test_missing_task_returns_404() -> None:
    task_repo = FakeTaskRepo()
    audit_repo = FakeAuditRepo()
    app = _make_app(task_repo=task_repo, audit_repo=audit_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/tasks/unknown/events", headers=_BEARER)
    assert response.status_code == 404


async def test_streams_audit_events_and_closes_on_terminal_state() -> None:
    task_repo = FakeTaskRepo()
    audit_repo = FakeAuditRepo()
    # Pre-populate a couple of audit events for the task; then arrange
    # the task to transition to SUCCEEDED so the stream closes
    # promptly.
    await task_repo.upsert(_task(state=TaskState.RUNNING))
    await audit_repo.append(
        _audit(
            "task.node_completed",
            datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC),
            event_id="ev-1",
        )
    )
    await audit_repo.append(
        _audit(
            "task.succeeded",
            datetime(2026, 6, 23, 12, 0, 1, tzinfo=UTC),
            event_id="ev-2",
        )
    )
    # The audit row is independent of the task row in the fake; flip
    # the task state to SUCCEEDED so the stream exits via the
    # terminal-state branch.
    await task_repo.update_state(
        _TENANT, "task-1", TaskState.SUCCEEDED, datetime(2026, 6, 23, 12, 0, 2, tzinfo=UTC)
    )

    app = _make_app(task_repo=task_repo, audit_repo=audit_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/tasks/task-1/events", headers=_BEARER)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    # Both audit rows surfaced with SSE framing.
    assert "id: ev-1" in body
    assert "id: ev-2" in body
    assert "event: task.node_completed" in body
    assert "event: task.succeeded" in body
    # Terminal envelope closed the stream cleanly.
    assert "event: task.terminal" in body
    assert '"state": "succeeded"' in body


async def test_cursor_skips_already_seen_events() -> None:
    task_repo = FakeTaskRepo()
    audit_repo = FakeAuditRepo()
    await task_repo.upsert(_task(state=TaskState.SUCCEEDED))
    t1 = datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 6, 23, 12, 0, 1, tzinfo=UTC)
    await audit_repo.append(_audit("task.node_completed", t1, event_id="ev-1"))
    await audit_repo.append(_audit("task.succeeded", t2, event_id="ev-2"))

    app = _make_app(task_repo=task_repo, audit_repo=audit_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/v1/tasks/task-1/events",
            params={
                "last_event_id": "ev-1",
                "last_event_at": t1.isoformat(),
            },
            headers=_BEARER,
        )
    body = response.text
    # ``ev-1`` filtered out via the keyset cursor; ``ev-2`` survives.
    assert "id: ev-1" not in body
    assert "id: ev-2" in body


async def test_no_events_running_task_terminates_at_max_duration() -> None:
    """Task stays RUNNING with no audit rows; the stream closes via the
    hard duration cap (squashed to 0.5s in the test fixture) without
    spinning forever."""

    task_repo = FakeTaskRepo()
    audit_repo = FakeAuditRepo()
    await task_repo.upsert(_task(state=TaskState.RUNNING))
    app = _make_app(task_repo=task_repo, audit_repo=audit_repo)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", timeout=5.0
    ) as client:
        response = await client.get("/v1/tasks/task-1/events", headers=_BEARER)
    assert response.status_code == 200
    # Stream closed without any data; the terminal envelope is absent
    # because the task never reached a terminal state.
    assert "event: task.terminal" not in response.text


async def test_unauthorized_without_bearer() -> None:
    task_repo = FakeTaskRepo()
    audit_repo = FakeAuditRepo()
    app = _make_app(task_repo=task_repo, audit_repo=audit_repo)
    # Drop the auth override so the real validator dependency runs;
    # the stub validator rejects everything except ``tok-test``.
    del app.dependency_overrides[get_request_ctx]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/tasks/task-1/events")
    assert response.status_code == 401
