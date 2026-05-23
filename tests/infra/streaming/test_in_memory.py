"""Unit tests for :class:`InMemoryChunkBroadcaster`.

Covers the four observable behaviours of the pub/sub port:

* publish + subscribe round-trip delivers chunks in order to one subscriber
* multiple subscribers fan out — each receives every published chunk
* a subscriber that closes mid-stream is unregistered cleanly
* tenant isolation: a subscriber bound to tenant A never sees a chunk
  published for tenant B (same task_id, different tenant)
* queue-full eviction drops oldest queued chunk rather than blocking
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest

from meta_agent.core.ports.llm import LLMStreamChunk
from meta_agent.infra.streaming.in_memory import InMemoryChunkBroadcaster


async def _collect_n(
    iterator: AsyncGenerator[LLMStreamChunk, None], n: int
) -> list[LLMStreamChunk]:
    collected: list[LLMStreamChunk] = []
    for _ in range(n):
        collected.append(await iterator.__anext__())
    return collected


async def test_publish_then_subscribe_round_trips_in_order() -> None:
    bus = InMemoryChunkBroadcaster()
    iterator = await bus.subscribe(tenant_id="t-1", task_id="task-1")
    await bus.publish(tenant_id="t-1", task_id="task-1", chunk=LLMStreamChunk(content_delta="hi"))
    await bus.publish(
        tenant_id="t-1", task_id="task-1", chunk=LLMStreamChunk(content_delta="there")
    )
    chunks = await _collect_n(iterator, 2)
    assert [c.content_delta for c in chunks] == ["hi", "there"]
    await iterator.aclose()


async def test_two_subscribers_each_receive_every_chunk() -> None:
    bus = InMemoryChunkBroadcaster()
    sub_a = await bus.subscribe(tenant_id="t-1", task_id="task-1")
    sub_b = await bus.subscribe(tenant_id="t-1", task_id="task-1")
    await bus.publish(tenant_id="t-1", task_id="task-1", chunk=LLMStreamChunk(content_delta="x"))
    a_chunk = await sub_a.__anext__()
    b_chunk = await sub_b.__anext__()
    assert a_chunk.content_delta == "x"
    assert b_chunk.content_delta == "x"
    await sub_a.aclose()
    await sub_b.aclose()


async def test_chunks_published_before_subscribe_are_lost() -> None:
    bus = InMemoryChunkBroadcaster()
    # publish before any subscriber — chunk is dropped, no error
    await bus.publish(tenant_id="t-1", task_id="task-1", chunk=LLMStreamChunk(content_delta="lost"))
    iterator = await bus.subscribe(tenant_id="t-1", task_id="task-1")
    await bus.publish(tenant_id="t-1", task_id="task-1", chunk=LLMStreamChunk(content_delta="kept"))
    chunk = await iterator.__anext__()
    assert chunk.content_delta == "kept"
    await iterator.aclose()


async def test_tenant_isolation_prevents_cross_tenant_delivery() -> None:
    bus = InMemoryChunkBroadcaster()
    sub_a = await bus.subscribe(tenant_id="t-A", task_id="task-1")
    sub_b = await bus.subscribe(tenant_id="t-B", task_id="task-1")
    await bus.publish(
        tenant_id="t-A", task_id="task-1", chunk=LLMStreamChunk(content_delta="a-only")
    )
    a_chunk = await sub_a.__anext__()
    assert a_chunk.content_delta == "a-only"

    # Tenant B's subscriber must not have received tenant A's chunk.
    # Pull with a tiny timeout; if it returns a chunk, that's a leak.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(sub_b.__anext__(), timeout=0.05)

    await sub_a.aclose()
    await sub_b.aclose()


async def test_closing_subscriber_unregisters_queue() -> None:
    bus = InMemoryChunkBroadcaster()
    iterator = await bus.subscribe(tenant_id="t-1", task_id="task-1")
    await iterator.aclose()
    # After close, the channel list should be empty so future publishes
    # have no queues to write to (no-op, no error).
    await bus.publish(
        tenant_id="t-1", task_id="task-1", chunk=LLMStreamChunk(content_delta="ignored")
    )
    # No assertion needed beyond "no exception" — the queue was removed
    # in the finally block of _drain. Confirm internal state explicitly.
    assert bus._channels == {}


async def test_full_queue_evicts_oldest_chunk_instead_of_blocking() -> None:
    bus = InMemoryChunkBroadcaster(queue_maxsize=2)
    iterator = await bus.subscribe(tenant_id="t-1", task_id="task-1")
    # Publish 3 with a slow subscriber — the first should be evicted.
    await bus.publish(tenant_id="t-1", task_id="task-1", chunk=LLMStreamChunk(content_delta="a"))
    await bus.publish(tenant_id="t-1", task_id="task-1", chunk=LLMStreamChunk(content_delta="b"))
    await bus.publish(tenant_id="t-1", task_id="task-1", chunk=LLMStreamChunk(content_delta="c"))
    assert bus.dropped_chunk_count == 1
    chunks = await _collect_n(iterator, 2)
    assert [c.content_delta for c in chunks] == ["b", "c"]
    await iterator.aclose()


async def test_zero_queue_maxsize_rejected() -> None:
    with pytest.raises(ValueError):
        InMemoryChunkBroadcaster(queue_maxsize=0)
