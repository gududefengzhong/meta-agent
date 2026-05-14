"""Unit tests for OutboxEvent model."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from meta_agent.core.domain import OutboxEvent, OutboxStatus


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def _outbox(**overrides: object) -> OutboxEvent:
    base: dict[str, object] = {
        "event_id": "oe-1",
        "tenant_id": "t-1",
        "trace_id": "trace-1",
        "aggregate_type": "Task",
        "aggregate_id": "task-1",
        "topic": "tasks.events",
        "payload": {"state": "succeeded"},
        "idempotency_key": "task-1:succeeded",
        "created_at": _now(),
    }
    base.update(overrides)
    return OutboxEvent(**base)


def test_outbox_default_status_is_pending() -> None:
    event = _outbox()
    assert event.status is OutboxStatus.PENDING
    assert event.attempts == 0
    assert event.dispatched_at is None


def test_outbox_requires_idempotency_key() -> None:
    with pytest.raises(ValidationError):
        _outbox(idempotency_key="")


def test_outbox_rejects_negative_attempts() -> None:
    with pytest.raises(ValidationError):
        _outbox(attempts=-1)
