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
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.ports.audit_sink import AuditSink
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

_AUDIT_ACTION_DENIED = "llm.rate_limited.denied"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _default_event_id() -> str:
    return f"ae-{uuid.uuid4()}"


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
    audit_sink:
        If provided, deny outcomes append an ``llm.rate_limited.denied``
        :class:`AuditEvent`. Audit-write failures are swallowed (warn
        log only) so a degraded ``audit_events`` table cannot brick
        the LLM heat path. Skipped if no :class:`RequestContext` is
        bound (the event schema requires tenant / principal / trace).
    """

    def __init__(
        self,
        inner: LLMClient,
        limiter: RateLimiter,
        *,
        provider: str,
        fail_open: bool = True,
        key_factory: Callable[[RequestContext | None, LLMRequest], str] | None = None,
        audit_sink: AuditSink | None = None,
        clock: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not provider:
            raise ValueError("provider must be a non-empty string")
        self._inner = inner
        self._limiter = limiter
        self._provider = provider
        self._fail_open = fail_open
        self._key_factory = key_factory if key_factory is not None else self._default_key
        self._audit_sink = audit_sink
        self._clock = clock if clock is not None else _utcnow
        self._event_id_factory = (
            event_id_factory if event_id_factory is not None else _default_event_id
        )

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
            await self._audit_deny(ctx, request, key, decision.retry_after_ms, decision.remaining)
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

    async def _audit_deny(
        self,
        ctx: RequestContext | None,
        request: LLMRequest,
        key: str,
        retry_after_ms: int | None,
        remaining: int | None,
    ) -> None:
        if self._audit_sink is None:
            return
        if ctx is None:
            # AuditEvent requires tenant/principal/trace; emit nothing
            # rather than fabricate identifiers.
            logger.debug(
                "llm.rate_limited.audit_skip_no_context",
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
            action=_AUDIT_ACTION_DENIED,
            payload={
                "provider": self._provider,
                "requested_model": request.model,
                "key": key,
                "retry_after_ms": retry_after_ms,
                "remaining": remaining,
            },
            occurred_at=self._clock(),
        )
        try:
            await self._audit_sink.append(event)
        except Exception as exc:
            logger.warning(
                "llm.rate_limited.audit_append_failed",
                extra={
                    "tenant_id": ctx.tenant_id,
                    "trace_id": ctx.trace_id,
                    "task_id": ctx.task_id,
                    "provider": self._provider,
                    "error_type": type(exc).__name__,
                },
            )


__all__ = ["RateLimitedLLMClient"]
