"""Unit tests for queue envelope (de)serialization."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from meta_agent.core.ports.message import MessageEnvelope
from meta_agent.infra.queue._serde import envelope_to_fields, fields_to_envelope


def _envelope() -> MessageEnvelope:
    return MessageEnvelope(
        message_id="m-1",
        topic="task.events",
        tenant_id="tenant-1",
        trace_id="trace-1",
        idempotency_key="idem-1",
        principal_id="user-1",
        session_id="sess-1",
        task_id="task-1",
        request_id="req-1",
        aggregate_type="task",
        aggregate_id="task-1",
        event_type="task.submitted",
        payload={"foo": "bar"},
        attempts=0,
        occurred_at=datetime(2026, 5, 14, tzinfo=UTC),
        enqueued_at=datetime(2026, 5, 14, tzinfo=UTC),
    )


def test_envelope_roundtrip_with_str_fields() -> None:
    envelope = _envelope()
    fields = envelope_to_fields(envelope)
    decoded = fields_to_envelope(dict(fields))  # type: ignore[arg-type]
    assert decoded == envelope


def test_envelope_roundtrip_with_bytes_fields() -> None:
    envelope = _envelope()
    fields = envelope_to_fields(envelope)
    encoded_bytes: dict[bytes | str, bytes | str] = {
        (k.encode() if isinstance(k, str) else k): (v.encode() if isinstance(v, str) else v)
        for k, v in fields.items()
        if isinstance(k, (str, bytes)) and isinstance(v, (str, bytes))
    }
    assert fields_to_envelope(encoded_bytes) == envelope


def test_fields_to_envelope_rejects_missing_payload_field() -> None:
    with pytest.raises(ValueError, match="missing"):
        fields_to_envelope({"other": "data"})
