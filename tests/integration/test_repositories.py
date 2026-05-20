"""Integration tests for the PG repository adapters.

These tests exercise the real asyncpg path against a containerised
Postgres. They focus on the multi-tenant guard, idempotent upsert
semantics, and cross-tenant isolation; deeper edge cases live in the
unit tests.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.domain.checkpoint import TaskCheckpoint
from meta_agent.core.domain.errors import ErrorCategory
from meta_agent.core.domain.llm_usage import LLMUsageRecord, LLMUsageStatus
from meta_agent.core.domain.outbox import OutboxEvent, OutboxStatus
from meta_agent.core.domain.session import Session
from meta_agent.core.domain.task import Task, TaskState, TaskType
from meta_agent.core.ports.llm_usage import (
    LLMUsageFilter,
    UsageGroupBy,
)
from meta_agent.core.ports.repository import AuditFilter, TenantIsolationError
from meta_agent.infra.persistence import (
    DatabasePool,
    PgAuditRepository,
    PgCheckpointRepository,
    PgLLMUsageRepository,
    PgOutboxRepository,
    PgSessionRepository,
    PgTaskRepository,
)
from meta_agent.infra.security.context import RequestContext, bind_context

pytestmark = pytest.mark.integration


def _ctx(tenant_id: str = "tenant-A") -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id,
        principal_id="user-1",
        trace_id="trace-1",
        request_id="req-1",
    )


def _task(task_id: str, tenant_id: str = "tenant-A") -> Task:
    now = datetime(2026, 5, 14, tzinfo=UTC)
    return Task(
        task_id=task_id,
        tenant_id=tenant_id,
        session_id=None,
        principal_id="user-1",
        trace_id="trace-1",
        idempotency_key=f"idem-{task_id}",
        task_type=TaskType.BUG_FIX,
        state=TaskState.PENDING,
        input_payload={"goal": "test"},
        created_at=now,
        updated_at=now,
    )


async def test_task_upsert_and_get_roundtrip(db_pool: DatabasePool) -> None:
    repo = PgTaskRepository(db_pool)
    task = _task("t-1")
    with bind_context(_ctx()):
        await repo.upsert(task)
        fetched = await repo.get("tenant-A", "t-1")
    assert fetched == task


async def test_task_graph_id_round_trips(db_pool: DatabasePool) -> None:
    repo = PgTaskRepository(db_pool)
    base = _task("t-graph")
    pinned = base.model_copy(update={"graph_id": "builtin.echo"})
    with bind_context(_ctx()):
        await repo.upsert(pinned)
        fetched = await repo.get("tenant-A", "t-graph")
    assert fetched is not None
    assert fetched.graph_id == "builtin.echo"


async def test_task_get_returns_none_for_other_tenant_isolation(
    db_pool: DatabasePool,
) -> None:
    repo = PgTaskRepository(db_pool)
    with bind_context(_ctx("tenant-A")):
        await repo.upsert(_task("t-1"))
    with bind_context(_ctx("tenant-B")):
        assert await repo.get("tenant-B", "t-1") is None


async def test_task_repo_rejects_cross_tenant_write(db_pool: DatabasePool) -> None:
    repo = PgTaskRepository(db_pool)
    with bind_context(_ctx("tenant-A")), pytest.raises(TenantIsolationError):
        await repo.upsert(_task("t-1", tenant_id="tenant-B"))


async def test_task_list_by_state_filters_by_tenant(db_pool: DatabasePool) -> None:
    repo = PgTaskRepository(db_pool)
    with bind_context(_ctx("tenant-A")):
        await repo.upsert(_task("t-A1"))
        await repo.upsert(_task("t-A2"))
    with bind_context(_ctx("tenant-B")):
        await repo.upsert(_task("t-B1", tenant_id="tenant-B"))
    with bind_context(_ctx("tenant-A")):
        rows = await repo.list_by_state("tenant-A", TaskState.PENDING)
    assert {t.task_id for t in rows} == {"t-A1", "t-A2"}


async def test_session_upsert_and_touch(db_pool: DatabasePool) -> None:
    repo = PgSessionRepository(db_pool)
    now = datetime(2026, 5, 14, tzinfo=UTC)
    session = Session(
        session_id="s-1",
        tenant_id="tenant-A",
        principal_id="user-1",
        created_at=now,
        last_active_at=now,
    )
    later = datetime(2026, 5, 14, 1, tzinfo=UTC)
    with bind_context(_ctx()):
        await repo.upsert(session)
        await repo.touch("tenant-A", "s-1", later)
        fetched = await repo.get("tenant-A", "s-1")
    assert fetched is not None
    assert fetched.last_active_at == later


async def test_outbox_enqueue_claim_and_dispatch(db_pool: DatabasePool) -> None:
    repo = PgOutboxRepository(db_pool)
    now = datetime(2026, 5, 14, tzinfo=UTC)
    event = OutboxEvent(
        event_id="e-1",
        tenant_id="tenant-A",
        trace_id="trace-1",
        aggregate_type="task",
        aggregate_id="t-1",
        topic="task.events",
        payload={"k": "v"},
        idempotency_key="idem-e-1",
        created_at=now,
    )
    with bind_context(_ctx()):
        await repo.enqueue(event)
    claimed = await repo.claim_pending(batch_size=10, now=now)
    assert [e.event_id for e in claimed] == ["e-1"]
    await repo.mark_dispatched("e-1", dispatched_at=now)
    fetched = await repo.get("e-1")
    assert fetched is not None
    assert fetched.status is OutboxStatus.DISPATCHED


async def test_audit_append_and_list_recent(db_pool: DatabasePool) -> None:
    repo = PgAuditRepository(db_pool)
    now = datetime(2026, 5, 14, tzinfo=UTC)
    event = AuditEvent(
        event_id="a-1",
        tenant_id="tenant-A",
        principal_id="user-1",
        trace_id="trace-1",
        action="task.submitted",
        payload={"task_id": "t-1"},
        occurred_at=now,
    )
    with bind_context(_ctx()):
        await repo.append(event)
        rows = await repo.list_recent("tenant-A")
    assert rows == [event]


def _usage(
    record_id: str,
    *,
    tenant_id: str = "tenant-A",
    task_id: str | None = "t-1",
    status: LLMUsageStatus = LLMUsageStatus.OK,
    created_at: datetime | None = None,
) -> LLMUsageRecord:
    return LLMUsageRecord(
        record_id=record_id,
        tenant_id=tenant_id,
        trace_id="trace-1",
        request_id="req-1",
        principal_id="user-1",
        task_id=task_id,
        provider="openrouter",
        model="openai/gpt-4o" if status is LLMUsageStatus.OK else None,
        requested_model="openai/gpt-4o",
        prompt_tokens=12 if status is LLMUsageStatus.OK else None,
        completion_tokens=34 if status is LLMUsageStatus.OK else None,
        total_tokens=46 if status is LLMUsageStatus.OK else None,
        finish_reason="stop" if status is LLMUsageStatus.OK else None,
        provider_response_id="gen_abc" if status is LLMUsageStatus.OK else None,
        cost_usd_micros=None,
        latency_ms=210,
        status=status,
        error_category=None if status is LLMUsageStatus.OK else ErrorCategory.TRANSIENT,
        error_message=None if status is LLMUsageStatus.OK else "upstream 503",
        created_at=created_at or datetime(2026, 5, 16, tzinfo=UTC),
    )


async def test_llm_usage_record_and_list_for_task(db_pool: DatabasePool) -> None:
    repo = PgLLMUsageRepository(db_pool)
    first = _usage("llmu-1", created_at=datetime(2026, 5, 16, 12, 0, tzinfo=UTC))
    second = _usage(
        "llmu-2",
        status=LLMUsageStatus.ERROR,
        created_at=datetime(2026, 5, 16, 12, 1, tzinfo=UTC),
    )
    with bind_context(_ctx()):
        await repo.record(first)
        await repo.record(second)
        rows = await repo.list_for_task("tenant-A", "t-1")
    assert [r.record_id for r in rows] == ["llmu-1", "llmu-2"]
    assert rows[1].status is LLMUsageStatus.ERROR
    assert rows[1].error_category is ErrorCategory.TRANSIENT


async def test_llm_usage_record_is_idempotent_on_record_id(db_pool: DatabasePool) -> None:
    repo = PgLLMUsageRepository(db_pool)
    record = _usage("llmu-dup")
    with bind_context(_ctx()):
        await repo.record(record)
        await repo.record(record)
        rows = await repo.list_for_task("tenant-A", "t-1")
    assert sum(1 for r in rows if r.record_id == "llmu-dup") == 1


async def test_llm_usage_rejects_cross_tenant_write(db_pool: DatabasePool) -> None:
    repo = PgLLMUsageRepository(db_pool)
    with bind_context(_ctx("tenant-A")), pytest.raises(TenantIsolationError):
        await repo.record(_usage("llmu-x", tenant_id="tenant-B"))


async def test_llm_usage_list_isolates_tenants(db_pool: DatabasePool) -> None:
    repo = PgLLMUsageRepository(db_pool)
    with bind_context(_ctx("tenant-A")):
        await repo.record(_usage("llmu-iso-A"))
    with bind_context(_ctx("tenant-B")):
        await repo.record(_usage("llmu-iso-B", tenant_id="tenant-B"))
        rows = await repo.list_for_task("tenant-B", "t-1")
    assert {r.record_id for r in rows} == {"llmu-iso-B"}


async def test_audit_list_filtered_keyset_paginates(db_pool: DatabasePool) -> None:
    """Two-page keyset walk: page 1 → cursor → page 2 → exhausted."""
    repo = PgAuditRepository(db_pool)
    base = datetime(2026, 5, 20, 9, 0, tzinfo=UTC)
    # Insert four events at distinct timestamps.
    events = [
        AuditEvent(
            event_id=f"af-{i}",
            tenant_id="tenant-A",
            principal_id="user-1",
            trace_id="trace-1",
            task_id="t-1" if i % 2 == 0 else "t-2",
            action="task.submitted" if i < 2 else "task.completed",
            payload={"i": i},
            occurred_at=base.replace(minute=i),
        )
        for i in range(4)
    ]
    with bind_context(_ctx()):
        for e in events:
            await repo.append(e)
        # Window covers all four.
        window = (base.replace(minute=0), base.replace(hour=10))
        page1 = await repo.list_filtered(
            "tenant-A",
            AuditFilter(since=window[0], until=window[1], limit=2),
        )
        assert [e.event_id for e in page1] == ["af-3", "af-2"]
        last = page1[-1]
        page2 = await repo.list_filtered(
            "tenant-A",
            AuditFilter(
                since=window[0],
                until=window[1],
                limit=2,
                before=(last.occurred_at, last.event_id),
            ),
        )
        assert [e.event_id for e in page2] == ["af-1", "af-0"]


async def test_audit_list_filtered_action_and_task_filters(db_pool: DatabasePool) -> None:
    repo = PgAuditRepository(db_pool)
    base = datetime(2026, 5, 21, 9, 0, tzinfo=UTC)
    events = [
        AuditEvent(
            event_id="afa-0",
            tenant_id="tenant-A",
            principal_id="user-1",
            trace_id="trace-1",
            task_id="t-1",
            action="task.submitted",
            payload={},
            occurred_at=base,
        ),
        AuditEvent(
            event_id="afa-1",
            tenant_id="tenant-A",
            principal_id="user-1",
            trace_id="trace-1",
            task_id="t-2",
            action="task.submitted",
            payload={},
            occurred_at=base.replace(minute=1),
        ),
        AuditEvent(
            event_id="afa-2",
            tenant_id="tenant-A",
            principal_id="user-1",
            trace_id="trace-1",
            task_id="t-1",
            action="task.completed",
            payload={},
            occurred_at=base.replace(minute=2),
        ),
    ]
    with bind_context(_ctx()):
        for e in events:
            await repo.append(e)
        rows = await repo.list_filtered(
            "tenant-A",
            AuditFilter(
                since=base,
                until=base.replace(hour=10),
                action="task.submitted",
                task_id="t-1",
                limit=100,
            ),
        )
    assert [e.event_id for e in rows] == ["afa-0"]


async def test_llm_usage_list_filtered_and_keyset(db_pool: DatabasePool) -> None:
    repo = PgLLMUsageRepository(db_pool)
    base = datetime(2026, 5, 22, 10, 0, tzinfo=UTC)
    records = [
        _usage("lf-0", task_id="t-A", created_at=base),
        _usage("lf-1", task_id="t-A", created_at=base.replace(minute=1)),
        _usage(
            "lf-2",
            task_id="t-B",
            status=LLMUsageStatus.ERROR,
            created_at=base.replace(minute=2),
        ),
    ]
    with bind_context(_ctx()):
        for r in records:
            await repo.record(r)
        # Filter by task — should return only the two t-A rows, DESC.
        rows = await repo.list_filtered(
            "tenant-A",
            LLMUsageFilter(
                since=base,
                until=base.replace(hour=11),
                task_id="t-A",
                limit=100,
            ),
        )
        assert [r.record_id for r in rows] == ["lf-1", "lf-0"]
        # Keyset cursor: jump past lf-1.
        page2 = await repo.list_filtered(
            "tenant-A",
            LLMUsageFilter(
                since=base,
                until=base.replace(hour=11),
                task_id="t-A",
                limit=100,
                before=(rows[0].created_at, rows[0].record_id),
            ),
        )
        assert [r.record_id for r in page2] == ["lf-0"]


async def test_llm_usage_aggregate_grouped_by_model(db_pool: DatabasePool) -> None:
    repo = PgLLMUsageRepository(db_pool)
    base = datetime(2026, 5, 23, 9, 0, tzinfo=UTC)
    with bind_context(_ctx()):
        await repo.record(_usage("agg-1", created_at=base))
        await repo.record(_usage("agg-2", created_at=base.replace(minute=1)))
        buckets = await repo.aggregate_grouped(
            "tenant-A",
            base,
            base.replace(hour=10),
            UsageGroupBy.MODEL,
        )
    assert len(buckets) == 1
    bucket = buckets[0]
    assert bucket.key == "openai/gpt-4o"
    # _usage helper writes total_tokens=46 for OK rows.
    assert bucket.tokens == 92
    assert bucket.calls == 2


async def test_checkpoint_append_and_latest(db_pool: DatabasePool) -> None:
    repo = PgCheckpointRepository(db_pool)
    now = datetime(2026, 5, 14, tzinfo=UTC)
    for seq in (0, 1, 2):
        cp = TaskCheckpoint(
            checkpoint_id=f"cp-{seq}",
            task_id="t-1",
            tenant_id="tenant-A",
            trace_id="trace-1",
            node_name="planner",
            sequence=seq,
            state_snapshot={"step": seq},
            created_at=now,
        )
        with bind_context(_ctx()):
            await repo.append(cp)
    with bind_context(_ctx()):
        latest = await repo.latest("tenant-A", "t-1")
    assert latest is not None
    assert latest.sequence == 2
