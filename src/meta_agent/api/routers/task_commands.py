"""Command-side task endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from meta_agent.api.deps import (
    get_approval_gateway,
    get_db_pool,
    get_outbox_repo,
    get_request_ctx,
    get_session_repo,
    get_task_repo,
    get_task_topic,
)
from meta_agent.api.mappers.tasks import to_task_response
from meta_agent.api.schemas import AbortRequest, ApprovalRequest, SubmitTaskRequest, TaskResponse
from meta_agent.api.services.task_submission import submit_task_transaction
from meta_agent.infra.persistence import (
    DatabasePool,
    PgOutboxRepository,
    PgSessionRepository,
    PgTaskRepository,
)
from meta_agent.infra.persistence.approval_gateway import (
    TaskApprovalGateway,
    TaskNotAwaitingApprovalError,
)
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
    session_repo: PgSessionRepository = Depends(get_session_repo),
    topic: str = Depends(get_task_topic),
) -> TaskResponse:
    task = await submit_task_transaction(
        body=body,
        ctx=ctx,
        pool=pool,
        task_repo=task_repo,
        outbox_repo=outbox_repo,
        session_repo=session_repo,
        topic=topic,
    )
    return to_task_response(task)


@router.post(
    "/tasks/{task_id}/approve",
    response_model=TaskResponse,
    summary="Approve a task paused at a human_gate",
)
async def approve_task(
    task_id: str,
    body: ApprovalRequest,
    ctx: RequestContext = Depends(get_request_ctx),
    gateway: TaskApprovalGateway = Depends(get_approval_gateway),
) -> TaskResponse:
    with bind_context(ctx):
        try:
            task = await gateway.approve(ctx.tenant_id, task_id, feedback=body.feedback)
        except TaskNotAwaitingApprovalError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
    return to_task_response(task)


@router.post(
    "/tasks/{task_id}/abort",
    response_model=TaskResponse,
    summary="Abort a task paused at a human_gate",
)
async def abort_task(
    task_id: str,
    body: AbortRequest,
    ctx: RequestContext = Depends(get_request_ctx),
    gateway: TaskApprovalGateway = Depends(get_approval_gateway),
) -> TaskResponse:
    with bind_context(ctx):
        try:
            task = await gateway.abort(ctx.tenant_id, task_id, reason=body.reason)
        except TaskNotAwaitingApprovalError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
    return to_task_response(task)
