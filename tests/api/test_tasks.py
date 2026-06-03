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
    get_audit_repo,
    get_db_pool,
    get_llm_usage_repo,
    get_outbox_repo,
    get_request_ctx,
    get_task_repo,
    get_task_topic,
    get_token_validator,
)
from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.domain.llm_usage import LLMUsageRecord, LLMUsageStatus
from meta_agent.core.domain.outbox import OutboxStatus
from meta_agent.core.domain.task import BudgetPolicy, Task, TaskState, TaskType
from meta_agent.core.orchestration.result import TaskError, TaskErrorCode, TaskResult
from meta_agent.core.ports.auth import Principal, TokenValidator
from meta_agent.core.ports.llm_usage import UsageAggregate, UsageGroupBy
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


class _FakeAuditRepo:
    def __init__(self, events: list[AuditEvent] | None = None) -> None:
        self._events = events or []

    async def list_for_task_since(
        self,
        tenant_id: str,
        task_id: str,
        *,
        after: tuple[datetime, str] | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        assert tenant_id == _TENANT
        rows = [event for event in self._events if event.task_id == task_id]
        if after is not None:
            rows = [event for event in rows if (event.occurred_at, event.event_id) > after]
        return rows[:limit]


class _FakeUsageRepo:
    def __init__(
        self,
        rows: list[LLMUsageRecord] | None = None,
        buckets: list[UsageAggregate] | None = None,
    ) -> None:
        self._rows = rows or []
        self._buckets = buckets or []

    async def list_for_task(self, tenant_id: str, task_id: str) -> list[LLMUsageRecord]:
        assert tenant_id == _TENANT
        return [row for row in self._rows if row.task_id == task_id]

    async def aggregate_for_task(
        self,
        tenant_id: str,
        task_id: str,
        group_by: UsageGroupBy,
    ) -> list[UsageAggregate]:
        assert tenant_id == _TENANT
        assert group_by is UsageGroupBy.STEP_KIND
        return list(self._buckets)


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
    audit_repo: _FakeAuditRepo | None = None,
    usage_repo: _FakeUsageRepo | None = None,
) -> Any:
    app = create_app(lifespan=None)
    app.dependency_overrides[get_task_repo] = lambda: fake_repo
    app.dependency_overrides[get_outbox_repo] = lambda: fake_outbox
    app.dependency_overrides[get_db_pool] = FakeDbPool
    app.dependency_overrides[get_task_topic] = lambda: _TOPIC
    app.dependency_overrides[get_request_ctx] = _fixed_ctx
    if audit_repo is not None:
        app.dependency_overrides[get_audit_repo] = lambda: audit_repo
    if usage_repo is not None:
        app.dependency_overrides[get_llm_usage_repo] = lambda: usage_repo
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


async def test_submit_bug_fix_task_validates_and_normalises_payload(
    fake_repo: FakeTaskRepo, fake_outbox: FakeOutboxRepo
) -> None:
    app = _make_app(fake_repo, fake_outbox)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/tasks",
            json={
                "task_type": "bug_fix",
                "input_payload": {
                    "issue_description": "Fix discount validation",
                    "repo_url": "https://github.com/acme/repo.git",
                    "target_files": ["src/discount.py", "tests/test_discount.py"],
                    "verify_suite": "python_test",
                    "model": "deepseek/deepseek-v4-pro",
                },
            },
            headers=_HEADERS,
        )
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    task = fake_repo.rows[(_TENANT, task_id)]
    assert task.task_type is TaskType.BUG_FIX
    assert task.input_payload == {
        "issue_description": "Fix discount validation",
        "repo_url": "https://github.com/acme/repo.git",
        "target_files": ["src/discount.py", "tests/test_discount.py"],
        "verify_suite": "python_test",
        "model": "deepseek/deepseek-v4-pro",
    }
    assert task.permission_mode.value == "auto"
    assert task.budget_policy.value == "none"
    assert task.budget_threshold_micros is None


async def test_submit_bug_fix_task_rejects_missing_repo_url(
    fake_repo: FakeTaskRepo, fake_outbox: FakeOutboxRepo
) -> None:
    app = _make_app(fake_repo, fake_outbox)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/tasks",
            json={
                "task_type": "bug_fix",
                "input_payload": {
                    "issue_description": "Fix discount validation",
                    "target_files": ["src/discount.py"],
                    "verify_suite": "python_test",
                },
            },
            headers=_HEADERS,
        )
    assert resp.status_code == 422
    assert fake_repo.rows == {}
    assert fake_outbox.rows == {}


async def test_submit_task_rejects_inline_permission_modes(
    fake_repo: FakeTaskRepo, fake_outbox: FakeOutboxRepo
) -> None:
    app = _make_app(fake_repo, fake_outbox)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/tasks",
            json={
                "task_type": "bug_fix",
                "permission_mode": "approve_each_tool",
                "input_payload": {
                    "issue_description": "Fix discount validation",
                    "repo_url": "https://github.com/acme/repo.git",
                    "target_files": ["src/discount.py"],
                },
            },
            headers=_HEADERS,
        )
    assert resp.status_code == 422
    assert fake_repo.rows == {}
    assert fake_outbox.rows == {}


async def test_submit_bug_fix_task_rejects_unknown_verify_suite(
    fake_repo: FakeTaskRepo, fake_outbox: FakeOutboxRepo
) -> None:
    app = _make_app(fake_repo, fake_outbox)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/tasks",
            json={
                "task_type": "bug_fix",
                "input_payload": {
                    "issue_description": "Fix discount validation",
                    "repo_url": "https://github.com/acme/repo.git",
                    "target_files": ["src/discount.py"],
                    "verify_suite": "go_test",
                },
            },
            headers=_HEADERS,
        )
    assert resp.status_code == 422
    assert fake_repo.rows == {}
    assert fake_outbox.rows == {}


async def test_submit_task_budget_threshold_round_trips(
    fake_repo: FakeTaskRepo, fake_outbox: FakeOutboxRepo
) -> None:
    app = _make_app(fake_repo, fake_outbox)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/tasks",
            json={
                "task_type": "bug_fix",
                "budget_policy": "gate_on_threshold",
                "budget_threshold_micros": 5000,
                "input_payload": {
                    "issue_description": "Fix discount validation",
                    "repo_url": "https://github.com/acme/repo.git",
                    "target_files": ["src/discount.py"],
                },
            },
            headers=_HEADERS,
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["budget_policy"] == "gate_on_threshold"
    assert body["budget_threshold_micros"] == 5000
    task = fake_repo.rows[(_TENANT, body["task_id"])]
    assert task.budget_policy.value == "gate_on_threshold"
    assert task.budget_threshold_micros == 5000


async def test_submit_task_rejects_budget_policy_without_threshold(
    fake_repo: FakeTaskRepo, fake_outbox: FakeOutboxRepo
) -> None:
    app = _make_app(fake_repo, fake_outbox)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/tasks",
            json={
                "task_type": "bug_fix",
                "budget_policy": "abort_on_threshold",
                "input_payload": {
                    "issue_description": "Fix discount validation",
                    "repo_url": "https://github.com/acme/repo.git",
                    "target_files": ["src/discount.py"],
                },
            },
            headers=_HEADERS,
        )
    assert resp.status_code == 422
    assert fake_repo.rows == {}
    assert fake_outbox.rows == {}


async def test_submit_task_rejects_threshold_without_budget_policy(
    fake_repo: FakeTaskRepo, fake_outbox: FakeOutboxRepo
) -> None:
    app = _make_app(fake_repo, fake_outbox)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/tasks",
            json={
                "task_type": "bug_fix",
                "budget_policy": "none",
                "budget_threshold_micros": 5000,
                "input_payload": {
                    "issue_description": "Fix discount validation",
                    "repo_url": "https://github.com/acme/repo.git",
                    "target_files": ["src/discount.py"],
                },
            },
            headers=_HEADERS,
        )
    assert resp.status_code == 422
    assert fake_repo.rows == {}
    assert fake_outbox.rows == {}


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


async def test_get_task_observability_summary(
    fake_repo: FakeTaskRepo, fake_outbox: FakeOutboxRepo
) -> None:
    now = datetime(2026, 6, 2, tzinfo=UTC)
    task = Task(
        task_id="t-obs",
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id="trace-obs",
        task_type=TaskType.BUG_FIX,
        state=TaskState.SUCCEEDED,
        budget_policy=BudgetPolicy.GATE_ON_THRESHOLD,
        budget_threshold_micros=5000,
        input_payload={},
        created_at=now,
        updated_at=now,
    )
    result = TaskResult(
        task_id="t-obs",
        tenant_id=_TENANT,
        trace_id="trace-obs",
        graph_id="builtin.bug_fix",
        status="succeeded",
        output={
            "verifier_passed": True,
            "attempts": 2,
            "files_changed": ["src/foo.py"],
            "patch": "diff --git a/src/foo.py b/src/foo.py",
        },
        error=None,
        node_sequence=4,
        started_at=now,
        finished_at=now,
    )
    fake_repo.rows[(_TENANT, "t-obs")] = task
    fake_repo.results[(_TENANT, "t-obs")] = result
    fake_repo.rows[(_TENANT, "child-pr-1")] = Task(
        task_id="child-pr-1",
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id="trace-obs-child",
        task_type=TaskType.AUTO_PR,
        state=TaskState.SUCCEEDED,
        input_payload={},
        created_at=now,
        updated_at=now,
    )
    fake_repo.results[(_TENANT, "child-pr-1")] = TaskResult(
        task_id="child-pr-1",
        tenant_id=_TENANT,
        trace_id="trace-obs-child",
        graph_id="builtin.auto_pr",
        status="succeeded",
        output={"action": "created"},
        error=None,
        node_sequence=2,
        started_at=now,
        finished_at=now,
    )
    usage_repo = _FakeUsageRepo(
        rows=[
            LLMUsageRecord(
                record_id="u-1",
                tenant_id=_TENANT,
                trace_id="trace-obs",
                task_id="t-obs",
                provider="openrouter",
                model="deepseek/deepseek-v4-pro",
                total_tokens=100,
                cost_usd_micros=55,
                latency_ms=1234,
                status=LLMUsageStatus.OK,
                step_kind="plan",
                created_at=now,
            ),
            LLMUsageRecord(
                record_id="u-2",
                tenant_id=_TENANT,
                trace_id="trace-obs",
                task_id="t-obs",
                provider="openrouter",
                model="deepseek/deepseek-v4-pro",
                total_tokens=120,
                cost_usd_micros=65,
                latency_ms=1500,
                status=LLMUsageStatus.ERROR,
                step_kind="edit",
                created_at=now,
            ),
        ],
        buckets=[
            UsageAggregate(key="edit", tokens=120, cost_usd_micros=65, calls=1),
            UsageAggregate(key="plan", tokens=100, cost_usd_micros=55, calls=1),
        ],
    )
    audit_repo = _FakeAuditRepo(
        events=[
            AuditEvent(
                event_id="a-1",
                tenant_id=_TENANT,
                principal_id=_PRINCIPAL,
                session_id=None,
                task_id="t-obs",
                trace_id="trace-obs",
                action="tool.invoked",
                payload={},
                occurred_at=now,
            ),
            AuditEvent(
                event_id="a-2",
                tenant_id=_TENANT,
                principal_id=_PRINCIPAL,
                session_id=None,
                task_id="t-obs",
                trace_id="trace-obs",
                action="tool.failed",
                payload={},
                occurred_at=now,
            ),
            AuditEvent(
                event_id="a-3",
                tenant_id=_TENANT,
                principal_id=_PRINCIPAL,
                session_id=None,
                task_id="t-obs",
                trace_id="trace-obs",
                action="task.awaiting_approval",
                payload={"gate_id": "budget"},
                occurred_at=now,
            ),
            AuditEvent(
                event_id="a-4",
                tenant_id=_TENANT,
                principal_id=_PRINCIPAL,
                session_id=None,
                task_id="t-obs",
                trace_id="trace-obs",
                action="task.chain_enqueued",
                payload={
                    "parent_task_id": "t-obs",
                    "child_task_id": "child-pr-1",
                    "follow_up_type": "auto_pr",
                },
                occurred_at=now,
            ),
        ]
    )
    app = _make_app(fake_repo, fake_outbox, audit_repo=audit_repo, usage_repo=usage_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/tasks/t-obs/observability", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == "t-obs"
    assert body["state"] == "succeeded"
    assert body["result_status"] == "succeeded"
    assert body["verifier_passed"] is True
    assert body["attempts"] == 2
    assert body["files_changed"] == ["src/foo.py"]
    assert body["patch_present"] is True
    assert body["llm_calls"] == 2
    assert body["llm_failures"] == 1
    assert body["total_tokens"] == 220
    assert body["total_cost_usd_micros"] == 120
    assert body["tool_events"] == 2
    assert body["tool_failures"] == 1
    assert body["human_interventions"] == 1
    assert body["budget_outcome"] == "approved"
    assert body["auto_pr_child_status"] == "created"
    assert body["cost_by_step_kind"] == {"edit": 65, "plan": 55}
    assert body["models"] == ["deepseek/deepseek-v4-pro"]


async def test_get_task_observability_summary_includes_failure_kind(
    fake_repo: FakeTaskRepo, fake_outbox: FakeOutboxRepo
) -> None:
    now = datetime(2026, 6, 2, tzinfo=UTC)
    task = Task(
        task_id="t-obs-fail",
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id="trace-obs-fail",
        task_type=TaskType.BUG_FIX,
        state=TaskState.FAILED,
        input_payload={},
        created_at=now,
        updated_at=now,
    )
    result = TaskResult(
        task_id="t-obs-fail",
        tenant_id=_TENANT,
        trace_id="trace-obs-fail",
        graph_id="builtin.bug_fix",
        status="failed",
        output={
            "verifier_passed": False,
            "failure_explanation": {
                "category": "verifier_failed",
                "summary": "verification environment failed",
                "retryable": False,
                "details": {
                    "failure_kind": "env_failed",
                },
            },
        },
        error=TaskError(
            code=TaskErrorCode.GRAPH_ERROR,
            message="verifier failed",
            details={},
        ),
        node_sequence=4,
        started_at=now,
        finished_at=now,
    )
    fake_repo.rows[(_TENANT, "t-obs-fail")] = task
    fake_repo.results[(_TENANT, "t-obs-fail")] = result
    app = _make_app(
        fake_repo, fake_outbox, audit_repo=_FakeAuditRepo(), usage_repo=_FakeUsageRepo()
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/tasks/t-obs-fail/observability", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["failure_category"] == "verifier_failed"
    assert body["failure_kind"] == "env_failed"


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
