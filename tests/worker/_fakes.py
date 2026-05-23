"""In-memory fakes for worker unit tests.

These fakes implement just enough of the persistence and delivery
ports to drive :class:`WorkerLoop` deterministically. They are not a
substitute for the integration tests against real Postgres / Redis,
which exercise the SQL and stream serialization paths separately.
"""

from __future__ import annotations

from datetime import UTC, datetime

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.domain.checkpoint import TaskCheckpoint
from meta_agent.core.domain.outbox import OutboxEvent, OutboxStatus
from meta_agent.core.domain.session import Session
from meta_agent.core.domain.task import Task, TaskState
from meta_agent.core.domain.workspace import Workspace
from meta_agent.core.orchestration.result import TaskResult
from meta_agent.core.ports.repository import (
    TERMINAL_TASK_STATES,
    AuditFilter,
    AuditRepository,
    CheckpointRepository,
    IllegalTaskTransitionError,
    OutboxRepository,
    SessionRepository,
    TaskRepository,
)
from meta_agent.core.ports.task_submitter import FollowUpSpec, TaskSubmitter
from meta_agent.core.ports.workspace import WorkspaceError, WorkspaceManager
from meta_agent.infra.queue.redis_consumer import DeliveredMessage


class FakeTaskRepo(TaskRepository):
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], Task] = {}
        self.results: dict[tuple[str, str], TaskResult] = {}

    async def upsert(self, task: Task) -> None:
        self.rows[(task.tenant_id, task.task_id)] = task

    async def upsert_in_conn(self, task: Task, conn: object) -> None:
        # ``conn`` is ignored by the in-memory fake; signature matches
        # the SQL adapter so router tests exercise the same call site.
        self.rows[(task.tenant_id, task.task_id)] = task

    async def get(self, tenant_id: str, task_id: str) -> Task | None:
        return self.rows.get((tenant_id, task_id))

    async def list_by_state(self, tenant_id: str, state: TaskState, limit: int = 100) -> list[Task]:
        return [t for t in self.rows.values() if t.tenant_id == tenant_id and t.state == state][
            :limit
        ]

    async def list_running_for_resume(self, limit: int = 100) -> list[Task]:
        # Cross-tenant scan, no guard — mirrors the SQL adapter's
        # "dispatcher-mode" semantics used by ``WorkerLoop.recover_in_flight``.
        return [t for t in self.rows.values() if t.state == TaskState.RUNNING][:limit]

    async def list_awaiting_approval_older_than(
        self,
        threshold_at: datetime,
        *,
        limit: int = 100,
    ) -> list[Task]:
        rows = [
            t
            for t in self.rows.values()
            if t.state == TaskState.AWAITING_APPROVAL and t.updated_at < threshold_at
        ]
        rows.sort(key=lambda t: t.updated_at)
        return rows[:limit]

    async def set_awaiting_approval(
        self,
        tenant_id: str,
        task_id: str,
        updated_at: datetime,
    ) -> None:
        key = (tenant_id, task_id)
        existing = self.rows.get(key)
        if existing is None or existing.state != TaskState.RUNNING:
            raise IllegalTaskTransitionError(
                f"task {task_id!r} cannot transition to AWAITING_APPROVAL: "
                "row missing or not in RUNNING"
            )
        self.rows[key] = existing.model_copy(
            update={"state": TaskState.AWAITING_APPROVAL, "updated_at": updated_at}
        )

    async def transition_from_awaiting_approval(
        self,
        tenant_id: str,
        task_id: str,
        new_state: TaskState,
        updated_at: datetime,
    ) -> None:
        key = (tenant_id, task_id)
        existing = self.rows.get(key)
        if existing is None or existing.state != TaskState.AWAITING_APPROVAL:
            raise IllegalTaskTransitionError(
                f"task {task_id!r} cannot transition from AWAITING_APPROVAL to "
                f"{new_state.value!r}: row missing or in a different state"
            )
        self.rows[key] = existing.model_copy(update={"state": new_state, "updated_at": updated_at})

    async def update_state(
        self,
        tenant_id: str,
        task_id: str,
        new_state: TaskState,
        updated_at: datetime,
    ) -> None:
        key = (tenant_id, task_id)
        if key not in self.rows:
            return
        existing = self.rows[key]
        self.rows[key] = existing.model_copy(update={"state": new_state, "updated_at": updated_at})

    async def complete(
        self,
        tenant_id: str,
        task_id: str,
        *,
        result: TaskResult,
        terminal_state: TaskState,
        updated_at: datetime,
    ) -> None:
        if terminal_state not in TERMINAL_TASK_STATES:
            raise IllegalTaskTransitionError(
                f"complete() requires a terminal state, got {terminal_state.value!r}"
            )
        key = (tenant_id, task_id)
        existing = self.rows.get(key)
        if existing is None or existing.state in TERMINAL_TASK_STATES:
            raise IllegalTaskTransitionError(
                f"task {task_id!r} cannot transition to {terminal_state.value!r}: "
                "row missing or already in a terminal state"
            )
        self.rows[key] = existing.model_copy(
            update={"state": terminal_state, "updated_at": updated_at}
        )
        self.results[key] = result

    async def get_result(self, tenant_id: str, task_id: str) -> TaskResult | None:
        return self.results.get((tenant_id, task_id))


class FakeCheckpointRepo(CheckpointRepository):
    def __init__(self) -> None:
        self.rows: list[TaskCheckpoint] = []

    async def append(self, checkpoint: TaskCheckpoint) -> None:
        self.rows.append(checkpoint)

    async def latest(self, tenant_id: str, task_id: str) -> TaskCheckpoint | None:
        candidates = [c for c in self.rows if c.tenant_id == tenant_id and c.task_id == task_id]
        return max(candidates, key=lambda c: c.sequence) if candidates else None

    async def list_for_task(self, tenant_id: str, task_id: str) -> list[TaskCheckpoint]:
        rows = [c for c in self.rows if c.tenant_id == tenant_id and c.task_id == task_id]
        return sorted(rows, key=lambda c: c.sequence)


class FakeAuditRepo(AuditRepository):
    def __init__(self) -> None:
        self.rows: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> None:
        self.rows.append(event)

    async def list_recent(self, tenant_id: str, limit: int = 100) -> list[AuditEvent]:
        return [e for e in self.rows if e.tenant_id == tenant_id][:limit]

    async def list_filtered(
        self,
        tenant_id: str,
        filt: AuditFilter,
    ) -> list[AuditEvent]:
        # Worker-side fake: query path is not exercised here, the API
        # layer has its own dedicated fakes in tests/api/test_queries.py.
        raise AssertionError("list_filtered not exercised by worker fakes")

    async def list_for_task_since(
        self,
        tenant_id: str,
        task_id: str,
        *,
        after: tuple[datetime, str] | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        rows = [e for e in self.rows if e.tenant_id == tenant_id and e.task_id == task_id]
        rows.sort(key=lambda e: (e.occurred_at, e.event_id))
        if after is not None:
            cur_at, cur_id = after
            rows = [e for e in rows if (e.occurred_at, e.event_id) > (cur_at, cur_id)]
        return rows[:limit]

    def actions(self) -> list[str]:
        return [e.action for e in self.rows]


class FakeOutboxRepo(OutboxRepository):
    """In-memory :class:`OutboxRepository` for API / submit-path tests.

    Mirrors the SQL adapter contract (``enqueue`` / ``enqueue_in_conn``
    both append a row) so router tests can assert that the transactional
    submit really wrote an outbox event.
    """

    def __init__(self) -> None:
        self.rows: dict[str, OutboxEvent] = {}

    async def enqueue(self, event: OutboxEvent) -> None:
        self.rows[event.event_id] = event

    async def enqueue_in_conn(self, event: OutboxEvent, conn: object) -> None:
        # ``conn`` is ignored by the fake; the test pool produces a
        # sentinel object whose only purpose is to satisfy the API.
        self.rows[event.event_id] = event

    async def claim_pending(
        self,
        *,
        batch_size: int,
        now: datetime,
    ) -> list[OutboxEvent]:
        pending = [e for e in self.rows.values() if e.status is OutboxStatus.PENDING]
        return pending[:batch_size]

    async def mark_dispatched(self, event_id: str, *, dispatched_at: datetime) -> None:
        existing = self.rows.get(event_id)
        if existing is None:
            return
        self.rows[event_id] = existing.model_copy(
            update={"status": OutboxStatus.DISPATCHED, "dispatched_at": dispatched_at},
        )

    async def mark_failed(
        self,
        event_id: str,
        *,
        error: str,
        next_attempt_at: datetime | None,
        terminal: bool,
    ) -> None:
        existing = self.rows.get(event_id)
        if existing is None:
            return
        new_status = OutboxStatus.FAILED if terminal else OutboxStatus.PENDING
        self.rows[event_id] = existing.model_copy(
            update={"status": new_status, "attempts": existing.attempts + 1},
        )

    async def get(self, event_id: str) -> OutboxEvent | None:
        return self.rows.get(event_id)

    async def count_by_status(self, tenant_id: str, status: OutboxStatus) -> int:
        return sum(1 for e in self.rows.values() if e.tenant_id == tenant_id and e.status is status)


class FakeSessionRepo(SessionRepository):
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], Session] = {}

    async def upsert(self, session: Session) -> None:
        self.rows[(session.tenant_id, session.session_id)] = session

    async def get(self, tenant_id: str, session_id: str) -> Session | None:
        return self.rows.get((tenant_id, session_id))

    async def touch(self, tenant_id: str, session_id: str, last_active_at: datetime) -> None:
        key = (tenant_id, session_id)
        if key in self.rows:
            self.rows[key] = self.rows[key].model_copy(update={"last_active_at": last_active_at})


class FakeWorkspaceManager(WorkspaceManager):
    """In-memory :class:`WorkspaceManager` for worker dispatch tests.

    Tracks every provision / cleanup call and exposes hooks to force
    failures on either side so the dispatcher's audit and retry
    behaviour can be asserted without invoking ``git``.
    """

    def __init__(
        self,
        *,
        fail_provision: bool = False,
        fail_cleanup: bool = False,
    ) -> None:
        self.provisioned: list[Workspace] = []
        self.cleaned: list[Workspace] = []
        self._fail_provision = fail_provision
        self._fail_cleanup = fail_cleanup
        self._counter = 0

    async def provision(
        self,
        *,
        tenant_id: str,
        task_id: str,
        trace_id: str,
        branch: str,
        repo_url: str | None = None,
        base_ref: str | None = None,
    ) -> Workspace:
        if self._fail_provision:
            raise WorkspaceError("provision boom")
        self._counter += 1
        from datetime import UTC, datetime

        ws = Workspace(
            workspace_id=f"ws-{self._counter}",
            tenant_id=tenant_id,
            task_id=task_id,
            trace_id=trace_id,
            repo_url=repo_url,
            base_ref=base_ref,
            branch=branch,
            worktree_path=f"/tmp/fake-ws/{self._counter}/feature",
            created_at=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
        )
        self.provisioned.append(ws)
        return ws

    async def cleanup(self, workspace: Workspace) -> None:
        if self._fail_cleanup:
            raise WorkspaceError("cleanup boom")
        self.cleaned.append(workspace)


class FakeStream:
    """In-memory :class:`DeliveryStream`."""

    def __init__(self) -> None:
        self._batches: list[list[DeliveredMessage]] = []
        self.acked: list[str] = []

    def push(self, batch: list[DeliveredMessage]) -> None:
        self._batches.append(batch)

    async def claim_batch(self, *, block_ms: int | None = None) -> list[DeliveredMessage]:
        return self._batches.pop(0) if self._batches else []

    async def ack(self, entry_id: str) -> None:
        self.acked.append(entry_id)


class FakeTaskSubmitter(TaskSubmitter):
    """In-memory :class:`TaskSubmitter` for runner chain-hook tests.

    Records every ``(parent, follow_up)`` pair and mints synthetic
    child task ids. ``simulate_duplicate`` mirrors the SQL adapter's
    ``UniqueViolationError`` path so callers can assert that the
    runner audits ``chain_skipped`` without raising. ``raise_on_submit``
    forces the failure branch.
    """

    def __init__(
        self,
        *,
        simulate_duplicate: bool = False,
        raise_on_submit: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[Task, FollowUpSpec]] = []
        self._simulate_duplicate = simulate_duplicate
        self._raise = raise_on_submit
        self._counter = 0

    async def submit_follow_up(
        self,
        parent: Task,
        follow_up: FollowUpSpec,
    ) -> Task | None:
        self.calls.append((parent, follow_up))
        if self._raise is not None:
            raise self._raise
        if self._simulate_duplicate:
            return None
        self._counter += 1
        now = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
        return Task(
            task_id=f"child-{self._counter}",
            tenant_id=parent.tenant_id,
            session_id=parent.session_id,
            principal_id=parent.principal_id,
            trace_id=parent.trace_id,
            idempotency_key=follow_up.idempotency_key,
            task_type=follow_up.task_type,
            state=TaskState.PENDING,
            input_payload=dict(follow_up.input_payload),
            created_at=now,
            updated_at=now,
        )
