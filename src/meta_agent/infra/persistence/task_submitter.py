"""asyncpg-backed :class:`TaskSubmitter` for task chaining.

Writes the follow-up ``tasks`` row and its accompanying
``outbox_events`` row inside the same PG transaction, mirroring the
submit-path pattern in :mod:`meta_agent.api.routers.tasks`. A
``UniqueViolationError`` on either ``(tenant_id, idempotency_key)``
index is treated as the legitimate "this chain step was already
recorded" signal and surfaced as a ``None`` return so the worker can
audit ``task.chain_skipped`` instead of failing the parent run.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import asyncpg

from meta_agent.core.domain.outbox import OutboxEvent
from meta_agent.core.domain.task import Task, TaskState
from meta_agent.core.ports.task_submitter import FollowUpSpec, TaskSubmitter
from meta_agent.infra.persistence.outbox_repo import PgOutboxRepository
from meta_agent.infra.persistence.pool import DatabasePool
from meta_agent.infra.persistence.task_repo import PgTaskRepository


class PgTaskSubmitter(TaskSubmitter):
    """Atomically persists a chained task + outbox event."""

    def __init__(
        self,
        pool: DatabasePool,
        task_repo: PgTaskRepository,
        outbox_repo: PgOutboxRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._pool = pool
        self._tasks = task_repo
        self._outbox = outbox_repo
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))

    async def submit_follow_up(
        self,
        parent: Task,
        follow_up: FollowUpSpec,
    ) -> Task | None:
        now = self._clock()
        child_task_id = self._id_factory()
        # The child inherits the parent's request-context dimensions
        # (tenant, session, principal, trace) so audit / billing
        # joins across the chain stay coherent.
        task = Task(
            task_id=child_task_id,
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
        event = OutboxEvent(
            event_id=self._id_factory(),
            tenant_id=parent.tenant_id,
            trace_id=parent.trace_id,
            aggregate_type="task",
            aggregate_id=child_task_id,
            topic=follow_up.topic,
            payload=dict(follow_up.input_payload),
            idempotency_key=follow_up.idempotency_key,
            created_at=now,
        )
        try:
            async with self._pool.transaction() as conn:
                await self._tasks.upsert_in_conn(task, conn)
                await self._outbox.enqueue_in_conn(event, conn)
        except asyncpg.UniqueViolationError:
            # Either ``uq_tasks_tenant_idem`` or ``uq_outbox_tenant_idem``
            # tripped: a prior redelivery already wrote this chain step.
            # The existing row is authoritative; surface the no-op so
            # the caller can audit it without raising.
            return None
        return task
