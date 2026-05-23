"""LLMClient decorator that broadcasts streaming chunks to a per-task channel.

Place this decorator **outside** :class:`RedactingLLMClient` in the
production wiring stack so subscribers see redacted bytes only —
the SSE wire to the client must not leak secrets the redactor would
otherwise scrub.

The decorator only affects :meth:`stream`. :meth:`complete` is a
straight pass-through: a buffered completion produces a single
:class:`LLMResponse`, not chunks, so there is nothing to fan out.
Future PRs may publish synthesised single-chunk events from
``complete`` if clients need to observe non-streaming calls; that
is YAGNI for now.

Publish failures are best-effort
================================
:class:`ChunkBroadcasterError` raised by the broadcaster is caught
and logged as a structured warning. The chunk is still yielded to
the caller — the agent loop must not stall because Redis blipped.
This mirrors :class:`MeteredLLMClient`'s "recording must not break
the heat path" rule.

Context derivation
==================
The publish key (``tenant_id`` + ``task_id``) comes from the bound
:class:`RequestContext`. If no context is bound, or ``task_id`` is
missing (e.g. a direct API LLM call not tied to a task), the
decorator skips publication and just forwards chunks: there is no
channel to publish to.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from meta_agent.core.ports.chunk_broadcaster import (
    ChunkBroadcaster,
    ChunkBroadcasterError,
)
from meta_agent.core.ports.llm import (
    LLMClient,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
)
from meta_agent.infra.security.context import get_current

logger = logging.getLogger(__name__)


class BroadcastingLLMClient(LLMClient):
    """Outermost streaming decorator: fan chunks out to a per-task channel."""

    def __init__(self, inner: LLMClient, broadcaster: ChunkBroadcaster) -> None:
        self._inner = inner
        self._broadcaster = broadcaster

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return await self._inner.complete(request)

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        ctx = get_current()
        tenant_id = ctx.tenant_id if ctx is not None else None
        task_id = ctx.task_id if ctx is not None else None
        async for chunk in self._inner.stream(request):
            if tenant_id and task_id:
                try:
                    await self._broadcaster.publish(
                        tenant_id=tenant_id, task_id=task_id, chunk=chunk
                    )
                except ChunkBroadcasterError as exc:
                    logger.warning(
                        "llm.broadcast.publish_failed",
                        extra={
                            "tenant_id": tenant_id,
                            "task_id": task_id,
                            "trace_id": ctx.trace_id if ctx is not None else None,
                            "error_type": type(exc).__name__,
                        },
                    )
            yield chunk

    async def close(self) -> None:
        await self._inner.close()


__all__ = ["BroadcastingLLMClient"]
