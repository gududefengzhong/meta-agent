"""Message envelope shared by the queue and outbox layers.

The envelope is the single in-flight unit on the queue. It carries the
full :class:`RequestContext` so downstream consumers can rebind context
without having to pull it from the payload. It is intentionally a
Pydantic model (not a dataclass) because it crosses process boundaries
as JSON and benefits from validation on the consumer side.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MessageEnvelope(BaseModel):
    """An envelope wrapping a single payload destined for a topic.

    ``context`` mirrors the IDs defined in
    ``docs/specs/CONTEXT_PROPAGATION.md`` §1. Producers must populate at
    least ``tenant_id``, ``trace_id`` and ``idempotency_key`` so the
    consumer side can isolate, trace and deduplicate the message.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(..., min_length=1)
    topic: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)
    principal_id: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    request_id: str | None = None
    aggregate_type: str | None = None
    aggregate_id: str | None = None
    event_type: str = Field(..., min_length=1)
    payload: dict[str, object]
    attempts: int = Field(default=0, ge=0)
    occurred_at: datetime
    enqueued_at: datetime


MessageHandler = Callable[[MessageEnvelope], Awaitable[None]]
"""Async callable invoked per delivered message.

Returning normally signals the consumer to ``ack`` the message.
Raising signals the consumer to ``nack`` so the broker can redeliver
according to its retry policy (or, in Redis Streams, to leave the
message in the PEL until the dispatcher retries it).
"""
