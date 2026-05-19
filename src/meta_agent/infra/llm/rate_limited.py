"""Rate-limit wrapper for :class:`LLMClient` adapters.

Sits **outside** :class:`MeteredLLMClient` in the wiring stack:

    OpenRouterClient  (raw HTTP)
      └─ MeteredLLMClient  (writes llm_usage_logs)
           └─ RateLimitedLLMClient  (consults RateLimiter port)

Why this order: denied requests are *not* billed. The rate-limit check
runs before any token is sent to the provider, before any usage row is
written. Surfaced exception is :class:`LLMRateLimitedError` so existing
retry policy upstream keeps working unchanged.

Backend-failure policy
======================
If the :class:`RateLimiter` raises :class:`RateLimiterBackendError`
(e.g. Redis disconnect) the wrapper **fails open** by default: it logs
a structured warning and forwards the call. Rationale: a Redis blip
must not silently brick all LLM traffic. Set ``fail_open=False`` to
flip to fail-closed for high-sensitivity deployments.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from meta_agent.core.ports.llm import (
    LLMClient,
    LLMRateLimitedError,
    LLMRequest,
    LLMResponse,
)
from meta_agent.core.ports.rate_limiter import RateLimiter, RateLimiterBackendError
from meta_agent.infra.security.context import RequestContext, get_current

logger = logging.getLogger(__name__)

_DEFAULT_TENANT_LABEL = "anonymous"
_DEFAULT_MODEL_LABEL = "default"


class RateLimitedLLMClient(LLMClient):
    """Decorator that consults a :class:`RateLimiter` before delegating.

    Parameters
    ----------
    inner:
        The wrapped :class:`LLMClient`. Typically already wrapped by
        :class:`MeteredLLMClient`.
    limiter:
        Backend used to gate calls. Production wiring injects a
        Redis-backed limiter; tests pass an in-memory one.
    provider:
        Logical provider name embedded in the bucket key
        (``openrouter`` today; future ``anthropic`` / ``vllm`` etc).
    fail_open:
        On :class:`RateLimiterBackendError`, forward the call instead
        of raising. Default ``True``.
    key_factory:
        Optional override; tests can inject a deterministic factory.
        Production code should leave this ``None``.
    """

    def __init__(
        self,
        inner: LLMClient,
        limiter: RateLimiter,
        *,
        provider: str,
        fail_open: bool = True,
        key_factory: Callable[[RequestContext | None, LLMRequest], str] | None = None,
    ) -> None:
        if not provider:
            raise ValueError("provider must be a non-empty string")
        self._inner = inner
        self._limiter = limiter
        self._provider = provider
        self._fail_open = fail_open
        self._key_factory = key_factory if key_factory is not None else self._default_key

    async def complete(self, request: LLMRequest) -> LLMResponse:
        ctx = get_current()
        key = self._key_factory(ctx, request)
        try:
            decision = await self._limiter.acquire(key)
        except RateLimiterBackendError as exc:
            if not self._fail_open:
                raise
            logger.warning(
                "llm.rate_limited.backend_error_fail_open",
                extra={
                    "tenant_id": ctx.tenant_id if ctx is not None else None,
                    "trace_id": ctx.trace_id if ctx is not None else None,
                    "task_id": ctx.task_id if ctx is not None else None,
                    "provider": self._provider,
                    "requested_model": request.model,
                    "error_type": type(exc).__name__,
                },
            )
            return await self._inner.complete(request)

        if not decision.allowed:
            logger.info(
                "llm.rate_limited.denied",
                extra={
                    "tenant_id": ctx.tenant_id if ctx is not None else None,
                    "trace_id": ctx.trace_id if ctx is not None else None,
                    "task_id": ctx.task_id if ctx is not None else None,
                    "provider": self._provider,
                    "requested_model": request.model,
                    "retry_after_ms": decision.retry_after_ms,
                    "remaining": decision.remaining,
                },
            )
            retry_after = (
                decision.retry_after_ms / 1000.0 if decision.retry_after_ms is not None else None
            )
            raise LLMRateLimitedError(
                f"rate limit exceeded for {self._provider}",
                retry_after=retry_after,
            )

        return await self._inner.complete(request)

    async def close(self) -> None:
        await self._inner.close()

    def _default_key(self, ctx: RequestContext | None, request: LLMRequest) -> str:
        tenant = ctx.tenant_id if ctx is not None else _DEFAULT_TENANT_LABEL
        model = request.model or _DEFAULT_MODEL_LABEL
        return f"llm:{self._provider}:tenant={tenant}:model={model}"


__all__ = ["RateLimitedLLMClient"]
