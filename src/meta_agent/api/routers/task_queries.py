"""Query-side task endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from meta_agent.api.deps import (
    get_audit_repo,
    get_llm_usage_repo,
    get_request_ctx,
    get_task_repo,
    get_trajectory_repo,
)
from meta_agent.api.mappers.tasks import to_task_response, to_task_result_response
from meta_agent.api.schemas import (
    TaskObservabilitySummaryResponse,
    TaskResponse,
    TaskResultResponse,
    TrajectoryResponse,
)
from meta_agent.api.services.task_observability import build_task_observability
from meta_agent.core.domain.task_observability import summary_to_json_dict
from meta_agent.infra.persistence import (
    PgAuditRepository,
    PgLLMUsageRepository,
    PgTaskRepository,
    PgTrajectoryRepository,
)
from meta_agent.infra.security.context import RequestContext, bind_context

router = APIRouter(tags=["tasks"])


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
    return to_task_response(task)


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
    return to_task_result_response(result)


@router.get(
    "/tasks/{task_id}/observability",
    response_model=TaskObservabilitySummaryResponse,
    summary="Get task-level observability and evaluation summary",
)
async def get_task_observability_summary(
    task_id: str,
    ctx: RequestContext = Depends(get_request_ctx),
    task_repo: PgTaskRepository = Depends(get_task_repo),
    audit_repo: PgAuditRepository = Depends(get_audit_repo),
    llm_usage_repo: PgLLMUsageRepository = Depends(get_llm_usage_repo),
) -> TaskObservabilitySummaryResponse:
    with bind_context(ctx):
        task = await task_repo.get(ctx.tenant_id, task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"task {task_id!r} not found",
            )
        summary = await build_task_observability(
            tenant_id=ctx.tenant_id,
            task=task,
            tasks=task_repo,
            audits=audit_repo,
            llm_usage=llm_usage_repo,
        )
    return TaskObservabilitySummaryResponse.model_validate(summary_to_json_dict(summary))


@router.get(
    "/tasks/{task_id}/trajectory",
    response_model=TrajectoryResponse,
    summary="Get the merged audit + checkpoint + LLM-usage timeline for one task",
)
async def get_task_trajectory(
    task_id: str,
    limit_per_source: int = Query(default=1000, ge=1, le=1000),
    ctx: RequestContext = Depends(get_request_ctx),
    task_repo: PgTaskRepository = Depends(get_task_repo),
    trajectory_repo: PgTrajectoryRepository = Depends(get_trajectory_repo),
) -> TrajectoryResponse:
    with bind_context(ctx):
        task = await task_repo.get(ctx.tenant_id, task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"task {task_id!r} not found",
            )
        page = await trajectory_repo.list_for_task(
            ctx.tenant_id,
            task_id,
            limit_per_source=limit_per_source,
        )
    return TrajectoryResponse(
        items=[item.model_dump(mode="json") for item in page.items],
        truncated=page.truncated,
    )
