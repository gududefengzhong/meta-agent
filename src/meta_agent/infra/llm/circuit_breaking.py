"""Circuit-breaker wrapper for :class:`LLMClient` adapters.

Sits **inside** :class:`MeteredLLMClient` in the wiring stack:

    OpenRouterClient                 (raw HTTP)
      └─ CircuitBreakingLLMClient    (guards the real provider only)
           └─ MeteredLLMClient       (writes llm_usage_logs)
                └─ RateLimitedLLMClient  (consults RateLimiter port)

Rationale for the order: the breaker counts **provider** failures only.
Putting it inside ``MeteredLLMClient`` means a flaky usage-log table
cannot trip the breaker (``MeteredLLMClient`` silently swallows
recorder errors), and rate-limit denials never reach the breaker at
all (they are intercepted by the outermost wrapper).

Translation of breaker outcomes
===============================
* :class:`CircuitBreakerOpenError` → :class:`LLMTransientError`
  so the contract "``LLMClient`` raises an :class:`LLMError` on
  failure" is preserved and existing retry policy upstream keeps
  working. The breaker's ``retry_after_ms`` hint is logged
  structurally; it is not exposed via :class:`LLMRateLimitedError`
  because the semantics are different (breaker open is downstream
  unhealthy, not "you sent too many").
* :class:`CircuitBreakerBackendError` → fail-open by default: the
  call is forwarded to ``inner`` and a structured warning is logged.
  Set ``fail_open=False`` for high-sensitivity deployments.

Failure counting
================
``should_count`` defaults to "count every exception except known
caller-side errors". :class:`LLMInvalidRequestError` and
:class:`LLMAuthError` are excluded because retrying will not help and
they should not poison the failure window.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from meta_agent.core.ports.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerBackendError,
    CircuitBreakerOpenError,
)
from meta_agent.core.ports.llm import (
    LLMAuthError,
    LLMClient,
    LLMInvalidRequestError,
    LLMRequest,
    LLMResponse,
    LLMTransientError,
)
from meta_agent.infra.security.context import RequestContext, get_current

logger = logging.getLogger(__name__)

_DEFAULT_TENANT_LABEL = "anonymous"
_DEFAULT_MODEL_LABEL = "default"


def _default_should_count(exc: BaseException) -> bool:
    """Count anything that is not a known caller-side error."""

    return not isinstance(exc, (LLMInvalidRequestError, LLMAuthError))


class CircuitBreakingLLMClient(LLMClient):
    """Decorator that runs ``inner.complete`` under a :class:`CircuitBreaker`."""

    def __init__(
        self,
        inner: LLMClient,
        breaker: CircuitBreaker,
        *,
        provider: str,
        fail_open: bool = True,
        key_factory: Callable[[RequestContext | None, LLMRequest], str] | None = None,
        should_count: Callable[[BaseException], bool] | None = None,
    ) -> None:
        if not provider:
            raise ValueError("provider must be a non-empty string")
        self._inner = inner
        self._breaker = breaker
        self._provider = provider
        self._fail_open = fail_open
        self._key_factory = key_factory if key_factory is not None else self._default_key
        self._should_count = should_count if should_count is not None else _default_should_count

    async def complete(self, request: LLMRequest) -> LLMResponse:
        ctx = get_current()
        key = self._key_factory(ctx, request)

        async def _call() -> LLMResponse:
            return await self._inner.complete(request)

        try:
            return await self._breaker.call(key, _call, should_count=self._should_count)
        except CircuitBreakerOpenError as exc:
            logger.info(
                "llm.circuit_breaker.open",
                extra={
                    "tenant_id": ctx.tenant_id if ctx is not None else None,
                    "trace_id": ctx.trace_id if ctx is not None else None,
                    "task_id": ctx.task_id if ctx is not None else None,
                    "provider": self._provider,
                    "requested_model": request.model,
                    "key": exc.key,
                    "retry_after_ms": exc.retry_after_ms,
                },
            )
            raise LLMTransientError(f"upstream circuit breaker open for {self._provider}") from exc
        except CircuitBreakerBackendError as exc:
            if not self._fail_open:
                raise
            logger.warning(
                "llm.circuit_breaker.backend_error_fail_open",
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

    async def close(self) -> None:
        await self._inner.close()

    def _default_key(self, ctx: RequestContext | None, request: LLMRequest) -> str:
        tenant = ctx.tenant_id if ctx is not None else _DEFAULT_TENANT_LABEL
        model = request.model or _DEFAULT_MODEL_LABEL
        return f"llm:{self._provider}:tenant={tenant}:model={model}"


__all__ = ["CircuitBreakingLLMClient"]
