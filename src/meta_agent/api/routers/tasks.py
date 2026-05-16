"""Task submission and query endpoints.

POST   /v1/tasks               – submit a new task, returns 201 + TaskResponse
GET    /v1/tasks/{task_id}     – get current task state, returns TaskResponse
GET    /v1/tasks/{task_id}/result – get completed result, returns TaskResultResponse

The handler binds the per-request :class:`RequestContext` around every
repository call so the multi-tenant isolation guard (``check_tenant``)
in the persistence layer is always satisfied.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status

from meta_agent.api.deps import (
    get_db_pool,
    get_outbox_repo,
    get_request_ctx,
    get_task_repo,
    get_task_topic,
)
from meta_agent.api.schemas import SubmitTaskRequest, TaskResponse, TaskResultResponse
from meta_agent.core.domain.outbox import OutboxEvent
from meta_agent.core.domain.task import Task, TaskState
from meta_agent.infra.persistence import DatabasePool, PgOutboxRepository, PgTaskRepository
from meta_agent.infra.security.context import RequestContext, bind_context

router = APIRouter(tags=["tasks"])


@router.post(
    "/tasks",
    status_code=status.HTTP_201_CREATED,
    response_model=TaskResponse,
    summary="Submit a new task",
)
async def submit_task(
    body: SubmitTaskRequest,
    ctx: RequestContext = Depends(get_request_ctx),
    pool: DatabasePool = Depends(get_db_pool),
    task_repo: PgTaskRepository = Depends(get_task_repo),
    outbox_repo: PgOutboxRepository = Depends(get_outbox_repo),
    topic: str = Depends(get_task_topic),
) -> TaskResponse:
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
        input_payload=body.input_payload,
        created_at=now,
        updated_at=now,
    )
    # The outbox row carries the command that the dispatcher will relay
    # to the queue. Writing it in the same PG transaction as the task
    # row is what makes submit atomic and closes the dual-write gap.
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
    with bind_context(ctx):
        async with pool.transaction() as conn:
            await task_repo.upsert_in_conn(task, conn)
            await outbox_repo.enqueue_in_conn(event, conn)

    return TaskResponse(
        task_id=task.task_id,
        tenant_id=task.tenant_id,
        state=task.state,
        task_type=task.task_type,
        trace_id=task.trace_id,
        session_id=task.session_id,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@router.get(
    "/tasks/{task_id}",
    response_model=TaskResponse,
    summary="Get task state",
)
async def get_task(
    task_id: str,
    ctx: RequestContext = Depends(get_request_ctx),
    task_repo: PgTaskRepository = Depends(get_task_repo),
) -> TaskResponse:
    with bind_context(ctx):
        task = await task_repo.get(ctx.tenant_id, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task {task_id!r} not found",
        )
    return TaskResponse(
        task_id=task.task_id,
        tenant_id=task.tenant_id,
        state=task.state,
        task_type=task.task_type,
        trace_id=task.trace_id,
        session_id=task.session_id,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@router.get(
    "/tasks/{task_id}/result",
    response_model=TaskResultResponse,
    summary="Get task result",
)
async def get_task_result(
    task_id: str,
    ctx: RequestContext = Depends(get_request_ctx),
    task_repo: PgTaskRepository = Depends(get_task_repo),
) -> TaskResultResponse:
    with bind_context(ctx):
        result = await task_repo.get_result(ctx.tenant_id, task_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"result for task {task_id!r} not yet available",
        )
    return TaskResultResponse(
        task_id=result.task_id,
        status=result.status,
        graph_id=result.graph_id,
        output=result.output,
        error=result.error,
        node_sequence=result.node_sequence,
        started_at=result.started_at,
        finished_at=result.finished_at,
    )
