"""Transactional outbox event model.

Cross-service consistency follows the Transactional Outbox pattern per
``docs/specs/AGENT_SPEC.md`` §架构原则 and the L0 distributed-consistency
constraint. Producers write the outbox row in the same DB transaction
as the business state change; a dispatcher relays it to the message
queue with at-least-once semantics and consumer-side idempotency.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class OutboxStatus(StrEnum):
    """Lifecycle status of an outbox entry."""

    PENDING = "pending"
    DISPATCHED = "dispatched"
    FAILED = "failed"


class OutboxEvent(BaseModel):
    """A pending message to be relayed from DB to the message queue.

    ``aggregate_type`` and ``aggregate_id`` allow downstream consumers
    to correlate events to their source aggregate. ``idempotency_key``
    is required and is used by consumers to deduplicate redeliveries.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    aggregate_type: str = Field(..., min_length=1)
    aggregate_id: str = Field(..., min_length=1)
    topic: str = Field(..., min_length=1, description="Logical queue topic")
    payload: dict[str, object]
    idempotency_key: str = Field(..., min_length=1)
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    created_at: datetime
    dispatched_at: datetime | None = None
