"""In-memory :class:`PermissionGate` backed by ``asyncio.Future`` + Queue fanout.

Used by unit tests and single-process dev mode (worker + API in
one process). For production with the worker pool and API tier in
separate processes, use :class:`RedisPermissionGate`.

Semantics
=========
``request(prompt)`` registers a pending future keyed by
``prompt_id``, fans the prompt out to every active prompt subscriber,
then awaits the future with a timeout. ``deliver`` looks up the
matching future and sets its result. Timeouts / cancellations
unregister the future so a long-running process doesn't accumulate
stale entries.

``subscribe_prompts`` returns an :class:`_PromptSubscription` that
receives every prompt published for the given ``(tenant_id, task_id)``
while it is subscribed (pub/sub semantics — no replay).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator

from meta_agent.core.domain.permission import PermissionDecision, PermissionPrompt
from meta_agent.core.ports.permission_gate import (
    PermissionGate,
    PermissionTimeoutError,
)

_DEFAULT_PROMPT_QUEUE_MAXSIZE = 64


class InMemoryPermissionGate(PermissionGate):
    """Process-local prompt/decision rendezvous."""

    def __init__(self, *, prompt_queue_maxsize: int = _DEFAULT_PROMPT_QUEUE_MAXSIZE) -> None:
        if prompt_queue_maxsize <= 0:
            raise ValueError("prompt_queue_maxsize must be > 0")
        self._prompt_queue_maxsize = prompt_queue_maxsize
        self._pending: dict[str, asyncio.Future[PermissionDecision]] = {}
        self._prompt_channels: dict[str, list[asyncio.Queue[PermissionPrompt]]] = {}
        self._lock = asyncio.Lock()
        self.dropped_prompt_count = 0

    async def request(
        self,
        prompt: PermissionPrompt,
        *,
        timeout_seconds: float,
    ) -> PermissionDecision:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[PermissionDecision] = loop.create_future()
        async with self._lock:
            if prompt.prompt_id in self._pending:
                raise ValueError(f"prompt_id {prompt.prompt_id!r} already has a pending request")
            self._pending[prompt.prompt_id] = future
            queues = list(
                self._prompt_channels.get(_channel_key(prompt.tenant_id, prompt.task_id), ())
            )
        # Fan the prompt out to live subscribers AFTER the future is
        # registered: a subscriber that publishes a decision right
        # away must find a pending future to deliver to.
        for queue in queues:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                self.dropped_prompt_count += 1
            queue.put_nowait(prompt)
        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError as exc:
            raise PermissionTimeoutError(
                f"no decision for prompt_id={prompt.prompt_id!r} within {timeout_seconds}s"
            ) from exc
        finally:
            async with self._lock:
                self._pending.pop(prompt.prompt_id, None)

    async def deliver(self, decision: PermissionDecision) -> None:
        async with self._lock:
            future = self._pending.get(decision.prompt_id)
        if future is None or future.done():
            # Stale / spurious decision — swallow per the port
            # contract. API has no way to distinguish from a typo.
            return
        future.set_result(decision)

    async def subscribe_prompts(
        self,
        *,
        tenant_id: str,
        task_id: str,
    ) -> AsyncGenerator[PermissionPrompt, None]:
        key = _channel_key(tenant_id, task_id)
        queue: asyncio.Queue[PermissionPrompt] = asyncio.Queue(maxsize=self._prompt_queue_maxsize)
        async with self._lock:
            self._prompt_channels.setdefault(key, []).append(queue)
        return _PromptSubscription(self, key, queue)

    async def close(self) -> None:
        async with self._lock:
            for future in self._pending.values():
                if not future.done():
                    future.cancel()
            self._pending.clear()
            self._prompt_channels.clear()

    async def _unregister_prompt_subscriber(
        self, key: str, queue: asyncio.Queue[PermissionPrompt]
    ) -> None:
        async with self._lock:
            if key in self._prompt_channels:
                with contextlib.suppress(ValueError):
                    self._prompt_channels[key].remove(queue)
                if not self._prompt_channels[key]:
                    del self._prompt_channels[key]


class _PromptSubscription:
    """Async iterator over one subscriber's prompt queue with explicit cleanup.

    Mirrors the pattern used by :class:`InMemoryChunkBroadcaster`'s
    subscription type: an async-generator-shaped iterator would
    leak the queue registration when ``aclose`` runs before the
    first ``__anext__`` (PEP 525 skips the ``finally`` block of an
    unstarted generator).
    """

    def __init__(
        self,
        gate: InMemoryPermissionGate,
        key: str,
        queue: asyncio.Queue[PermissionPrompt],
    ) -> None:
        self._gate = gate
        self._key = key
        self._queue = queue
        self._closed = False

    def __aiter__(self) -> _PromptSubscription:
        return self

    async def __anext__(self) -> PermissionPrompt:
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._gate._unregister_prompt_subscriber(self._key, self._queue)

    async def asend(self, value: None) -> PermissionPrompt:
        return await self.__anext__()

    async def athrow(
        self,
        typ: type[BaseException] | BaseException,
        val: BaseException | object = None,
        tb: object = None,
    ) -> PermissionPrompt:
        await self.aclose()
        if isinstance(typ, BaseException):
            raise typ
        if isinstance(val, BaseException):
            raise val
        raise typ()


def _channel_key(tenant_id: str, task_id: str) -> str:
    return f"{tenant_id}::{task_id}"
