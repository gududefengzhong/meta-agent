"""Unit tests for Session model."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from meta_agent.core.domain import Session


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def test_session_requires_tenant_principal_and_id() -> None:
    session = Session(
        session_id="s-1",
        tenant_id="t-1",
        principal_id="p-1",
        created_at=_now(),
        last_active_at=_now(),
    )
    assert session.is_closed is False


def test_session_rejects_blank_tenant() -> None:
    with pytest.raises(ValidationError):
        Session(
            session_id="s-1",
            tenant_id="",
            principal_id="p-1",
            created_at=_now(),
            last_active_at=_now(),
        )
