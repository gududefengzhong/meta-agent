"""Monthly-budget wrapper for :class:`LLMClient` adapters.

Sits **outside** :class:`RateLimitedLLMClient` in the wiring stack:

    OpenRouterClient  (raw HTTP)
      └─ MeteredLLMClient            (writes llm_usage_logs)
           └─ RateLimitedLLMClient   (consults RateLimiter port)
                └─ BudgetEnforcingLLMClient  (consults BudgetEnforcer port)

Why outermost: a budget-denied call must not consume a rate-limit token
and must not even reach the upstream provider. Failing here also keeps
the audit row authoritative — one ``llm.budget.exceeded`` event per
real attempt.

TTL cache
=========
``BudgetEnforcer`` reads aggregate against ``llm_usage_logs`` on every
call. With request rates in the tens-of-Hz range that round-trip is
wasted: monthly budgets move in minutes / hours, not milliseconds.
A small in-process TTL cache keyed on ``tenant_id`` caps the DB read
rate at ``1 / cache_ttl_s`` per tenant per worker. The cache stores the
full :class:`BudgetDecision` so a deny stays cheap inside the window.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.ports.audit_sink import AuditSink
from meta_agent.core.ports.budget import (
    BudgetBackendError,
    BudgetDecision,
    BudgetEnforcer,
)
from meta_agent.core.ports.llm import (
    LLMBudgetExceededError,
    LLMClient,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
)
from meta_agent.infra.security.context import RequestContext, get_current

logger = logging.getLogger(__name__)

_AUDIT_ACTION_EXCEEDED = "llm.budget.exceeded"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _default_event_id() -> str:
    return f"ae-{uuid.uuid4()}"


class BudgetEnforcingLLMClient(LLMClient):
    """Decorator that consults a :class:`BudgetEnforcer` before delegating.

    Parameters
    ----------
    inner:
        The wrapped :class:`LLMClient`. Typically already wrapped by
        :class:`RateLimitedLLMClient`.
    enforcer:
        Backend used to evaluate the tenant's running monthly usage.
    provider:
        Logical provider name — included in audit payload only.
    fail_open:
        On :class:`BudgetBackendError`, forward the call instead of
        raising. Default ``True`` (a DB blip must not brick LLM traffic).
    cache_ttl_s:
        Seconds an in-process decision is reused for the same tenant.
        ``0`` disables the cache (every call hits the enforcer).
    audit_sink:
        If provided, deny outcomes append an ``llm.budget.exceeded``
        :class:`AuditEvent`. Append failures are swallowed (warn log
        only); skipped if no :class:`RequestContext` is bound.
    """

    def __init__(
        self,
        inner: LLMClient,
        enforcer: BudgetEnforcer,
        *,
        provider: str,
        fail_open: bool = True,
        cache_ttl_s: float = 10.0,
        audit_sink: AuditSink | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not provider:
            raise ValueError("provider must be a non-empty string")
        if cache_ttl_s < 0:
            raise ValueError("cache_ttl_s must be >= 0")
        self._inner = inner
        self._enforcer = enforcer
        self._provider = provider
        self._fail_open = fail_open
        self._cache_ttl_s = cache_ttl_s
        self._audit_sink = audit_sink
        self._clock = clock if clock is not None else _utcnow
        self._monotonic = monotonic
        self._event_id_factory = (
            event_id_factory if event_id_factory is not None else _default_event_id
        )
        self._cache: dict[str, tuple[float, BudgetDecision]] = {}

    async def complete(self, request: LLMRequest) -> LLMResponse:
        ctx = get_current()
        tenant_id = ctx.tenant_id if ctx is not None else None
        if not tenant_id:
            # No tenant ⇒ no per-tenant cap to evaluate. Mirrors the
            # rate-limit / audit no-ctx behaviour: log at debug and
            # forward; tenant-binding is an upstream concern.
            logger.debug(
                "llm.budget.skip_no_tenant",
                extra={"provider": self._provider, "requested_model": request.model},
            )
            return await self._inner.complete(request)

        decision = await self._check(tenant_id)
        if decision is None:
            return await self._inner.complete(request)

        if not decision.allowed:
            logger.info(
                "llm.budget.denied",
                extra={
                    "tenant_id": tenant_id,
                    "trace_id": ctx.trace_id if ctx is not None else None,
                    "task_id": ctx.task_id if ctx is not None else None,
                    "provider": self._provider,
                    "requested_model": request.model,
                    "tokens_used": decision.usage.tokens_used,
                    "limit_tokens": decision.limit_tokens,
                },
            )
            await self._audit_deny(ctx, request, decision)
            raise LLMBudgetExceededError(
                f"monthly token budget exceeded for tenant={tenant_id!r}",
                tokens_used=decision.usage.tokens_used,
                limit_tokens=decision.limit_tokens,
            )
        return await self._inner.complete(request)

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        """Same budget gate as :meth:`complete`, applied before any chunk flows."""

        ctx = get_current()
        tenant_id = ctx.tenant_id if ctx is not None else None
        if not tenant_id:
            logger.debug(
                "llm.budget.skip_no_tenant",
                extra={"provider": self._provider, "requested_model": request.model},
            )
            async for chunk in self._inner.stream(request):
                yield chunk
            return

        decision = await self._check(tenant_id)
        if decision is not None and not decision.allowed:
            logger.info(
                "llm.budget.denied",
                extra={
                    "tenant_id": tenant_id,
                    "trace_id": ctx.trace_id if ctx is not None else None,
                    "task_id": ctx.task_id if ctx is not None else None,
                    "provider": self._provider,
                    "requested_model": request.model,
                    "tokens_used": decision.usage.tokens_used,
                    "limit_tokens": decision.limit_tokens,
                },
            )
            await self._audit_deny(ctx, request, decision)
            raise LLMBudgetExceededError(
                f"monthly token budget exceeded for tenant={tenant_id!r}",
                tokens_used=decision.usage.tokens_used,
                limit_tokens=decision.limit_tokens,
            )
        async for chunk in self._inner.stream(request):
            yield chunk

    async def close(self) -> None:
        await self._inner.close()

    async def _check(self, tenant_id: str) -> BudgetDecision | None:
        """Return cached or fresh :class:`BudgetDecision`.

        ``None`` signals "treat as allowed and bypass enforcement" — only
        used when the enforcer raises :class:`BudgetBackendError` and
        ``fail_open`` is true.
        """

        now = self._monotonic()
        if self._cache_ttl_s > 0:
            cached = self._cache.get(tenant_id)
            if cached is not None:
                expires_at, decision = cached
                if now < expires_at:
                    return decision
        try:
            decision = await self._enforcer.check(tenant_id)
        except BudgetBackendError as exc:
            if not self._fail_open:
                raise
            logger.warning(
                "llm.budget.backend_error_fail_open",
                extra={
                    "tenant_id": tenant_id,
                    "provider": self._provider,
                    "error_type": type(exc).__name__,
                },
            )
            return None
        if self._cache_ttl_s > 0:
            self._cache[tenant_id] = (now + self._cache_ttl_s, decision)
        return decision

    async def _audit_deny(
        self,
        ctx: RequestContext | None,
        request: LLMRequest,
        decision: BudgetDecision,
    ) -> None:
        if self._audit_sink is None:
            return
        if ctx is None:
            # AuditEvent requires tenant/principal/trace; emit nothing
            # rather than fabricate identifiers.
            logger.debug(
                "llm.budget.audit_skip_no_context",
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
            action=_AUDIT_ACTION_EXCEEDED,
            payload={
                "provider": self._provider,
                "requested_model": request.model,
                "tokens_used": decision.usage.tokens_used,
                "cost_usd_micros_used": decision.usage.cost_usd_micros_used,
                "limit_tokens": decision.limit_tokens,
            },
            occurred_at=self._clock(),
        )
        try:
            await self._audit_sink.append(event)
        except Exception as exc:
            logger.warning(
                "llm.budget.audit_append_failed",
                extra={
                    "tenant_id": ctx.tenant_id,
                    "trace_id": ctx.trace_id,
                    "task_id": ctx.task_id,
                    "provider": self._provider,
                    "error_type": type(exc).__name__,
                },
            )


__all__ = ["BudgetEnforcingLLMClient"]
