"""End-to-end :class:`RedisChunkBroadcaster` against a real Redis.

The unit tests cover the in-memory backend's pub/sub semantics; this
test runs the actual Redis client + Lua-free pub/sub round-trip so
any drift between the broadcaster's wire encoding and the listener's
JSON decode is caught immediately.
"""

from __future__ import annotations

import asyncio

import pytest
from redis.asyncio import Redis

from meta_agent.core.ports.llm import LLMStreamChunk, LLMUsage, ToolCallDelta
from meta_agent.infra.streaming.redis_broadcaster import RedisChunkBroadcaster


async def _drain_async(subscription, n: int):  # type: ignore[no-untyped-def]
    out: list[LLMStreamChunk] = []
    for _ in range(n):
        out.append(await subscription.__anext__())
    return out


async def test_publish_then_subscribe_round_trips_through_redis(
    redis_client: Redis,
) -> None:
    bus = RedisChunkBroadcaster(redis_client, channel_prefix="test-llm-stream")
    iterator = await bus.subscribe(tenant_id="t-1", task_id="task-1")

    chunks_in = [
        LLMStreamChunk(content_delta="he"),
        LLMStreamChunk(
            tool_call_deltas=(
                ToolCallDelta(index=0, id="call-1", name="fs_read", arguments_delta='{"p'),
            )
        ),
        LLMStreamChunk(
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=3, completion_tokens=5, total_tokens=8),
            model="deepseek/deepseek-chat",
            provider_response_id="resp-1",
        ),
    ]

    async def producer() -> None:
        # Give the subscriber a beat to register on the Redis channel.
        await asyncio.sleep(0.05)
        for chunk in chunks_in:
            await bus.publish(tenant_id="t-1", task_id="task-1", chunk=chunk)

    producer_task = asyncio.create_task(producer())
    try:
        received = await asyncio.wait_for(_drain_async(iterator, 3), timeout=2.0)
    finally:
        await producer_task
        await iterator.aclose()
        await bus.close()

    # Wire encoding is symmetric — what publish emitted, subscribe parsed.
    assert received[0].content_delta == "he"
    assert received[1].tool_call_deltas[0].id == "call-1"
    assert received[1].tool_call_deltas[0].arguments_delta == '{"p'
    assert received[2].finish_reason == "stop"
    assert received[2].usage is not None
    assert received[2].usage.total_tokens == 8
    assert received[2].model == "deepseek/deepseek-chat"


async def test_tenant_isolation_on_redis_channels(redis_client: Redis) -> None:
    bus = RedisChunkBroadcaster(redis_client, channel_prefix="test-llm-stream")
    sub_a = await bus.subscribe(tenant_id="t-A", task_id="task-1")
    sub_b = await bus.subscribe(tenant_id="t-B", task_id="task-1")
    try:
        await asyncio.sleep(0.05)
        await bus.publish(
            tenant_id="t-A",
            task_id="task-1",
            chunk=LLMStreamChunk(content_delta="a-only"),
        )
        a_chunk = await asyncio.wait_for(sub_a.__anext__(), timeout=1.0)
        assert a_chunk.content_delta == "a-only"
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(sub_b.__anext__(), timeout=0.2)
    finally:
        await sub_a.aclose()
        await sub_b.aclose()
        await bus.close()
