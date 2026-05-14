"""Unit tests for Tenant model."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from meta_agent.core.domain import Tenant


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def test_tenant_accepts_valid_payload() -> None:
    tenant = Tenant(tenant_id="t-1", display_name="Acme", created_at=_now())
    assert tenant.tenant_id == "t-1"
    assert tenant.is_active is True


def test_tenant_rejects_empty_id() -> None:
    with pytest.raises(ValidationError):
        Tenant(tenant_id="", display_name="Acme", created_at=_now())


def test_tenant_is_frozen() -> None:
    tenant = Tenant(tenant_id="t-1", display_name="Acme", created_at=_now())
    with pytest.raises(ValidationError):
        tenant.display_name = "Other"  # type: ignore[misc]


def test_tenant_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Tenant(
            tenant_id="t-1",
            display_name="Acme",
            created_at=_now(),
            unknown="x",  # type: ignore[call-arg]
        )
