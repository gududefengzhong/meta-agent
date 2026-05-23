"""Integration test: :class:`PgTrajectoryRepository` against real Postgres.

Writes one row into each of the three source tables (``audit_events``,
``task_checkpoints``, ``llm_usage_logs``) and asserts the merge
returns them in timestamp order with the correct discriminated kinds.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.domain.checkpoint import TaskCheckpoint
from meta_agent.core.domain.llm_usage import LLMUsageRecord, LLMUsageStatus
from meta_agent.infra.persistence.audit_repo import PgAuditRepository
from meta_agent.infra.persistence.checkpoint_repo import PgCheckpointRepository
from meta_agent.infra.persistence.llm_usage_repo import PgLLMUsageRepository
from meta_agent.infra.persistence.pool import DatabasePool
from meta_agent.infra.persistence.trajectory_repo import PgTrajectoryRepository
from meta_agent.infra.security.context import RequestContext, bind_context

pytestmark = pytest.mark.integration


def _ctx(tenant_id: str) -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id,
        principal_id="system",
        trace_id=f"trace-{uuid.uuid4().hex[:6]}",
        request_id=f"req-{uuid.uuid4().hex[:6]}",
    )


async def test_merges_three_sources_in_timestamp_order(db_pool: DatabasePool) -> None:
    tenant_id = f"tenant-traj-{uuid.uuid4().hex[:6]}"
    task_id = f"task-{uuid.uuid4().hex[:6]}"
    trace_id = f"trace-{uuid.uuid4().hex[:6]}"
    ctx = _ctx(tenant_id)

    audit_repo = PgAuditRepository(db_pool)
    checkpoint_repo = PgCheckpointRepository(db_pool)
    usage_repo = PgLLMUsageRepository(db_pool)
    trajectory_repo = PgTrajectoryRepository(db_pool)

    t_base = datetime.now(UTC).replace(microsecond=0)
    t_audit = t_base
    t_checkpoint = t_base + timedelta(seconds=1)
    t_usage = t_base + timedelta(seconds=2)
    t_audit2 = t_base + timedelta(seconds=3)

    with bind_context(ctx):
        await audit_repo.append(
            AuditEvent(
                event_id=f"evt-1-{uuid.uuid4().hex[:6]}",
                tenant_id=tenant_id,
                principal_id="system",
                session_id=None,
                task_id=task_id,
                trace_id=trace_id,
                action="task.node_completed",
                payload={"node": "plan"},
                occurred_at=t_audit,
            )
        )
        await checkpoint_repo.append(
            TaskCheckpoint(
                checkpoint_id=f"cp-1-{uuid.uuid4().hex[:6]}",
                task_id=task_id,
                tenant_id=tenant_id,
                trace_id=trace_id,
                node_name="plan",
                sequence=1,
                state_snapshot={
                    "current_node": "plan",
                    "awaiting_approval": False,
                    "finished": False,
                    "data": {"k": "v"},
                },
                created_at=t_checkpoint,
            )
        )
        await usage_repo.record(
            LLMUsageRecord(
                record_id=f"rec-1-{uuid.uuid4().hex[:6]}",
                tenant_id=tenant_id,
                trace_id=trace_id,
                request_id=None,
                principal_id="system",
                session_id=None,
                task_id=task_id,
                provider="openrouter",
                model="deepseek/deepseek-chat",
                requested_model=None,
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                cost_usd_micros=1234,
                latency_ms=420,
                status=LLMUsageStatus.OK,
                prompt_id="bug_fix_v2.system",
                prompt_version=1,
                step_kind="plan",
                created_at=t_usage,
            )
        )
        await audit_repo.append(
            AuditEvent(
                event_id=f"evt-2-{uuid.uuid4().hex[:6]}",
                tenant_id=tenant_id,
                principal_id="system",
                session_id=None,
                task_id=task_id,
                trace_id=trace_id,
                action="task.succeeded",
                payload={},
                occurred_at=t_audit2,
            )
        )

        page = await trajectory_repo.list_for_task(tenant_id, task_id)

    kinds = [item.kind for item in page.items]
    assert kinds == ["audit", "checkpoint", "usage", "audit"]
    assert page.truncated is False
    # Usage item carries the γ-A / β+ provenance columns.
    usage_item = next(i for i in page.items if i.kind == "usage")
    assert usage_item.prompt_id == "bug_fix_v2.system"
    assert usage_item.prompt_version == 1
    assert usage_item.step_kind == "plan"
    # Checkpoint item projects the snapshot, doesn't inline full state.
    cp_item = next(i for i in page.items if i.kind == "checkpoint")
    assert cp_item.current_node == "plan"
    assert cp_item.awaiting_approval is False


async def test_returns_empty_page_when_task_has_no_rows(db_pool: DatabasePool) -> None:
    tenant_id = f"tenant-empty-{uuid.uuid4().hex[:6]}"
    task_id = f"missing-{uuid.uuid4().hex[:6]}"
    trajectory_repo = PgTrajectoryRepository(db_pool)
    with bind_context(_ctx(tenant_id)):
        page = await trajectory_repo.list_for_task(tenant_id, task_id)
    assert page.items == ()
    assert page.truncated is False


async def test_truncated_flag_set_when_source_hits_cap(db_pool: DatabasePool) -> None:
    tenant_id = f"tenant-trunc-{uuid.uuid4().hex[:6]}"
    task_id = f"task-{uuid.uuid4().hex[:6]}"
    trace_id = f"trace-{uuid.uuid4().hex[:6]}"
    ctx = _ctx(tenant_id)
    audit_repo = PgAuditRepository(db_pool)
    trajectory_repo = PgTrajectoryRepository(db_pool)

    base = datetime.now(UTC).replace(microsecond=0)
    with bind_context(ctx):
        for i in range(5):
            await audit_repo.append(
                AuditEvent(
                    event_id=f"evt-{i}-{uuid.uuid4().hex[:6]}",
                    tenant_id=tenant_id,
                    principal_id="system",
                    session_id=None,
                    task_id=task_id,
                    trace_id=trace_id,
                    action="task.node_completed",
                    payload={"i": i},
                    occurred_at=base + timedelta(seconds=i),
                )
            )
        # Cap at 3; one source has 5 → truncated must be True.
        page = await trajectory_repo.list_for_task(tenant_id, task_id, limit_per_source=3)
    assert len(page.items) == 3
    assert page.truncated is True


async def test_since_filter_skips_earlier_rows(db_pool: DatabasePool) -> None:
    tenant_id = f"tenant-since-{uuid.uuid4().hex[:6]}"
    task_id = f"task-{uuid.uuid4().hex[:6]}"
    trace_id = f"trace-{uuid.uuid4().hex[:6]}"
    ctx = _ctx(tenant_id)
    audit_repo = PgAuditRepository(db_pool)
    trajectory_repo = PgTrajectoryRepository(db_pool)

    base = datetime.now(UTC).replace(microsecond=0)
    with bind_context(ctx):
        for i in range(3):
            await audit_repo.append(
                AuditEvent(
                    event_id=f"evt-{i}-{uuid.uuid4().hex[:6]}",
                    tenant_id=tenant_id,
                    principal_id="system",
                    session_id=None,
                    task_id=task_id,
                    trace_id=trace_id,
                    action="task.node_completed",
                    payload={"i": i},
                    occurred_at=base + timedelta(seconds=i),
                )
            )
        # Skip the first row by passing its timestamp.
        page = await trajectory_repo.list_for_task(tenant_id, task_id, since=base)
    assert len(page.items) == 2
    assert all(item.occurred_at > base for item in page.items)
