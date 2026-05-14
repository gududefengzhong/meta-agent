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


class AuditRepository(ABC):
    """Append-only persistence for :class:`AuditEvent`."""

    @abstractmethod
    async def append(self, event: AuditEvent) -> None: ...

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
