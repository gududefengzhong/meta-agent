"""Cross-process channel for LLM stream chunks (Phase δ-1).

The worker runs the agent loop and makes LLM calls. The API tier
serves the SSE endpoint that delivers chunks to clients (VS Code
plugin / CLI). These two processes need a way to ferry
:class:`LLMStreamChunk` instances from producer to consumer without
sharing memory.

This port is intentionally minimal: it does not persist chunks (a
client that misses an in-flight chunk cannot replay it — the agent
loop is the source of truth and the client should reconnect to the
audit / trajectory stream for any state that matters) and does not
guarantee per-subscriber delivery (a subscriber that is slow may
drop chunks; the backend decides whether to buffer or evict).

Backends
========
* :class:`InMemoryChunkBroadcaster` — asyncio queues; single-process,
  used by unit tests and the local single-process dev mode.
* ``RedisChunkBroadcaster`` (infra) — Redis pub/sub; production
  default. Subscribers receive only chunks published while they are
  subscribed; pub/sub has no replay semantics.

Tenant isolation
================
Every method takes a ``tenant_id`` + ``task_id``. Backends MUST
namespace the channel so chunks from one tenant never leak to a
subscriber bound to a different tenant — the API endpoint enforces
that the requesting principal owns the task, but the broadcaster's
own keying is a second line of defence.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator

from meta_agent.core.ports.llm import LLMStreamChunk


class ChunkBroadcasterError(Exception):
    """Raised by backends that fail to publish or subscribe.

    Producers SHOULD treat this as best-effort: a publish failure
    must never abort the underlying LLM stream, since the chunks are
    being relayed for UX, not correctness.
    """


class ChunkBroadcaster(ABC):
    """Publish + subscribe surface for LLM stream chunks."""

    @abstractmethod
    async def publish(self, *, tenant_id: str, task_id: str, chunk: LLMStreamChunk) -> None:
        """Fan ``chunk`` out to every active subscriber for ``(tenant_id, task_id)``.

        Backends MUST be safe to call concurrently from multiple
        coroutines (the LLM stream may be driven by a single graph
        node, but the per-tenant publish key is shared across the
        worker pool). On backend failure raise :class:`ChunkBroadcasterError`
        — the caller (the broadcasting LLM decorator) swallows it
        with a structured warning.
        """

    @abstractmethod
    async def subscribe(
        self, *, tenant_id: str, task_id: str
    ) -> AsyncGenerator[LLMStreamChunk, None]:
        """Return an async iterator over chunks published after this call.

        ``subscribe`` is ``async`` because the backend MUST register
        the subscriber before returning. A lazy generator that only
        registers on first ``__anext__`` would race against a
        ``publish`` happening between ``subscribe`` and the first
        pull, dropping chunks the caller expected to see.

        Pub/sub semantics: a subscriber receives only chunks that
        are published while the subscription is active. Chunks
        published before ``subscribe`` returns or after the
        subscriber stops iterating are dropped.

        The returned iterator MUST be closeable via the standard
        async-generator ``aclose`` protocol so callers can release
        backend resources (Redis pub/sub channels, asyncio queues)
        on disconnect.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release any backend-side connection state. Idempotent."""
