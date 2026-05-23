"""Session read endpoints (Phase δ-1 multi-turn).

* ``GET /v1/sessions/{session_id}`` — current session row
* ``GET /v1/sessions/{session_id}/messages`` — reconstructed
  user/assistant message thread across the session's tasks

Sessions are *write-on-task-submit*: there is no explicit
``POST /v1/sessions`` endpoint in v0; the first task submission
that references a ``session_id`` upserts the row. A future PR can
add explicit lifecycle endpoints (rename / close / archive) once
the UX needs them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from meta_agent.api.deps import (
    get_request_ctx,
    get_session_repo,
    get_task_repo,
)
from meta_agent.api.schemas import (
    SessionMessage,
    SessionMessagesResponse,
    SessionResponse,
)
from meta_agent.core.domain.task import TaskState
from meta_agent.infra.persistence import PgSessionRepository, PgTaskRepository
from meta_agent.infra.security.context import RequestContext, bind_context

router = APIRouter(tags=["sessions"])


@router.get(
    "/sessions/{session_id}",
    response_model=SessionResponse,
    summary="Get a session",
)
async def get_session(
    session_id: str,
    ctx: RequestContext = Depends(get_request_ctx),
    session_repo: PgSessionRepository = Depends(get_session_repo),
) -> SessionResponse:
    """Return the session row for the requesting tenant.

    404 when the session does not exist OR belongs to a different
    tenant — the repository's tenant guard already filters by
    ``tenant_id`` so we cannot leak the existence of a
    cross-tenant session.
    """

    with bind_context(ctx):
        session = await session_repo.get(ctx.tenant_id, session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"session {session_id!r} not found",
        )
    return SessionResponse(
        session_id=session.session_id,
        tenant_id=session.tenant_id,
        principal_id=session.principal_id,
        created_at=session.created_at,
        last_active_at=session.last_active_at,
        is_closed=session.is_closed,
    )


@router.get(
    "/sessions/{session_id}/messages",
    response_model=SessionMessagesResponse,
    summary="Get the reconstructed message thread for a session",
)
async def get_session_messages(
    session_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    ctx: RequestContext = Depends(get_request_ctx),
    session_repo: PgSessionRepository = Depends(get_session_repo),
    task_repo: PgTaskRepository = Depends(get_task_repo),
) -> SessionMessagesResponse:
    """Return the (user, assistant) thread derived from session tasks.

    Mirrors what the worker injects into the graph state under
    ``_prior_messages`` — but exposes it via a stable wire shape
    so clients can render the conversation history without
    re-running the agent loop.

    Tasks that never wrote an ``assistant_message`` (failed runs,
    diagnostic-only graphs) are skipped, matching
    :func:`build_prior_messages`.
    """

    with bind_context(ctx):
        session = await session_repo.get(ctx.tenant_id, session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"session {session_id!r} not found",
        )
    with bind_context(ctx):
        tasks = await task_repo.list_by_session(ctx.tenant_id, session_id, limit=limit)
    messages: list[SessionMessage] = []
    for task in tasks:
        if task.state != TaskState.SUCCEEDED:
            continue
        user_prompt = task.input_payload.get("user_prompt")
        if not isinstance(user_prompt, str) or not user_prompt:
            continue
        result = await task_repo.get_result(ctx.tenant_id, task.task_id)
        if result is None or result.output is None:
            continue
        assistant_message = result.output.get("assistant_message")
        if not isinstance(assistant_message, str) or not assistant_message:
            continue
        messages.append(
            SessionMessage(
                role="user",
                content=user_prompt,
                task_id=task.task_id,
                created_at=task.created_at,
            )
        )
        messages.append(
            SessionMessage(
                role="assistant",
                content=assistant_message,
                task_id=task.task_id,
                created_at=task.created_at,
            )
        )
    return SessionMessagesResponse(session_id=session_id, messages=messages)
