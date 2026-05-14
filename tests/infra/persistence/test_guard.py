"""Unit tests for the repository tenant guard."""

from __future__ import annotations

import pytest

from meta_agent.core.domain.errors import ErrorCategory
from meta_agent.core.ports.repository import TenantIsolationError
from meta_agent.infra.persistence._guard import check_tenant
from meta_agent.infra.security.context import (
    MissingContextError,
    RequestContext,
    bind_context,
)


def _ctx(tenant_id: str) -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id,
        principal_id="user-1",
        trace_id="trace-1",
        request_id="req-1",
    )


def test_check_tenant_passes_when_bound_tenant_matches() -> None:
    with bind_context(_ctx("tenant-A")):
        check_tenant("tenant-A")


def test_check_tenant_raises_on_mismatch() -> None:
    with bind_context(_ctx("tenant-A")), pytest.raises(TenantIsolationError) as exc:
        check_tenant("tenant-B")
    assert exc.value.category is ErrorCategory.PERMISSION
    assert exc.value.retryable is False


def test_check_tenant_raises_when_no_context_bound() -> None:
    with pytest.raises(MissingContextError):
        check_tenant("tenant-A")
