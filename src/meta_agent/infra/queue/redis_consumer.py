"""Redis Streams implementation of :class:`MessageConsumer`.

Each :class:`RedisStreamConsumer` binds to one ``(topic, group,
consumer_name)`` triple. On :meth:`start`, the consumer:

1. Ensures the consumer group exists (``XGROUP CREATE`` with
   ``MKSTREAM``; tolerates ``BUSYGROUP`` on restart).
2. Loops on ``XREADGROUP`` to fetch new entries.
3. Rebinds the :class:`RequestContext` from each envelope before
   invoking ``handler`` so downstream code keeps multi-tenant
   isolation and tracing.
4. On handler success, ``XACK`` the entry; on failure, leaves it in
   the pending entries list (PEL) for a future reclaim.

The dispatcher / orchestration layer is responsible for deciding
ultimate retry policy by inspecting envelope ``attempts`` and the
domain-specific :class:`AgentError` category.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

from meta_agent.core.ports.message import MessageEnvelope, MessageHandler
from meta_agent.core.ports.queue import MessageConsumer, QueueError
from meta_agent.infra.queue._serde import fields_to_envelope
from meta_agent.infra.queue.topic import (
    DEFAULT_STREAM_PREFIX,
    stream_name_for_topic,
)
from meta_agent.infra.security.context import RequestContext, bind_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveredMessage:
    """One in-flight entry with its Redis-Streams PEL metadata.

    ``delivery_count`` reflects how many times Redis has delivered this
    entry to a consumer in the group; it starts at 1 on first delivery
    and grows whenever a previously-unacked entry is redelivered. The
    worker uses it to bound retries before declaring the task abandoned.
    """

    envelope: MessageEnvelope
    entry_id: str
    delivery_count: int


class RedisStreamConsumer(MessageConsumer):
    """Polling Redis-Streams consumer bound to one group identity."""

    def __init__(
        self,
        client: Redis,
        *,
        topic: str,
        group: str,
        consumer_name: str,
        batch_size: int = 16,
        block_ms: int = 1_000,
        stream_prefix: str = DEFAULT_STREAM_PREFIX,
    ) -> None:
        self._client = client
        self._topic = topic
        self._group = group
        self._consumer_name = consumer_name
        self._batch_size = batch_size
        self._block_ms = block_ms
        self._stream = stream_name_for_topic(topic, prefix=stream_prefix)
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False

    async def _ensure_group(self) -> None:
        try:
            await self._client.xgroup_create(
                name=self._stream,
                groupname=self._group,
                id="0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise QueueError(
                    f"failed to create consumer group {self._group!r} on {self._stream!r}"
                ) from exc

    async def start(self, handler: MessageHandler) -> None:
        if self._running:
            raise QueueError("RedisStreamConsumer is already running")
        self._running = True
        self._stop_event.clear()
        await self._ensure_group()
        try:
            while not self._stop_event.is_set():
                await self._poll_once(handler)
        finally:
            self._running = False

    async def stop(self) -> None:
        self._stop_event.set()

    async def _poll_once(self, handler: MessageHandler) -> None:
        try:
            entries: Any = await self._client.xreadgroup(
                groupname=self._group,
                consumername=self._consumer_name,
                streams={self._stream: ">"},
                count=self._batch_size,
                block=self._block_ms,
            )
        except RedisError as exc:
            raise QueueError("redis xreadgroup failed") from exc
        if not entries:
            return
        # entries: [(stream_name, [(entry_id, {field: value}), ...])]
        for _stream, batch in entries:
            for entry_id, fields in batch:
                await self._dispatch(handler, entry_id, fields)

    async def _dispatch(
        self,
        handler: MessageHandler,
        entry_id: bytes | str,
        fields: dict[bytes | str, bytes | str],
    ) -> None:
        try:
            envelope = fields_to_envelope(fields)
        except Exception as exc:  # malformed payload: ack to skip poison
            logger.exception(
                "queue.poison_message",
                extra={"entry_id": str(entry_id), "error": str(exc)},
            )
            await self._safe_ack(entry_id)
            return
        ctx = _envelope_to_context(envelope)
        try:
            with bind_context(ctx):
                await handler(envelope)
        except Exception:
            logger.exception(
                "queue.handler_failed",
                extra={
                    "entry_id": str(entry_id),
                    "topic": envelope.topic,
                    "message_id": envelope.message_id,
                },
            )
            return  # leave in PEL for later reclaim
        await self._safe_ack(entry_id)

    async def _safe_ack(self, entry_id: bytes | str) -> None:
        try:
            await self._client.xack(self._stream, self._group, entry_id)
        except RedisError:
            logger.exception("queue.ack_failed", extra={"entry_id": str(entry_id)})

    async def claim_batch(
        self,
        *,
        block_ms: int | None = None,
    ) -> list[DeliveredMessage]:
        """Pull a batch of entries with their PEL delivery counts.

        Reads previously-claimed-but-unacked entries first (own PEL via
        ``XREADGROUP`` with id ``"0"``) and then new entries (id
        ``">"``). For each returned entry, ``delivery_count`` is read
        from ``XPENDING``; entries that have been XACK'd between the
        read and the pending lookup are skipped silently.
        """

        await self._ensure_group()
        block = self._block_ms if block_ms is None else block_ms
        delivered: list[DeliveredMessage] = []
        for stream_id in ("0", ">"):
            entries = await self._read_for_id(stream_id, block_ms=block)
            for entry_id, fields in entries:
                try:
                    envelope = fields_to_envelope(fields)
                except Exception as exc:
                    logger.exception(
                        "queue.poison_message",
                        extra={"entry_id": str(entry_id), "error": str(exc)},
                    )
                    await self._safe_ack(entry_id)
                    continue
                count = await self._delivery_count(entry_id)
                delivered.append(
                    DeliveredMessage(
                        envelope=envelope,
                        entry_id=_to_str(entry_id),
                        delivery_count=count,
                    )
                )
        return delivered

    async def ack(self, entry_id: str) -> None:
        """Acknowledge ``entry_id`` so Redis removes it from the PEL."""

        await self._safe_ack(entry_id)

    async def _read_for_id(
        self,
        stream_id: str,
        *,
        block_ms: int,
    ) -> list[tuple[bytes | str, dict[bytes | str, bytes | str]]]:
        try:
            entries: Any = await self._client.xreadgroup(
                groupname=self._group,
                consumername=self._consumer_name,
                streams={self._stream: stream_id},
                count=self._batch_size,
                block=block_ms,
            )
        except RedisError as exc:
            raise QueueError("redis xreadgroup failed") from exc
        if not entries:
            return []
        out: list[tuple[bytes | str, dict[bytes | str, bytes | str]]] = []
        for _stream, batch in entries:
            out.extend(batch)
        return out

    async def _delivery_count(self, entry_id: bytes | str) -> int:
        try:
            pending: Any = await self._client.xpending_range(
                name=self._stream,
                groupname=self._group,
                min=entry_id,
                max=entry_id,
                count=1,
            )
        except RedisError as exc:
            raise QueueError("redis xpending failed") from exc
        if not pending:
            return 1
        record = pending[0]
        # redis-py returns either a dict-like or tuple, normalise to int
        raw = record["times_delivered"] if isinstance(record, dict) else record[3]
        return int(raw)


def _to_str(entry_id: bytes | str) -> str:
    return entry_id.decode() if isinstance(entry_id, bytes) else entry_id


def _envelope_to_context(envelope: MessageEnvelope) -> RequestContext:
    return RequestContext(
        tenant_id=envelope.tenant_id,
        principal_id=envelope.principal_id or "system",
        trace_id=envelope.trace_id,
        request_id=envelope.request_id or envelope.message_id,
        session_id=envelope.session_id,
        task_id=envelope.task_id,
        idempotency_key=envelope.idempotency_key,
    )
