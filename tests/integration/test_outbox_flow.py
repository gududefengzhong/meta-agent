"""End-to-end outbox → dispatcher → Redis stream → consumer flow.

This is the headline integration test for milestone 0.3: it exercises
the full Transactional Outbox path on real Postgres + Redis.

Flow:
1. Producer ``enqueue`` writes an OutboxEvent row.
2. :class:`OutboxDispatcher` claims the row and publishes via
   :class:`RedisStreamPublisher` (XADD).
3. :class:`RedisStreamConsumer` reads via XREADGROUP, invokes the
   handler, and XACKs on success.
4. Dispatcher marks the outbox row dispatched.

Assertion targets:
- Handler receives the original envelope with intact tenant context.
- Outbox row transitions PENDING → DISPATCHED.
- Redis PEL is empty after ack (no stuck messages).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis

from meta_agent.core.domain.outbox import OutboxEvent, OutboxStatus
from meta_agent.core.ports.message import MessageEnvelope
from meta_agent.infra.persistence import (
    DatabasePool,
    OutboxDispatcher,
    PgOutboxRepository,
)
from meta_agent.infra.queue import (
    RedisStreamConsumer,
    RedisStreamPublisher,
    stream_name_for_topic,
)
from meta_agent.infra.security.context import RequestContext, bind_context

pytestmark = pytest.mark.integration


async def test_outbox_flow_end_to_end(db_pool: DatabasePool, redis_client: Redis) -> None:
    topic = "task.events"
    outbox_repo = PgOutboxRepository(db_pool)
    publisher = RedisStreamPublisher(redis_client)
    dispatcher = OutboxDispatcher(outbox_repo, publisher)
    consumer = RedisStreamConsumer(
        redis_client,
        topic=topic,
        group="workers",
        consumer_name="worker-1",
        batch_size=8,
        block_ms=200,
    )

    delivered: list[MessageEnvelope] = []
    delivered_event = asyncio.Event()

    async def handler(envelope: MessageEnvelope) -> None:
        delivered.append(envelope)
        delivered_event.set()

    now = datetime(2026, 5, 14, tzinfo=UTC)
    event = OutboxEvent(
        event_id="e-flow-1",
        tenant_id="tenant-A",
        trace_id="trace-flow-1",
        aggregate_type="task",
        aggregate_id="t-1",
        topic=topic,
        payload={"goal": "test"},
        idempotency_key="idem-e-flow-1",
        created_at=now,
    )

    ctx = RequestContext(
        tenant_id="tenant-A",
        principal_id="user-1",
        trace_id="trace-flow-1",
        request_id="req-1",
    )
    with bind_context(ctx):
        await outbox_repo.enqueue(event)

    consumer_task = asyncio.create_task(consumer.start(handler))
    try:
        drained = await dispatcher.run_once()
        assert drained == 1
        await asyncio.wait_for(delivered_event.wait(), timeout=5.0)
    finally:
        await consumer.stop()
        await asyncio.wait_for(consumer_task, timeout=5.0)

    assert len(delivered) == 1
    envelope = delivered[0]
    assert envelope.message_id == "e-flow-1"
    assert envelope.tenant_id == "tenant-A"
    assert envelope.topic == topic
    assert envelope.payload == {"goal": "test"}

    fetched = await outbox_repo.get("e-flow-1")
    assert fetched is not None
    assert fetched.status is OutboxStatus.DISPATCHED

    # PEL must be empty: handler acked successfully.
    pending = await redis_client.xpending(stream_name_for_topic(topic), "workers")
    assert pending["pending"] == 0
