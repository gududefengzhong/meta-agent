"""Unit tests for :class:`AwaitingApprovalSweeper`."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from meta_agent.core.domain.task import (
    BudgetPolicy,
    PermissionMode,
    Task,
    TaskState,
    TaskType,
)
from meta_agent.worker.expiry_sweeper import AwaitingApprovalSweeper
from tests.worker._fakes import FakeAuditRepo, FakeTaskRepo

NOW = datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC)


def _id_factory() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"id-{next(counter)}"


def _task(
    *,
    task_id: str,
    state: TaskState = TaskState.AWAITING_APPROVAL,
    updated_at: datetime = NOW,
    tenant_id: str = "t-1",
) -> Task:
    return Task(
        task_id=task_id,
        tenant_id=tenant_id,
        principal_id="user-1",
        trace_id=f"trace-{task_id}",
        idempotency_key=f"idem-{task_id}",
        task_type=TaskType.SYSTEM_ECHO,
        graph_id=None,
        state=state,
        permission_mode=PermissionMode.APPROVE_BEFORE_PUSH,
        budget_policy=BudgetPolicy.NONE,
        input_payload={},
        created_at=updated_at,
        updated_at=updated_at,
    )


def _build_sweeper(
    *,
    tasks: FakeTaskRepo | None = None,
    audits: FakeAuditRepo | None = None,
    expiry_days: int = 30,
) -> tuple[AwaitingApprovalSweeper, FakeTaskRepo, FakeAuditRepo]:
    tasks = tasks or FakeTaskRepo()
    audits = audits or FakeAuditRepo()
    return (
        AwaitingApprovalSweeper(
            tasks=tasks,
            audits=audits,
            expiry_days=expiry_days,
            clock=lambda: NOW,
            id_factory=_id_factory(),
        ),
        tasks,
        audits,
    )


async def test_expires_tasks_paused_longer_than_threshold() -> None:
    sweeper, tasks, audits = _build_sweeper(expiry_days=30)
    # 45 days ago → stale
    stale = _task(task_id="stale", updated_at=NOW - timedelta(days=45))
    # 2 days ago → still fresh
    fresh = _task(task_id="fresh", updated_at=NOW - timedelta(days=2))
    await tasks.upsert(stale)
    await tasks.upsert(fresh)

    expired = await sweeper.run_once()

    assert expired == 1
    refreshed_stale = await tasks.get("t-1", "stale")
    refreshed_fresh = await tasks.get("t-1", "fresh")
    assert refreshed_stale is not None
    assert refreshed_stale.state == TaskState.EXPIRED
    assert refreshed_fresh is not None
    assert refreshed_fresh.state == TaskState.AWAITING_APPROVAL
    # Audit emitted so γ-B-2 fanout can push a notification.
    actions = audits.actions()
    assert "task.expired" in actions
    expiry_audit = next(e for e in audits.rows if e.action == "task.expired")
    assert expiry_audit.payload["task_id"] == "stale"
    assert expiry_audit.payload["expired_after_days"] == 30


async def test_does_not_touch_non_awaiting_states() -> None:
    sweeper, tasks, _audits = _build_sweeper()
    running = _task(
        task_id="running",
        state=TaskState.RUNNING,
        updated_at=NOW - timedelta(days=100),
    )
    succeeded = _task(
        task_id="succeeded",
        state=TaskState.SUCCEEDED,
        updated_at=NOW - timedelta(days=100),
    )
    await tasks.upsert(running)
    await tasks.upsert(succeeded)

    expired = await sweeper.run_once()
    assert expired == 0
    assert (await tasks.get("t-1", "running")).state == TaskState.RUNNING  # type: ignore[union-attr]
    assert (await tasks.get("t-1", "succeeded")).state == TaskState.SUCCEEDED  # type: ignore[union-attr]


async def test_handles_lost_race_against_approve_without_aborting_batch() -> None:
    """If a row flips out of AWAITING_APPROVAL between scan and transition,
    the sweeper should log + continue."""

    class _RacingTasks(FakeTaskRepo):
        async def list_awaiting_approval_older_than(
            self,
            threshold_at: datetime,
            *,
            limit: int = 100,
        ) -> list[Task]:
            base = await super().list_awaiting_approval_older_than(threshold_at, limit=limit)
            # Simulate the race: mutate ``raced`` out of AWAITING_APPROVAL
            # right after the scan would have observed it — the
            # transition guard inside the fake will then reject the
            # ``raced`` write and accept the ``ok`` one.
            for t in base:
                if t.task_id == "raced":
                    self.rows[(t.tenant_id, t.task_id)] = t.model_copy(
                        update={"state": TaskState.RUNNING}
                    )
            return base

    racing_tasks = _RacingTasks()
    stale = _task(task_id="raced", updated_at=NOW - timedelta(days=45))
    other = _task(task_id="ok", updated_at=NOW - timedelta(days=45))
    await racing_tasks.upsert(stale)
    await racing_tasks.upsert(other)
    sweeper, _, audits = _build_sweeper(tasks=racing_tasks)

    expired = await sweeper.run_once()
    # ``ok`` still expires; ``raced`` is skipped.
    assert expired == 1
    actions = audits.actions()
    assert actions.count("task.expired") == 1


async def test_constructor_rejects_invalid_args() -> None:
    with pytest.raises(ValueError, match="expiry_days"):
        AwaitingApprovalSweeper(
            tasks=FakeTaskRepo(),
            audits=FakeAuditRepo(),
            expiry_days=0,
        )
    with pytest.raises(ValueError, match="batch_size"):
        AwaitingApprovalSweeper(
            tasks=FakeTaskRepo(),
            audits=FakeAuditRepo(),
            batch_size=0,
        )
