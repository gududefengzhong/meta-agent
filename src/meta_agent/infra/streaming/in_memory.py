"""In-memory :class:`ChunkBroadcaster` backed by ``asyncio.Queue`` fanout.

Used by unit tests and the single-process dev mode (one worker + one
API in the same process). For production with the worker pool and
API tier in separate processes, use :class:`RedisChunkBroadcaster`.

Semantics
=========
Pub/sub: a subscriber receives only chunks published while it is
subscribed. Chunks published before the subscriber's iterator is
pulled or after the subscriber stops iterating are dropped.

Each subscriber owns an :class:`asyncio.Queue` with a bounded size
(default 256 chunks). If the queue is full when ``publish`` is
called, the *oldest* queued chunk for that subscriber is evicted
to make room: chunks are UX, not state, and dropping old fragments
is preferable to backpressuring the LLM stream. The eviction count
is exposed via the broadcaster for observability tests.

Subscription lifetime
=====================
``subscribe`` returns a custom subscription object (not a vanilla
async generator). Async generators that are closed before their
first ``__anext__`` skip their ``finally`` block per PEP 525, so a
generator-based design would leak queues on early close. The
explicit ``aclose`` runs registration cleanup unconditionally.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator

from meta_agent.core.ports.chunk_broadcaster import ChunkBroadcaster
from meta_agent.core.ports.llm import LLMStreamChunk

_DEFAULT_QUEUE_MAXSIZE = 256


class InMemoryChunkBroadcaster(ChunkBroadcaster):
    """Process-local pub/sub for :class:`LLMStreamChunk`."""

    def __init__(self, *, queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE) -> None:
        if queue_maxsize <= 0:
            raise ValueError("queue_maxsize must be > 0")
        self._queue_maxsize = queue_maxsize
        self._channels: dict[str, list[asyncio.Queue[LLMStreamChunk]]] = {}
        self._lock = asyncio.Lock()
        self.dropped_chunk_count = 0

    async def publish(self, *, tenant_id: str, task_id: str, chunk: LLMStreamChunk) -> None:
        key = _channel_key(tenant_id, task_id)
        async with self._lock:
            queues = list(self._channels.get(key, ()))
        for q in queues:
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                self.dropped_chunk_count += 1
            q.put_nowait(chunk)

    async def subscribe(
        self, *, tenant_id: str, task_id: str
    ) -> AsyncGenerator[LLMStreamChunk, None]:
        key = _channel_key(tenant_id, task_id)
        queue: asyncio.Queue[LLMStreamChunk] = asyncio.Queue(maxsize=self._queue_maxsize)
        async with self._lock:
            self._channels.setdefault(key, []).append(queue)
        return _Subscription(self, key, queue)

    async def close(self) -> None:
        async with self._lock:
            self._channels.clear()

    async def _unregister(self, key: str, queue: asyncio.Queue[LLMStreamChunk]) -> None:
        async with self._lock:
            if key in self._channels:
                with contextlib.suppress(ValueError):
                    self._channels[key].remove(queue)
                if not self._channels[key]:
                    del self._channels[key]


class _Subscription:
    """Async iterator over one subscriber's queue with explicit cleanup.

    Conforms to the :class:`AsyncGenerator` protocol surface
    (``__aiter__`` / ``__anext__`` / ``aclose``) so callers can treat
    it as a generator without caring about the underlying type.
    """

    def __init__(
        self,
        broadcaster: InMemoryChunkBroadcaster,
        key: str,
        queue: asyncio.Queue[LLMStreamChunk],
    ) -> None:
        self._broadcaster = broadcaster
        self._key = key
        self._queue = queue
        self._closed = False

    def __aiter__(self) -> _Subscription:
        return self

    async def __anext__(self) -> LLMStreamChunk:
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._broadcaster._unregister(self._key, self._queue)

    async def asend(self, value: None) -> LLMStreamChunk:
        # AsyncGenerator protocol completeness; we only consume chunks,
        # so ``send`` reduces to ``__anext__``.
        return await self.__anext__()

    async def athrow(
        self,
        typ: type[BaseException] | BaseException,
        val: BaseException | object = None,
        tb: object = None,
    ) -> LLMStreamChunk:
        # AsyncGenerator protocol completeness; we don't accept thrown
        # values, so just close and re-raise.
        await self.aclose()
        if isinstance(typ, BaseException):
            raise typ
        if isinstance(val, BaseException):
            raise val
        raise typ()


def _channel_key(tenant_id: str, task_id: str) -> str:
    return f"{tenant_id}::{task_id}"
