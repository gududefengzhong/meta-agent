"""Submission service for task-creation API flows."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from meta_agent.api.schemas import SubmitTaskRequest
from meta_agent.core.domain.outbox import OutboxEvent
from meta_agent.core.domain.session import Session
from meta_agent.core.domain.task import Task, TaskState
from meta_agent.infra.persistence import (
    DatabasePool,
    PgOutboxRepository,
    PgSessionRepository,
    PgTaskRepository,
)
from meta_agent.infra.security.context import RequestContext, bind_context


async def submit_task_transaction(
    *,
    body: SubmitTaskRequest,
    ctx: RequestContext,
    pool: DatabasePool,
    task_repo: PgTaskRepository,
    outbox_repo: PgOutboxRepository,
    session_repo: PgSessionRepository,
    topic: str,
) -> Task:
    """Atomically persist the task row and matching outbox command."""

    task_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    task = Task(
        task_id=task_id,
        tenant_id=ctx.tenant_id,
        principal_id=ctx.principal_id,
        trace_id=ctx.trace_id,
        session_id=body.session_id,
        idempotency_key=body.idempotency_key,
        task_type=body.task_type,
        graph_id=body.graph_id,
        state=TaskState.PENDING,
        permission_mode=body.permission_mode,
        budget_policy=body.budget_policy,
        budget_threshold_micros=body.budget_threshold_micros,
        input_payload=body.input_payload,
        created_at=now,
        updated_at=now,
    )
    event = OutboxEvent(
        event_id=str(uuid.uuid4()),
        tenant_id=ctx.tenant_id,
        trace_id=ctx.trace_id,
        aggregate_type="task",
        aggregate_id=task_id,
        topic=topic,
        payload=dict(body.input_payload),
        idempotency_key=body.idempotency_key or task_id,
        created_at=now,
    )
    session_row: Session | None = None
    if body.session_id:
        session_row = Session(
            session_id=body.session_id,
            tenant_id=ctx.tenant_id,
            principal_id=ctx.principal_id,
            created_at=now,
            last_active_at=now,
        )

    with bind_context(ctx):
        async with pool.transaction() as conn:
            if session_row is not None:
                await session_repo.upsert_in_conn(session_row, conn)
            await task_repo.upsert_in_conn(task, conn)
            await outbox_repo.enqueue_in_conn(event, conn)
    return task
