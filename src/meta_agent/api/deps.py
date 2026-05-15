"""FastAPI dependency providers for the task API.

Every handler receives its collaborators through these providers.  In
production they resolve from ``app.state`` (populated by the lifespan).
In unit tests each provider is replaced via ``app.dependency_overrides``
so no real Postgres / Redis connection is required.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, Header, HTTPException, Request, status

from meta_agent.infra.persistence import DatabasePool, PgOutboxRepository, PgTaskRepository
from meta_agent.infra.queue import RedisStreamPublisher
from meta_agent.infra.security.context import RequestContext

# ── Infrastructure handles (populated by lifespan) ───────────────────────────


def get_db_pool(request: Request) -> DatabasePool:
    """Return the shared asyncpg pool attached to ``app.state``."""
    return request.app.state.db_pool  # type: ignore[no-any-return]


def get_publisher(request: Request) -> RedisStreamPublisher:
    """Return the shared Redis stream publisher attached to ``app.state``."""
    return request.app.state.publisher  # type: ignore[no-any-return]


def get_task_topic(request: Request) -> str:
    """Return the Redis stream topic for task commands."""
    return request.app.state.task_topic  # type: ignore[no-any-return]


# ── Domain-level collaborators ────────────────────────────────────────────────


def get_task_repo(pool: DatabasePool = Depends(get_db_pool)) -> PgTaskRepository:
    """Construct a :class:`PgTaskRepository` from the shared pool."""
    return PgTaskRepository(pool)


def get_outbox_repo(pool: DatabasePool = Depends(get_db_pool)) -> PgOutboxRepository:
    """Construct a :class:`PgOutboxRepository` from the shared pool."""
    return PgOutboxRepository(pool)


# ── Request context ───────────────────────────────────────────────────────────


def get_request_ctx(
    x_tenant_id: str | None = Header(default=None),
    x_principal_id: str | None = Header(default=None),
    x_trace_id: str | None = Header(default=None),
) -> RequestContext:
    """Build a :class:`RequestContext` from standard request headers.

    ``X-Tenant-Id`` and ``X-Principal-Id`` are mandatory; missing either
    returns HTTP 401 so the caller gets a clear signal rather than a 422
    Pydantic validation error.
    """

    if not x_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Tenant-Id header is required",
        )
    if not x_principal_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Principal-Id header is required",
        )
    trace_id = x_trace_id if x_trace_id else str(uuid.uuid4())
    return RequestContext(
        tenant_id=x_tenant_id,
        principal_id=x_principal_id,
        trace_id=trace_id,
        request_id=str(uuid.uuid4()),
    )
