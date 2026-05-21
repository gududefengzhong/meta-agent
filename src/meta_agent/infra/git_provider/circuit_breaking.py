"""Circuit-breaker wrapper for :class:`GitProvider` adapters.

Sits **inside** :class:`RateLimitedGitProvider` in the wiring stack:

    GitHubGitProvider                  (raw HTTP)
      └─ CircuitBreakingGitProvider    (guards the real provider only)
           └─ RateLimitedGitProvider   (consults RateLimiter port)

Mirrors the LLM stack order in
:mod:`meta_agent.infra.llm.circuit_breaking`: the breaker counts only
**provider** failures. Putting it inside the rate-limit wrapper means
denied calls never reach the breaker (deny is normal control flow, not
a downstream failure), and the breaker therefore cannot be tripped by
self-inflicted limit pressure.

Translation of breaker outcomes
===============================
* :class:`CircuitBreakerOpenError` → :class:`GitProviderTransientError`
  so the port contract ("adapter raises ``GitProviderError`` on
  failure") is preserved and existing retry policy upstream
  (``auto_pr``) keeps working. The breaker's ``retry_after_ms`` hint is
  logged structurally; it is not surfaced through the port because the
  v1 :class:`GitProviderError` family has no retry-hint slot.
* :class:`CircuitBreakerBackendError` → fail-open by default. A flaky
  breaker backend must not brick PR publishing; high-sensitivity
  deployments flip ``fail_open=False``.

Failure counting
================
``should_count`` defaults to "count every exception except known
caller-side errors". :class:`GitProviderAuthError` (revoked tokens) and
:class:`GitProviderInvalidRequestError` (malformed repo URL / forbidden
cross-fork push) are excluded because retrying will not help and they
should not poison the failure window.
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
from meta_agent.core.ports.git_provider import (
    GitProvider,
    GitProviderAuthError,
    GitProviderInvalidRequestError,
    GitProviderTransientError,
    PullRequestRef,
)
from meta_agent.infra.security.context import RequestContext, get_current

logger = logging.getLogger(__name__)

_AUDIT_ACTION_OPEN = "git.circuit_breaker.open"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _default_event_id() -> str:
    return f"ae-{uuid.uuid4()}"


def _default_should_count(exc: BaseException) -> bool:
    """Count anything that is not a known caller-side git error."""

    return not isinstance(exc, (GitProviderInvalidRequestError, GitProviderAuthError))


class CircuitBreakingGitProvider(GitProvider):
    """Decorator that runs ``inner.open_or_reuse_pr`` under a :class:`CircuitBreaker`.

    Parameters mirror :class:`RateLimitedGitProvider`. ``audit_sink``,
    when provided, emits a ``git.circuit_breaker.open``
    :class:`AuditEvent` on :class:`CircuitBreakerOpenError`.
    Audit-write failures are swallowed (warn log only) so a degraded
    ``audit_events`` table cannot brick the publish path.
    ``probe_failed`` is not surfaced here: the decorator only sees the
    underlying exception, not the breaker's state transition, and every
    probe failure is immediately followed by an ``open`` event anyway.
    """

    def __init__(
        self,
        inner: GitProvider,
        breaker: CircuitBreaker,
        *,
        provider: str,
        fail_open: bool = True,
        key_factory: Callable[[str, str], str] | None = None,
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

    async def open_or_reuse_pr(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        repo_url: str,
        base_ref: str,
        head_branch: str,
        head_commit_sha: str,
        title: str,
        body: str,
    ) -> PullRequestRef:
        key = self._key_factory(tenant_id, repo_url)

        async def _call() -> PullRequestRef:
            return await self._inner.open_or_reuse_pr(
                tenant_id=tenant_id,
                trace_id=trace_id,
                repo_url=repo_url,
                base_ref=base_ref,
                head_branch=head_branch,
                head_commit_sha=head_commit_sha,
                title=title,
                body=body,
            )

        try:
            return await self._breaker.call(key, _call, should_count=self._should_count)
        except CircuitBreakerOpenError as exc:
            ctx = get_current()
            logger.info(
                "git.circuit_breaker.open",
                extra={
                    "tenant_id": tenant_id,
                    "trace_id": trace_id,
                    "task_id": ctx.task_id if ctx is not None else None,
                    "provider": self._provider,
                    "repo_url": repo_url,
                    "key": exc.key,
                    "retry_after_ms": exc.retry_after_ms,
                },
            )
            await self._audit_open(
                ctx=ctx,
                tenant_id=tenant_id,
                trace_id=trace_id,
                repo_url=repo_url,
                key=exc.key,
                retry_after_ms=exc.retry_after_ms,
            )
            raise GitProviderTransientError(
                f"upstream circuit breaker open for {self._provider} repo={repo_url}"
            ) from exc
        except CircuitBreakerBackendError as exc:
            if not self._fail_open:
                raise
            logger.warning(
                "git.circuit_breaker.backend_error_fail_open",
                extra={
                    "tenant_id": tenant_id,
                    "trace_id": trace_id,
                    "provider": self._provider,
                    "repo_url": repo_url,
                    "error_type": type(exc).__name__,
                },
            )
            return await _call()

    async def close(self) -> None:
        await self._inner.close()

    def _default_key(self, tenant_id: str, repo_url: str) -> str:
        return f"git:{self._provider}:tenant={tenant_id}:repo={repo_url}"

    async def _audit_open(
        self,
        *,
        ctx: RequestContext | None,
        tenant_id: str,
        trace_id: str,
        repo_url: str,
        key: str,
        retry_after_ms: int | None,
    ) -> None:
        if self._audit_sink is None:
            return
        if ctx is None:
            logger.debug(
                "git.circuit_breaker.audit_skip_no_context",
                extra={"provider": self._provider, "repo_url": repo_url},
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
                "repo_url": repo_url,
                "key": key,
                "retry_after_ms": retry_after_ms,
            },
            occurred_at=self._clock(),
        )
        try:
            await self._audit_sink.append(event)
        except Exception as exc:
            logger.warning(
                "git.circuit_breaker.audit_append_failed",
                extra={
                    "tenant_id": tenant_id,
                    "trace_id": trace_id,
                    "provider": self._provider,
                    "repo_url": repo_url,
                    "error_type": type(exc).__name__,
                },
            )


__all__ = ["CircuitBreakingGitProvider"]
