"""Redis pub/sub :class:`ChunkBroadcaster` backend.

Wire format
===========
Each :class:`LLMStreamChunk` is published as its
``model_dump_json`` bytes on the channel
``llm-stream:{tenant_id}:{task_id}``. Subscribers decode each
message back into a typed :class:`LLMStreamChunk` via Pydantic
``model_validate_json``.

Pub/sub (not Streams) is intentional: chunks are UX, not state.
A subscriber that connects after the producer has finished sees
nothing — that's correct semantics, the client should reconnect to
the audit / trajectory stream for any state that matters. Using
Streams would persist chunks and force a TTL story we do not need.

Connection lifecycle
====================
The broadcaster does not own the :class:`Redis` client — construct
it externally so the same connection pool serves the message queue,
rate limiter, circuit breaker, and broadcaster. ``close()`` does
not close the shared client, only any per-subscriber pubsub handle
created internally.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from meta_agent.core.ports.chunk_broadcaster import (
    ChunkBroadcaster,
    ChunkBroadcasterError,
)
from meta_agent.core.ports.llm import LLMStreamChunk

logger = logging.getLogger(__name__)

_DEFAULT_CHANNEL_PREFIX = "llm-stream"


class RedisChunkBroadcaster(ChunkBroadcaster):
    """Pub/sub-backed chunk fanout across worker + API processes."""

    def __init__(
        self,
        client: Redis,
        *,
        channel_prefix: str = _DEFAULT_CHANNEL_PREFIX,
    ) -> None:
        if not channel_prefix:
            raise ValueError("channel_prefix must be a non-empty string")
        self._client = client
        self._prefix = channel_prefix

    async def publish(self, *, tenant_id: str, task_id: str, chunk: LLMStreamChunk) -> None:
        channel = self._channel(tenant_id, task_id)
        payload = chunk.model_dump_json()
        try:
            await self._client.publish(channel, payload)
        except RedisError as exc:
            raise ChunkBroadcasterError(f"redis publish failed for channel={channel!r}") from exc

    async def subscribe(
        self, *, tenant_id: str, task_id: str
    ) -> AsyncGenerator[LLMStreamChunk, None]:
        channel = self._channel(tenant_id, task_id)
        pubsub: Any = self._client.pubsub()
        try:
            await pubsub.subscribe(channel)
        except RedisError as exc:
            await _safe_close_pubsub(pubsub)
            raise ChunkBroadcasterError(f"redis subscribe failed for channel={channel!r}") from exc
        return self._drain(pubsub, channel)

    async def _drain(self, pubsub: Any, channel: str) -> AsyncGenerator[LLMStreamChunk, None]:
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    try:
                        payload = data.decode("utf-8")
                    except UnicodeDecodeError:
                        logger.warning(
                            "chunk_broadcaster.invalid_utf8",
                            extra={"channel": channel, "bytes": len(data)},
                        )
                        continue
                elif isinstance(data, str):
                    payload = data
                else:
                    logger.warning(
                        "chunk_broadcaster.unexpected_data_type",
                        extra={"channel": channel, "type": type(data).__name__},
                    )
                    continue
                try:
                    yield LLMStreamChunk.model_validate_json(payload)
                except ValidationError as exc:
                    logger.warning(
                        "chunk_broadcaster.invalid_chunk",
                        extra={"channel": channel, "error": str(exc)[:200]},
                    )
                    continue
        finally:
            await _safe_close_pubsub(pubsub)

    async def close(self) -> None:
        # The shared :class:`Redis` client is owned by the lifespan
        # that created it; the broadcaster only holds a reference.
        # No per-instance resources to release.
        return None

    def _channel(self, tenant_id: str, task_id: str) -> str:
        return f"{self._prefix}:{tenant_id}:{task_id}"


async def _safe_close_pubsub(pubsub: Any) -> None:
    """Close a pubsub handle without propagating shutdown-time errors."""
    try:
        await pubsub.unsubscribe()
    except RedisError as exc:
        logger.debug(
            "chunk_broadcaster.unsubscribe_error",
            extra={"error_type": type(exc).__name__},
        )
    try:
        await pubsub.aclose()
    except (RedisError, AttributeError) as exc:
        logger.debug(
            "chunk_broadcaster.pubsub_close_error",
            extra={"error_type": type(exc).__name__},
        )
