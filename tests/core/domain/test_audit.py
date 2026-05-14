"""Unit tests for AuditEvent model."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from meta_agent.core.domain import AuditEvent


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def test_audit_event_minimal_payload() -> None:
    event = AuditEvent(
        event_id="ae-1",
        tenant_id="t-1",
        principal_id="p-1",
        trace_id="trace-1",
        action="task.submitted",
        occurred_at=_now(),
    )
    assert event.payload == {}
    assert event.task_id is None
    assert event.session_id is None


def test_audit_event_requires_action() -> None:
    with pytest.raises(ValidationError):
        AuditEvent(
            event_id="ae-1",
            tenant_id="t-1",
            principal_id="p-1",
            trace_id="trace-1",
            action="",
            occurred_at=_now(),
        )
