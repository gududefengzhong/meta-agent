"""Shared helpers for repository tenant-isolation enforcement.

Every repository write/read goes through :func:`check_tenant` so the
caller cannot accidentally cross tenant boundaries. The L0 contract is
defined in ``docs/specs/CONTEXT_PROPAGATION.md``.
"""

from __future__ import annotations

from meta_agent.core.ports.repository import TenantIsolationError
from meta_agent.infra.security.context import require_tenant_id


def check_tenant(tenant_id: str) -> None:
    """Assert ``tenant_id`` matches the currently bound request context.

    Raises :class:`TenantIsolationError` (PERMISSION category, not
    retryable) if the bound tenant differs from ``tenant_id``. Raises
    :class:`MissingContextError` from
    :func:`require_tenant_id` if no context is bound at all.
    """
    bound = require_tenant_id()
    if bound != tenant_id:
        raise TenantIsolationError(f"tenant mismatch: bound={bound!r} requested={tenant_id!r}")
