"""In-memory fakes for worker unit tests.

These fakes implement just enough of the persistence and delivery
ports to drive :class:`WorkerLoop` deterministically. They are not a
substitute for the integration tests against real Postgres / Redis,
which exercise the SQL and stream serialization paths separately.
"""

from __future__ import annotations

from datetime import datetime

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.domain.checkpoint import TaskCheckpoint
from meta_agent.core.domain.session import Session
from meta_agent.core.domain.task import Task, TaskState
from meta_agent.core.orchestration.result import TaskResult
from meta_agent.core.ports.repository import (
    TERMINAL_TASK_STATES,
    AuditRepository,
    CheckpointRepository,
    IllegalTaskTransitionError,
    SessionRepository,
    TaskRepository,
)
from meta_agent.infra.queue.redis_consumer import DeliveredMessage


class FakeTaskRepo(TaskRepository):
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], Task] = {}
        self.results: dict[tuple[str, str], TaskResult] = {}

    async def upsert(self, task: Task) -> None:
        self.rows[(task.tenant_id, task.task_id)] = task

    async def get(self, tenant_id: str, task_id: str) -> Task | None:
        return self.rows.get((tenant_id, task_id))

    async def list_by_state(self, tenant_id: str, state: TaskState, limit: int = 100) -> list[Task]:
        return [t for t in self.rows.values() if t.tenant_id == tenant_id and t.state == state][
            :limit
        ]

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

    def actions(self) -> list[str]:
        return [e.action for e in self.rows]


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
