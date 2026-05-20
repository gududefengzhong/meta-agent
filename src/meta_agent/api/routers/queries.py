"""Audit and LLM-usage query endpoints.

GET /v1/audits                      – list audit events (keyset paged)
GET /v1/usages                      – list LLM usage records (keyset paged)
GET /v1/usages/aggregate            – grouped usage aggregate (no paging)

Tenancy is taken from the bearer-validated :class:`RequestContext`;
the underlying repositories enforce ``check_tenant`` so the handler
never has to repeat the guard.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status

from meta_agent.api.cursor import CursorError, decode_cursor, encode_cursor
from meta_agent.api.deps import (
    get_audit_repo,
    get_llm_usage_repo,
    get_request_ctx,
)
from meta_agent.api.schemas import (
    AuditEventResponse,
    AuditListResponse,
    LLMUsageListResponse,
    LLMUsageResponse,
    UsageAggregateListResponse,
    UsageAggregateResponse,
)
from meta_agent.core.domain.llm_usage import LLMUsageStatus
from meta_agent.core.ports.llm_usage import LLMUsageFilter, UsageGroupBy
from meta_agent.core.ports.repository import AuditFilter
from meta_agent.infra.persistence import PgAuditRepository, PgLLMUsageRepository
from meta_agent.infra.security.context import RequestContext, bind_context

router = APIRouter(tags=["queries"])

# Default look-back window when caller omits ``since`` / ``until``.
# 24h matches the audit-log retention SLA in the spec and keeps the
# default page small enough that the keyset index drives the query.
_DEFAULT_WINDOW = timedelta(hours=24)
_AGGREGATE_DEFAULT_WINDOW = timedelta(days=7)
_MAX_LIMIT = 500


def _resolve_window(
    since: datetime | None,
    until: datetime | None,
    *,
    default_window: timedelta,
) -> tuple[datetime, datetime]:
    """Fill defaults, validate ordering, return ``(since, until)``."""
    now = datetime.now(UTC)
    resolved_until = until if until is not None else now
    resolved_since = since if since is not None else resolved_until - default_window
    if resolved_since >= resolved_until:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'since' must be strictly before 'until'",
        )
    return resolved_since, resolved_until


def _decode_before(cursor: str | None) -> tuple[datetime, str] | None:
    if cursor is None:
        return None
    try:
        return decode_cursor(cursor)
    except CursorError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid cursor: {exc}",
        ) from exc


@router.get(
    "/audits",
    response_model=AuditListResponse,
    summary="List audit events (keyset paginated, DESC by occurred_at)",
)
async def list_audits(
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    action: str | None = Query(default=None, min_length=1),
    task_id: str | None = Query(default=None, min_length=1),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    ctx: RequestContext = Depends(get_request_ctx),
    repo: PgAuditRepository = Depends(get_audit_repo),
) -> AuditListResponse:
    resolved_since, resolved_until = _resolve_window(since, until, default_window=_DEFAULT_WINDOW)
    before = _decode_before(cursor)
    filt = AuditFilter(
        since=resolved_since,
        until=resolved_until,
        action=action,
        task_id=task_id,
        before=before,
        limit=limit,
    )
    with bind_context(ctx):
        events = await repo.list_filtered(ctx.tenant_id, filt)
    items = [
        AuditEventResponse(
            event_id=e.event_id,
            tenant_id=e.tenant_id,
            principal_id=e.principal_id,
            session_id=e.session_id,
            task_id=e.task_id,
            trace_id=e.trace_id,
            action=e.action,
            payload=dict(e.payload),
            occurred_at=e.occurred_at,
        )
        for e in events
    ]
    # Only emit a cursor when the page is full; a short page means we
    # exhausted the window so there is nothing more to fetch.
    next_cursor: str | None = None
    if len(events) == limit:
        last = events[-1]
        next_cursor = encode_cursor(last.occurred_at, last.event_id)
    return AuditListResponse(items=items, next_cursor=next_cursor)


@router.get(
    "/usages",
    response_model=LLMUsageListResponse,
    summary="List LLM usage records (keyset paginated, DESC by created_at)",
)
async def list_usages(
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    model: str | None = Query(default=None, min_length=1),
    task_id: str | None = Query(default=None, min_length=1),
    status_: LLMUsageStatus | None = Query(default=None, alias="status"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    ctx: RequestContext = Depends(get_request_ctx),
    repo: PgLLMUsageRepository = Depends(get_llm_usage_repo),
) -> LLMUsageListResponse:
    resolved_since, resolved_until = _resolve_window(since, until, default_window=_DEFAULT_WINDOW)
    before = _decode_before(cursor)
    filt = LLMUsageFilter(
        since=resolved_since,
        until=resolved_until,
        model=model,
        task_id=task_id,
        status=status_,
        before=before,
        limit=limit,
    )
    with bind_context(ctx):
        records = await repo.list_filtered(ctx.tenant_id, filt)
    items = [
        LLMUsageResponse(
            record_id=r.record_id,
            tenant_id=r.tenant_id,
            trace_id=r.trace_id,
            request_id=r.request_id,
            principal_id=r.principal_id,
            session_id=r.session_id,
            task_id=r.task_id,
            provider=r.provider,
            model=r.model,
            requested_model=r.requested_model,
            prompt_tokens=r.prompt_tokens,
            completion_tokens=r.completion_tokens,
            total_tokens=r.total_tokens,
            finish_reason=r.finish_reason,
            provider_response_id=r.provider_response_id,
            cost_usd_micros=r.cost_usd_micros,
            latency_ms=r.latency_ms,
            status=r.status,
            error_category=r.error_category,
            error_message=r.error_message,
            created_at=r.created_at,
        )
        for r in records
    ]
    next_cursor: str | None = None
    if len(records) == limit:
        last = records[-1]
        next_cursor = encode_cursor(last.created_at, last.record_id)
    return LLMUsageListResponse(items=items, next_cursor=next_cursor)


@router.get(
    "/usages/aggregate",
    response_model=UsageAggregateListResponse,
    summary="Grouped LLM usage aggregate over [since, until)",
)
async def aggregate_usages(
    group_by: UsageGroupBy = Query(...),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    ctx: RequestContext = Depends(get_request_ctx),
    repo: PgLLMUsageRepository = Depends(get_llm_usage_repo),
) -> UsageAggregateListResponse:
    resolved_since, resolved_until = _resolve_window(
        since, until, default_window=_AGGREGATE_DEFAULT_WINDOW
    )
    with bind_context(ctx):
        buckets = await repo.aggregate_grouped(
            ctx.tenant_id,
            resolved_since,
            resolved_until,
            group_by,
        )
    return UsageAggregateListResponse(
        items=[
            UsageAggregateResponse(
                key=b.key,
                tokens=b.tokens,
                cost_usd_micros=b.cost_usd_micros,
                calls=b.calls,
            )
            for b in buckets
        ],
        group_by=group_by,
        since=resolved_since,
        until=resolved_until,
    )
