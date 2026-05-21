"""Rate-limit wrapper for :class:`GitProvider` adapters.

Sits **outside** :class:`CircuitBreakingGitProvider` in the wiring stack:

    GitHubGitProvider                  (raw HTTP)
      └─ CircuitBreakingGitProvider    (guards the real provider only)
           └─ RateLimitedGitProvider   (consults RateLimiter port)

Mirrors the LLM stack order in :mod:`meta_agent.infra.llm.rate_limited`:
keeping the limiter on the outside means denied calls never advance to
the breaker (so deny noise cannot trip the breaker) and never touch the
upstream provider (so a hostile burst cannot exhaust the per-token
GitHub quota for the whole worker).

Key shape
=========
``git:{provider}:tenant={tid}:repo={repo_url}`` — mirrors the LLM key
shape so operators can grep across both surfaces. ``repo_url`` is used
verbatim (the port already treats it as an opaque caller-side
identifier; normalising it here would only add a parser we cannot keep
in sync with the GitHub adapter's own URL handling).

Translation of limiter outcomes
===============================
* deny → :class:`GitProviderTransientError`. Callers already retry that
  category with backoff; mapping deny into it lets ``auto_pr`` reuse
  the existing retry envelope without learning a new error class.
* :class:`RateLimiterBackendError` → fail-open by default. A flaky
  limiter must not brick PR publishing; high-sensitivity deployments
  flip ``fail_open=False``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.ports.audit_sink import AuditSink
from meta_agent.core.ports.git_provider import (
    GitProvider,
    GitProviderTransientError,
    PullRequestRef,
)
from meta_agent.core.ports.rate_limiter import RateLimiter, RateLimiterBackendError
from meta_agent.infra.security.context import RequestContext, get_current

logger = logging.getLogger(__name__)

_AUDIT_ACTION_DENIED = "git.rate_limited.denied"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _default_event_id() -> str:
    return f"ae-{uuid.uuid4()}"


class RateLimitedGitProvider(GitProvider):
    """Decorator that consults a :class:`RateLimiter` before delegating.

    Parameters
    ----------
    inner:
        The wrapped :class:`GitProvider`. Typically a
        :class:`CircuitBreakingGitProvider` in production.
    limiter:
        Backend used to gate calls. Production wiring injects the same
        Redis-backed limiter the LLM stack uses; tests pass an
        in-memory one.
    provider:
        Logical provider name embedded in the bucket key (``github``
        today; future ``gitlab`` etc).
    fail_open:
        On :class:`RateLimiterBackendError`, forward the call instead
        of raising. Default ``True``.
    key_factory:
        Optional override; tests can inject a deterministic factory.
        Production code should leave this ``None``.
    audit_sink:
        If provided, deny outcomes append a ``git.rate_limited.denied``
        :class:`AuditEvent`. Audit-write failures are swallowed (warn
        log only); the limiter decision is not affected.
    """

    def __init__(
        self,
        inner: GitProvider,
        limiter: RateLimiter,
        *,
        provider: str,
        fail_open: bool = True,
        key_factory: Callable[[str, str], str] | None = None,
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
        try:
            decision = await self._limiter.acquire(key)
        except RateLimiterBackendError as exc:
            if not self._fail_open:
                raise
            logger.warning(
                "git.rate_limited.backend_error_fail_open",
                extra={
                    "tenant_id": tenant_id,
                    "trace_id": trace_id,
                    "provider": self._provider,
                    "repo_url": repo_url,
                    "error_type": type(exc).__name__,
                },
            )
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

        if not decision.allowed:
            ctx = get_current()
            logger.info(
                "git.rate_limited.denied",
                extra={
                    "tenant_id": tenant_id,
                    "trace_id": trace_id,
                    "task_id": ctx.task_id if ctx is not None else None,
                    "provider": self._provider,
                    "repo_url": repo_url,
                    "retry_after_ms": decision.retry_after_ms,
                    "remaining": decision.remaining,
                },
            )
            await self._audit_deny(
                ctx=ctx,
                tenant_id=tenant_id,
                trace_id=trace_id,
                repo_url=repo_url,
                key=key,
                retry_after_ms=decision.retry_after_ms,
                remaining=decision.remaining,
            )
            raise GitProviderTransientError(
                f"rate limit exceeded for {self._provider} repo={repo_url}"
            )

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

    async def close(self) -> None:
        await self._inner.close()

    def _default_key(self, tenant_id: str, repo_url: str) -> str:
        return f"git:{self._provider}:tenant={tenant_id}:repo={repo_url}"

    async def _audit_deny(
        self,
        *,
        ctx: RequestContext | None,
        tenant_id: str,
        trace_id: str,
        repo_url: str,
        key: str,
        retry_after_ms: int | None,
        remaining: int | None,
    ) -> None:
        if self._audit_sink is None:
            return
        if ctx is None:
            # AuditEvent requires principal_id; emit nothing rather than
            # fabricate identifiers when running outside a bound context.
            logger.debug(
                "git.rate_limited.audit_skip_no_context",
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
            action=_AUDIT_ACTION_DENIED,
            payload={
                "provider": self._provider,
                "repo_url": repo_url,
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
                "git.rate_limited.audit_append_failed",
                extra={
                    "tenant_id": tenant_id,
                    "trace_id": trace_id,
                    "provider": self._provider,
                    "repo_url": repo_url,
                    "error_type": type(exc).__name__,
                },
            )


__all__ = ["RateLimitedGitProvider"]
