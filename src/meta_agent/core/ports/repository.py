"""Repository ports for the five persisted aggregates.

Repositories are the only write path into the database. Every mutating
method receives a :class:`RequestContext`-bound tenant identifier
through :func:`meta_agent.infra.security.context.require_tenant_id`;
adapters validate that the tenant matches the entity being written and
raise :class:`TenantIsolationError` on mismatch.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.domain.checkpoint import TaskCheckpoint
from meta_agent.core.domain.errors import AgentError, ErrorCategory
from meta_agent.core.domain.outbox import OutboxEvent, OutboxStatus
from meta_agent.core.domain.session import Session
from meta_agent.core.domain.task import Task, TaskState
from meta_agent.core.orchestration.result import TaskResult
from meta_agent.core.ports.audit_sink import AuditSink


class RepositoryError(AgentError):
    """Base class for repository adapter errors.

    Default category is :class:`ErrorCategory.TRANSIENT` so callers
    retry pure infrastructure hiccups; adapters override ``category``
    on subclasses for non-retryable cases (e.g. constraint violations).
    """

    category = ErrorCategory.TRANSIENT


class TenantIsolationError(RepositoryError):
    """Raised when a write would cross tenant boundaries.

    This is a hard L0 contract failure, never retryable; categorised
    as :class:`ErrorCategory.PERMISSION` to drive escalation.
    """

    category = ErrorCategory.PERMISSION


class IllegalTaskTransitionError(RepositoryError):
    """Raised when :meth:`TaskRepository.complete` is called on a task
    that has already reached a terminal state, or that does not exist.

    Categorised as :class:`ErrorCategory.LOGIC` because the caller has
    a stale view of the lifecycle; retrying the same write blindly is
    never the right answer.
    """

    category = ErrorCategory.LOGIC


# Lifecycle states from which :meth:`TaskRepository.complete` will
# refuse to transition. Kept here (rather than on :class:`TaskState`)
# because the rule lives at the persistence boundary: it is what makes
# "state + result_json" atomic write idempotent under concurrent
# redelivery.
TERMINAL_TASK_STATES: frozenset[TaskState] = frozenset(
    {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}
)


class TaskRepository(ABC):
    """Persistence for :class:`Task` aggregates."""

    @abstractmethod
    async def upsert(self, task: Task) -> None: ...

    @abstractmethod
    async def get(self, tenant_id: str, task_id: str) -> Task | None: ...

    @abstractmethod
    async def list_by_state(
        self,
        tenant_id: str,
        state: TaskState,
        limit: int = 100,
    ) -> list[Task]: ...

    @abstractmethod
    async def update_state(
        self,
        tenant_id: str,
        task_id: str,
        new_state: TaskState,
        updated_at: datetime,
    ) -> None: ...

    @abstractmethod
    async def complete(
        self,
        tenant_id: str,
        task_id: str,
        *,
        result: TaskResult,
        terminal_state: TaskState,
        updated_at: datetime,
    ) -> None:
        """Atomically write ``state``, ``result_json`` and ``updated_at``.

        ``terminal_state`` must be one of :data:`TERMINAL_TASK_STATES`.
        The write is guarded by ``WHERE state NOT IN terminal_states``
        so a concurrent writer (e.g. redelivered message after a crash
        between graph-finish and ack) cannot overwrite a finished
        result. A guard miss raises :class:`IllegalTaskTransitionError`.
        """

    @abstractmethod
    async def get_result(self, tenant_id: str, task_id: str) -> TaskResult | None:
        """Return the persisted :class:`TaskResult`, or ``None`` if absent."""


class SessionRepository(ABC):
    """Persistence for :class:`Session` aggregates."""

    @abstractmethod
    async def upsert(self, session: Session) -> None: ...

    @abstractmethod
    async def get(self, tenant_id: str, session_id: str) -> Session | None: ...

    @abstractmethod
    async def touch(
        self,
        tenant_id: str,
        session_id: str,
        last_active_at: datetime,
    ) -> None: ...


class OutboxRepository(ABC):
    """Persistence for :class:`OutboxEvent` rows.

    Producers call :meth:`enqueue` from inside the same DB transaction
    as the business state change. The dispatcher uses
    :meth:`claim_pending` / :meth:`mark_dispatched` / :meth:`mark_failed`
    to drive the relay loop.
    """

    @abstractmethod
    async def enqueue(self, event: OutboxEvent) -> None: ...

    @abstractmethod
    async def claim_pending(
        self,
        *,
        batch_size: int,
        now: datetime,
    ) -> list[OutboxEvent]: ...

    @abstractmethod
    async def mark_dispatched(
        self,
        event_id: str,
        *,
        dispatched_at: datetime,
    ) -> None: ...

    @abstractmethod
    async def mark_failed(
        self,
        event_id: str,
        *,
        error: str,
        next_attempt_at: datetime | None,
        terminal: bool,
    ) -> None: ...

    @abstractmethod
    async def get(self, event_id: str) -> OutboxEvent | None: ...

    @abstractmethod
    async def count_by_status(self, tenant_id: str, status: OutboxStatus) -> int: ...


class AuditRepository(AuditSink):
    """Append-only persistence for :class:`AuditEvent`.

    Extends :class:`AuditSink` so producers that only need the write
    capability (decorators, ingress middlewares) can depend on the
    smaller port without pulling the read path.
    """

    @abstractmethod
    async def list_recent(
        self,
        tenant_id: str,
        limit: int = 100,
    ) -> list[AuditEvent]: ...


class CheckpointRepository(ABC):
    """Persistence for :class:`TaskCheckpoint`.

    Checkpoints are append-only and ordered by ``sequence`` per task.
    """

    @abstractmethod
    async def append(self, checkpoint: TaskCheckpoint) -> None: ...

    @abstractmethod
    async def latest(
        self,
        tenant_id: str,
        task_id: str,
    ) -> TaskCheckpoint | None: ...

    @abstractmethod
    async def list_for_task(
        self,
        tenant_id: str,
        task_id: str,
    ) -> list[TaskCheckpoint]: ...
