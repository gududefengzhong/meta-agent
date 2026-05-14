"""Redis Streams implementation of :class:`MessagePublisher`."""

from __future__ import annotations

from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from meta_agent.core.ports.message import MessageEnvelope
from meta_agent.core.ports.queue import MessagePublisher, QueueError
from meta_agent.infra.queue._serde import envelope_to_fields
from meta_agent.infra.queue.topic import (
    DEFAULT_STREAM_PREFIX,
    stream_name_for_topic,
)


class RedisStreamPublisher(MessagePublisher):
    """Publishes envelopes to a Redis stream via ``XADD``.

    The publisher does not own the :class:`Redis` client lifecycle;
    construct it externally so multiple adapters (publisher, consumer,
    rate limiter, etc.) can share one connection pool.
    """

    def __init__(
        self,
        client: Redis,
        *,
        stream_prefix: str = DEFAULT_STREAM_PREFIX,
        max_len: int | None = None,
    ) -> None:
        self._client = client
        self._stream_prefix = stream_prefix
        self._max_len = max_len

    async def publish(self, envelope: MessageEnvelope) -> None:
        stream = stream_name_for_topic(envelope.topic, prefix=self._stream_prefix)
        fields = envelope_to_fields(envelope)
        kwargs: dict[str, Any] = {}
        if self._max_len is not None:
            kwargs["maxlen"] = self._max_len
            kwargs["approximate"] = True
        try:
            await self._client.xadd(stream, fields, **kwargs)
        except RedisError as exc:
            raise QueueError(f"redis xadd failed for topic={envelope.topic!r}") from exc
