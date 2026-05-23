"""In-memory :class:`PermissionGate` backed by ``asyncio.Future``.

Used by unit tests and single-process dev mode (worker + API in
one process). For production with the worker pool and API tier in
separate processes, use :class:`RedisPermissionGate`.

Semantics
=========
``request(prompt)`` registers a pending future keyed by
``prompt_id``, then awaits the future with a timeout. ``deliver``
looks up the matching future and sets its result. Timeouts /
cancellations unregister the future so a long-running process
doesn't accumulate stale entries.
"""

from __future__ import annotations

import asyncio

from meta_agent.core.domain.permission import PermissionDecision, PermissionPrompt
from meta_agent.core.ports.permission_gate import (
    PermissionGate,
    PermissionTimeoutError,
)


class InMemoryPermissionGate(PermissionGate):
    """Process-local prompt/decision rendezvous."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[PermissionDecision]] = {}
        self._lock = asyncio.Lock()

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

    async def close(self) -> None:
        async with self._lock:
            for future in self._pending.values():
                if not future.done():
                    future.cancel()
            self._pending.clear()
