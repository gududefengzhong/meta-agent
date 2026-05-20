"""FastAPI dependency providers for the task API.

Every handler receives its collaborators through these providers.  In
production they resolve from ``app.state`` (populated by the lifespan).
In unit tests each provider is replaced via ``app.dependency_overrides``
so no real Postgres / Redis connection is required.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import Depends, Header, HTTPException, Request, status

from meta_agent.core.ports.auth import AuthBackendError, TokenValidator
from meta_agent.infra.persistence import (
    DatabasePool,
    PgAuditRepository,
    PgLLMUsageRepository,
    PgOutboxRepository,
    PgTaskRepository,
)
from meta_agent.infra.queue import RedisStreamPublisher
from meta_agent.infra.security.context import RequestContext

logger = logging.getLogger(__name__)

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


def get_token_validator(request: Request) -> TokenValidator:
    """Return the shared :class:`TokenValidator` attached to ``app.state``."""
    return request.app.state.token_validator  # type: ignore[no-any-return]


# ── Domain-level collaborators ────────────────────────────────────────────────


def get_task_repo(pool: DatabasePool = Depends(get_db_pool)) -> PgTaskRepository:
    """Construct a :class:`PgTaskRepository` from the shared pool."""
    return PgTaskRepository(pool)


def get_outbox_repo(pool: DatabasePool = Depends(get_db_pool)) -> PgOutboxRepository:
    """Construct a :class:`PgOutboxRepository` from the shared pool."""
    return PgOutboxRepository(pool)


def get_audit_repo(pool: DatabasePool = Depends(get_db_pool)) -> PgAuditRepository:
    """Construct a :class:`PgAuditRepository` from the shared pool."""
    return PgAuditRepository(pool)


def get_llm_usage_repo(pool: DatabasePool = Depends(get_db_pool)) -> PgLLMUsageRepository:
    """Construct a :class:`PgLLMUsageRepository` from the shared pool."""
    return PgLLMUsageRepository(pool)


# ── Request context ───────────────────────────────────────────────────────────


_INVALID_TOKEN = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="invalid or missing bearer token",
    headers={"WWW-Authenticate": "Bearer"},
)


def _parse_bearer(authorization: str | None) -> str | None:
    """Return the bearer token from ``Authorization`` or ``None`` if absent."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


async def get_request_ctx(
    authorization: str | None = Header(default=None),
    x_trace_id: str | None = Header(default=None),
    validator: TokenValidator = Depends(get_token_validator),
) -> RequestContext:
    """Validate ``Authorization: Bearer`` and build a :class:`RequestContext`.

    ``tenant_id`` and ``principal_id`` are taken from the validated
    :class:`Principal`; any ``X-Tenant-Id`` / ``X-Principal-Id`` headers
    the client may send are ignored — header-derived tenancy would let
    any caller spoof a tenant. ``X-Trace-Id`` is still honoured because
    tracing is not a security boundary.
    """
    token = _parse_bearer(authorization)
    if token is None:
        raise _INVALID_TOKEN
    try:
        principal = await validator.validate(token)
    except AuthBackendError:
        logger.exception("auth.backend_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication backend unavailable",
        ) from None
    if principal is None:
        raise _INVALID_TOKEN
    trace_id = x_trace_id if x_trace_id else str(uuid.uuid4())
    return RequestContext(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        trace_id=trace_id,
        request_id=str(uuid.uuid4()),
    )
