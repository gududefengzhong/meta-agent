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
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.ports.audit_sink import AuditSink
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

_AUDIT_ACTION_OPEN = "llm.circuit_breaker.open"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _default_event_id() -> str:
    return f"ae-{uuid.uuid4()}"


def _default_should_count(exc: BaseException) -> bool:
    """Count anything that is not a known caller-side error."""

    return not isinstance(exc, (LLMInvalidRequestError, LLMAuthError))


class CircuitBreakingLLMClient(LLMClient):
    """Decorator that runs ``inner.complete`` under a :class:`CircuitBreaker`.

    Parameters mirror :class:`RateLimitedLLMClient`. ``audit_sink``, when
    provided, emits an ``llm.circuit_breaker.open`` :class:`AuditEvent`
    on :class:`CircuitBreakerOpenError`. Audit-write failures are
    swallowed (warn log only) so a degraded ``audit_events`` table
    cannot brick the LLM heat path. ``probe_failed`` is not surfaced
    here: the decorator only sees the underlying exception, not the
    breaker's state transition, and every probe failure is immediately
    followed by an ``open`` event anyway.
    """

    def __init__(
        self,
        inner: LLMClient,
        breaker: CircuitBreaker,
        *,
        provider: str,
        fail_open: bool = True,
        key_factory: Callable[[RequestContext | None, LLMRequest], str] | None = None,
        should_count: Callable[[BaseException], bool] | None = None,
        audit_sink: AuditSink | None = None,
        clock: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not provider:
            raise ValueError("provider must be a non-empty string")
        self._inner = inner
        self._breaker = breaker
        self._provider = provider
        self._fail_open = fail_open
        self._key_factory = key_factory if key_factory is not None else self._default_key
        self._should_count = should_count if should_count is not None else _default_should_count
        self._audit_sink = audit_sink
        self._clock = clock if clock is not None else _utcnow
        self._event_id_factory = (
            event_id_factory if event_id_factory is not None else _default_event_id
        )

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
            await self._audit_open(ctx, request, exc.key, exc.retry_after_ms)
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

    async def _audit_open(
        self,
        ctx: RequestContext | None,
        request: LLMRequest,
        key: str,
        retry_after_ms: int | None,
    ) -> None:
        if self._audit_sink is None:
            return
        if ctx is None:
            # AuditEvent requires tenant/principal/trace; emit nothing
            # rather than fabricate identifiers.
            logger.debug(
                "llm.circuit_breaker.audit_skip_no_context",
                extra={"provider": self._provider, "requested_model": request.model},
            )
            return
        event = AuditEvent(
            event_id=self._event_id_factory(),
            tenant_id=ctx.tenant_id,
            principal_id=ctx.principal_id,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            trace_id=ctx.trace_id,
            action=_AUDIT_ACTION_OPEN,
            payload={
                "provider": self._provider,
                "requested_model": request.model,
                "key": key,
                "retry_after_ms": retry_after_ms,
            },
            occurred_at=self._clock(),
        )
        try:
            await self._audit_sink.append(event)
        except Exception as exc:
            logger.warning(
                "llm.circuit_breaker.audit_append_failed",
                extra={
                    "tenant_id": ctx.tenant_id,
                    "trace_id": ctx.trace_id,
                    "task_id": ctx.task_id,
                    "provider": self._provider,
                    "error_type": type(exc).__name__,
                },
            )


__all__ = ["CircuitBreakingLLMClient"]
