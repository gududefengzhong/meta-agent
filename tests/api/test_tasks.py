"""Unit tests for the task submission API.

The FastAPI app is created with ``lifespan=None`` so no real Postgres or
Redis connections are made.  Every infrastructure dep is overridden with
an in-memory fake so the tests are fast and fully offline.
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
    get_outbox_repo,
    get_request_ctx,
    get_task_repo,
    get_task_topic,
    get_token_validator,
)
from meta_agent.core.domain.outbox import OutboxStatus
from meta_agent.core.domain.task import Task, TaskState, TaskType
from meta_agent.core.orchestration.result import TaskError, TaskErrorCode, TaskResult
from meta_agent.core.ports.auth import Principal, TokenValidator
from meta_agent.infra.security.context import RequestContext
from tests.worker._fakes import FakeOutboxRepo, FakeTaskRepo


class _StubTokenValidator(TokenValidator):
    """Resolve a single hardcoded token to the test tenant/principal."""

    async def validate(self, token: str) -> Principal | None:
        if token == "tok-test":
            return Principal(tenant_id="tenant-test", principal_id="user-test")
        return None


# ── Shared fakes ──────────────────────────────────────────────────────────────

_TOPIC = "task.commands"
_TENANT = "tenant-test"
_PRINCIPAL = "user-test"
_HEADERS = {"X-Tenant-Id": _TENANT, "X-Principal-Id": _PRINCIPAL}
_BEARER = {"Authorization": "Bearer tok-test"}


class FakeDbPool:
    """Minimal stand-in that only implements ``transaction()``.

    The submit handler composes the task and outbox writes inside a
    single ``async with pool.transaction()`` block. The fake yields a
    sentinel connection that both fakes accept and ignore.
    """

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


def _make_app(
    fake_repo: FakeTaskRepo,
    fake_outbox: FakeOutboxRepo,
) -> Any:
    app = create_app(lifespan=None)
    app.dependency_overrides[get_task_repo] = lambda: fake_repo
    app.dependency_overrides[get_outbox_repo] = lambda: fake_outbox
    app.dependency_overrides[get_db_pool] = FakeDbPool
    app.dependency_overrides[get_task_topic] = lambda: _TOPIC
    app.dependency_overrides[get_request_ctx] = _fixed_ctx
    return app


@pytest.fixture
def fake_repo() -> FakeTaskRepo:
    return FakeTaskRepo()


@pytest.fixture
def fake_outbox() -> FakeOutboxRepo:
    return FakeOutboxRepo()


# ── POST /v1/tasks ────────────────────────────────────────────────────────────


async def test_submit_task_returns_201(
    fake_repo: FakeTaskRepo, fake_outbox: FakeOutboxRepo
) -> None:
    app = _make_app(fake_repo, fake_outbox)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/tasks",
            json={"task_type": "system_echo", "input_payload": {"message": "hi"}},
            headers=_HEADERS,
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["state"] == "pending"
    assert body["task_type"] == "system_echo"
    assert body["tenant_id"] == _TENANT
    task_id = body["task_id"]

    # Task row persisted in fake repo
    assert (_TENANT, task_id) in fake_repo.rows
    assert len(fake_repo.rows) == 1

    # Outbox row enqueued in the same transaction — the dispatcher will
    # later relay it to the queue.
    assert len(fake_outbox.rows) == 1
    event = next(iter(fake_outbox.rows.values()))
    assert event.aggregate_type == "task"
    assert event.aggregate_id == task_id
    assert event.topic == _TOPIC
    assert event.tenant_id == _TENANT
    assert event.status is OutboxStatus.PENDING
    assert event.payload == {"message": "hi"}


async def test_submit_task_missing_bearer_returns_401(
    fake_repo: FakeTaskRepo, fake_outbox: FakeOutboxRepo
) -> None:
    """No ``Authorization`` header → 401 via real :func:`get_request_ctx`.

    The X-Tenant-Id / X-Principal-Id headers must NOT be enough to
    authenticate: tenancy is taken from the validated bearer token, so
    sending only legacy headers is treated as unauthenticated.
    """
    app = create_app(lifespan=None)
    app.dependency_overrides[get_task_repo] = lambda: fake_repo
    app.dependency_overrides[get_outbox_repo] = lambda: fake_outbox
    app.dependency_overrides[get_db_pool] = FakeDbPool
    app.dependency_overrides[get_task_topic] = lambda: _TOPIC
    app.dependency_overrides[get_token_validator] = _StubTokenValidator
    # No get_request_ctx override → uses real bearer-reading dep

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/tasks",
            json={"task_type": "system_echo"},
            headers=_HEADERS,  # legacy tenant/principal headers; no bearer
        )
    assert resp.status_code == 401


# ── GET /v1/tasks/{task_id} ───────────────────────────────────────────────────


async def test_get_task_found(fake_repo: FakeTaskRepo, fake_outbox: FakeOutboxRepo) -> None:
    now = datetime(2026, 5, 15, tzinfo=UTC)
    task = Task(
        task_id="t-1",
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id="trace-1",
        task_type=TaskType.SYSTEM_ECHO,
        state=TaskState.SUCCEEDED,
        input_payload={},
        created_at=now,
        updated_at=now,
    )
    fake_repo.rows[(_TENANT, "t-1")] = task

    app = _make_app(fake_repo, fake_outbox)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/tasks/t-1", headers=_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["state"] == "succeeded"


async def test_get_task_not_found(fake_repo: FakeTaskRepo, fake_outbox: FakeOutboxRepo) -> None:
    app = _make_app(fake_repo, fake_outbox)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/tasks/nonexistent", headers=_HEADERS)
    assert resp.status_code == 404


# ── GET /v1/tasks/{task_id}/result ───────────────────────────────────────────


async def test_get_result_not_yet_available(
    fake_repo: FakeTaskRepo, fake_outbox: FakeOutboxRepo
) -> None:
    """Task exists but has no result yet → 404."""
    now = datetime(2026, 5, 15, tzinfo=UTC)
    task = Task(
        task_id="t-2",
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id="trace-2",
        task_type=TaskType.SYSTEM_ECHO,
        state=TaskState.RUNNING,
        input_payload={},
        created_at=now,
        updated_at=now,
    )
    fake_repo.rows[(_TENANT, "t-2")] = task
    # No result stored yet

    app = _make_app(fake_repo, fake_outbox)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/tasks/t-2/result", headers=_HEADERS)
    assert resp.status_code == 404
    assert "not yet available" in resp.json()["detail"]


async def test_get_result_succeeded(fake_repo: FakeTaskRepo, fake_outbox: FakeOutboxRepo) -> None:
    now = datetime(2026, 5, 15, tzinfo=UTC)
    task = Task(
        task_id="t-3",
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id="trace-3",
        task_type=TaskType.SYSTEM_ECHO,
        state=TaskState.SUCCEEDED,
        input_payload={},
        created_at=now,
        updated_at=now,
    )
    result = TaskResult(
        task_id="t-3",
        tenant_id=_TENANT,
        trace_id="trace-3",
        graph_id="builtin.echo",
        status="succeeded",
        output={"echo": "hello"},
        node_sequence=3,
        started_at=now,
        finished_at=now,
    )
    fake_repo.rows[(_TENANT, "t-3")] = task
    fake_repo.results[(_TENANT, "t-3")] = result

    app = _make_app(fake_repo, fake_outbox)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/tasks/t-3/result", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["output"] == {"echo": "hello"}
    assert body["graph_id"] == "builtin.echo"
    assert body["error"] is None


async def test_get_result_failed(fake_repo: FakeTaskRepo, fake_outbox: FakeOutboxRepo) -> None:
    now = datetime(2026, 5, 15, tzinfo=UTC)
    task = Task(
        task_id="t-4",
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id="trace-4",
        task_type=TaskType.SYSTEM_ECHO,
        state=TaskState.FAILED,
        input_payload={},
        created_at=now,
        updated_at=now,
    )
    result = TaskResult(
        task_id="t-4",
        tenant_id=_TENANT,
        trace_id="trace-4",
        graph_id="builtin.echo",
        status="failed",
        error=TaskError(code=TaskErrorCode.GRAPH_ERROR, message="something went wrong"),
        node_sequence=1,
        started_at=now,
        finished_at=now,
    )
    fake_repo.rows[(_TENANT, "t-4")] = task
    fake_repo.results[(_TENANT, "t-4")] = result

    app = _make_app(fake_repo, fake_outbox)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/tasks/t-4/result", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "graph_error"
    assert body["output"] is None


# ── Health ────────────────────────────────────────────────────────────────────


async def test_health(fake_repo: FakeTaskRepo, fake_outbox: FakeOutboxRepo) -> None:
    app = _make_app(fake_repo, fake_outbox)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
