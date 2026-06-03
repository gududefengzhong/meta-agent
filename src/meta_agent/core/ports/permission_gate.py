"""Cross-process inline-permission rendezvous (Phase δ-1).

The worker (graph node) needs to ask "may I run this tool?" and
*block* until the client (VS Code / CLI / browser) answers, then
proceed exactly as if the answer had been available locally. The
API tier is what physically receives the client's HTTP POST.

This port hides that worker↔API split behind a small rendezvous
surface:

* :meth:`request` — worker side. Publishes the prompt, waits for the
  decision (with a timeout), returns it. Raises
  :class:`PermissionTimeoutError` if no answer arrives in time so
  the graph can fall back gracefully.
* :meth:`deliver` — API side. Called by the POST handler when the
  client decides. Routes the decision back to the waiting
  :meth:`request` coroutine.

Backends
========
* :class:`InMemoryPermissionGate` (infra) — asyncio.Future fanout;
  single-process tests + dev.
* ``RedisPermissionGate`` (infra) — Redis pub/sub round-trip;
  production default (worker and API are separate processes that
  share a Redis pool).

"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator

from meta_agent.core.domain.permission import PermissionDecision, PermissionPrompt


class PermissionGateError(Exception):
    """Base class for gate-backend failures."""


class PermissionTimeoutError(PermissionGateError):
    """No decision arrived inside the :meth:`request` timeout window.

    The graph node MUST decide what to do — typically "treat as
    deny" so a silent client doesn't get accidentally trusted, but
    the choice is left to the caller because some flows might want
    a different default (e.g. read-only tools could timeout-allow).
    """


class PermissionGate(ABC):
    """Two-way rendezvous for inline permission prompts."""

    @abstractmethod
    async def request(
        self,
        prompt: PermissionPrompt,
        *,
        timeout_seconds: float,
    ) -> PermissionDecision:
        """Worker-side: publish the prompt + block until the client decides.

        Raises :class:`PermissionTimeoutError` if no decision arrives
        within ``timeout_seconds``. Backends MUST clean up the
        listener registration on timeout / cancellation so a stale
        registration doesn't pin memory or leak Redis pubsub channels.
        """

    @abstractmethod
    async def deliver(self, decision: PermissionDecision) -> None:
        """API-side: route ``decision`` to the worker's waiting :meth:`request`.

        A decision that arrives for a prompt nobody is waiting on
        (worker already timed out, or the prompt was never issued)
        MUST be silently swallowed — the API has no way to
        distinguish "stale" from "spurious" and raising would force
        the API to retain prompt-issuance state it shouldn't own.
        """

    @abstractmethod
    async def subscribe_prompts(
        self,
        *,
        tenant_id: str,
        task_id: str,
    ) -> AsyncGenerator[PermissionPrompt, None]:
        """Optional client-side prompt subscription for ``(tenant_id, task_id)``.

        Pub/sub semantics: a subscriber receives only prompts
        published while it is subscribed; prompts before / after are
        dropped.

        The returned iterator MUST be closeable via the standard
        async-generator ``aclose`` protocol so the caller can release
        backend resources when the subscription ends.

        ``async def`` because the backend MUST register the
        subscriber before returning — a lazy generator could race
        with a fast ``request`` and drop the prompt.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release any per-backend connection state. Idempotent."""


__all__ = [
    "PermissionGate",
    "PermissionGateError",
    "PermissionTimeoutError",
]
