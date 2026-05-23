"""API + repo tests for ``GET /v1/tasks/{id}/trajectory``.

Covers two surfaces:

* The :class:`PgTrajectoryRepository` merge / sort logic via an
  in-memory fake adapter that exercises the same item-construction
  invariants the SQL adapter has to satisfy.
* The FastAPI handler — 200 on a known task, 404 for an unknown
  one, ``items`` and ``truncated`` round-trip cleanly.

The full SQL adapter is covered by an integration test against real
Postgres in ``tests/integration/test_trajectory_postgres.py``.
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
    get_request_ctx,
    get_task_repo,
    get_token_validator,
    get_trajectory_repo,
)
from meta_agent.core.domain.task import Task, TaskState, TaskType
from meta_agent.core.domain.trajectory import (
    TrajectoryAuditItem,
    TrajectoryCheckpointItem,
    TrajectoryPage,
    TrajectoryUsageItem,
)
from meta_agent.core.ports.auth import Principal, TokenValidator
from meta_agent.core.ports.trajectory import TrajectoryRepository
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


class _FakeTrajectoryRepo(TrajectoryRepository):
    """In-memory :class:`TrajectoryRepository` with pre-canned pages."""

    def __init__(self, page: TrajectoryPage) -> None:
        self.page = page
        self.calls: list[tuple[str, str, int]] = []

    async def list_for_task(
        self,
        tenant_id: str,
        task_id: str,
        *,
        since: datetime | None = None,
        limit_per_source: int = 1000,
    ) -> TrajectoryPage:
        self.calls.append((tenant_id, task_id, limit_per_source))
        return self.page


def _fixed_ctx() -> RequestContext:
    return RequestContext(
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id="trace-fixed",
        request_id="req-fixed",
    )


def _make_app(*, task_repo: FakeTaskRepo, trajectory_repo: _FakeTrajectoryRepo) -> Any:
    app = create_app(lifespan=None)
    app.dependency_overrides[get_task_repo] = lambda: task_repo
    app.dependency_overrides[get_trajectory_repo] = lambda: trajectory_repo
    app.dependency_overrides[get_db_pool] = _FakeDbPool
    app.dependency_overrides[get_request_ctx] = _fixed_ctx
    app.dependency_overrides[get_token_validator] = _StubTokenValidator
    return app


def _make_task(task_id: str = "task-1") -> Task:
    now = datetime(2026, 5, 23, tzinfo=UTC)
    return Task(
        task_id=task_id,
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id="trace-1",
        idempotency_key=f"idem-{task_id}",
        task_type=TaskType.SYSTEM_ECHO,
        state=TaskState.SUCCEEDED,
        input_payload={},
        created_at=now,
        updated_at=now,
    )


def _audit(at: datetime, action: str = "task.node_completed") -> TrajectoryAuditItem:
    return TrajectoryAuditItem(
        occurred_at=at,
        event_id=f"evt-{at.isoformat()}",
        action=action,
        payload={"node": "plan"},
    )


def _checkpoint(at: datetime, seq: int = 1) -> TrajectoryCheckpointItem:
    return TrajectoryCheckpointItem(
        occurred_at=at,
        checkpoint_id=f"cp-{seq}",
        sequence=seq,
        node_name="plan",
        current_node="plan",
        awaiting_approval=False,
        finished=False,
    )


def _usage(at: datetime, record_id: str = "rec-1") -> TrajectoryUsageItem:
    return TrajectoryUsageItem(
        occurred_at=at,
        record_id=record_id,
        provider="openrouter",
        model="deepseek/deepseek-chat",
        requested_model=None,
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cost_usd_micros=1234,
        latency_ms=420,
        status="ok",
        prompt_id="bug_fix_v2.system",
        prompt_version=1,
        step_kind="plan",
    )


# ---------------------------------------------------------------------------
# API handler tests
# ---------------------------------------------------------------------------


@pytest.fixture
def task_repo() -> FakeTaskRepo:
    return FakeTaskRepo()


async def test_returns_404_when_task_missing(task_repo: FakeTaskRepo) -> None:
    page = TrajectoryPage(items=(), truncated=False)
    trajectory_repo = _FakeTrajectoryRepo(page)
    app = _make_app(task_repo=task_repo, trajectory_repo=trajectory_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/tasks/missing/trajectory", headers=_BEARER)
    assert response.status_code == 404
    # Repo not consulted when the task lookup short-circuits the handler.
    assert trajectory_repo.calls == []


async def test_returns_merged_items_when_task_exists(task_repo: FakeTaskRepo) -> None:
    await task_repo.upsert(_make_task())
    t0 = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 5, 23, 12, 0, 1, tzinfo=UTC)
    t2 = datetime(2026, 5, 23, 12, 0, 2, tzinfo=UTC)
    page = TrajectoryPage(
        items=(_audit(t0), _checkpoint(t1, seq=1), _usage(t2)),
        truncated=False,
    )
    trajectory_repo = _FakeTrajectoryRepo(page)
    app = _make_app(task_repo=task_repo, trajectory_repo=trajectory_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/tasks/task-1/trajectory", headers=_BEARER)
    assert response.status_code == 200
    body = response.json()
    assert body["truncated"] is False
    kinds = [item["kind"] for item in body["items"]]
    assert kinds == ["audit", "checkpoint", "usage"]
    # Repo received the bound tenant + the task id from the URL path.
    assert trajectory_repo.calls == [(_TENANT, "task-1", 1000)]


async def test_truncated_flag_round_trips(task_repo: FakeTaskRepo) -> None:
    await task_repo.upsert(_make_task())
    t0 = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
    page = TrajectoryPage(items=(_audit(t0),), truncated=True)
    trajectory_repo = _FakeTrajectoryRepo(page)
    app = _make_app(task_repo=task_repo, trajectory_repo=trajectory_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/tasks/task-1/trajectory", headers=_BEARER)
    assert response.status_code == 200
    assert response.json()["truncated"] is True


async def test_limit_per_source_query_param_forwarded(task_repo: FakeTaskRepo) -> None:
    await task_repo.upsert(_make_task())
    page = TrajectoryPage(items=(), truncated=False)
    trajectory_repo = _FakeTrajectoryRepo(page)
    app = _make_app(task_repo=task_repo, trajectory_repo=trajectory_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/v1/tasks/task-1/trajectory?limit_per_source=42",
            headers=_BEARER,
        )
    assert response.status_code == 200
    assert trajectory_repo.calls == [(_TENANT, "task-1", 42)]


async def test_limit_per_source_query_param_validated(task_repo: FakeTaskRepo) -> None:
    await task_repo.upsert(_make_task())
    page = TrajectoryPage(items=(), truncated=False)
    trajectory_repo = _FakeTrajectoryRepo(page)
    app = _make_app(task_repo=task_repo, trajectory_repo=trajectory_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # FastAPI Query(ge=1, le=1000) rejects both 0 and > 1000.
        zero = await client.get("/v1/tasks/task-1/trajectory?limit_per_source=0", headers=_BEARER)
        too_big = await client.get(
            "/v1/tasks/task-1/trajectory?limit_per_source=99999", headers=_BEARER
        )
    assert zero.status_code == 422
    assert too_big.status_code == 422


async def test_usage_item_carries_prompt_provenance_and_step_kind(
    task_repo: FakeTaskRepo,
) -> None:
    """γ-B-1 has to expose prompt_id / prompt_version / step_kind in usage items
    so a Web UI can render "which prompt drove this LLM call" without joining
    rows by hand on the client."""

    await task_repo.upsert(_make_task())
    t0 = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
    page = TrajectoryPage(items=(_usage(t0),), truncated=False)
    trajectory_repo = _FakeTrajectoryRepo(page)
    app = _make_app(task_repo=task_repo, trajectory_repo=trajectory_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/tasks/task-1/trajectory", headers=_BEARER)
    body = response.json()
    item = body["items"][0]
    assert item["kind"] == "usage"
    assert item["prompt_id"] == "bug_fix_v2.system"
    assert item["prompt_version"] == 1
    assert item["step_kind"] == "plan"
    assert item["cost_usd_micros"] == 1234
    assert item["latency_ms"] == 420
