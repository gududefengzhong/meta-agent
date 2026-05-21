"""Unit tests for :class:`RateLimitedGitProvider`.

Cover the four observable behaviours:

* allow path forwards to inner unchanged and computes the right key
* deny path raises :class:`GitProviderTransientError` and emits a
  ``git.rate_limited.denied`` audit event
* :class:`RateLimiterBackendError` fails open by default (and
  propagates when ``fail_open=False``)
* key derivation embeds ``tenant_id`` and ``repo_url`` so multi-tenant
  buckets stay isolated
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.ports.audit_sink import AuditSink
from meta_agent.core.ports.git_provider import (
    GitProvider,
    GitProviderTransientError,
    PullRequestRef,
)
from meta_agent.core.ports.rate_limiter import (
    RateLimitDecision,
    RateLimiter,
    RateLimiterBackendError,
)
from meta_agent.infra.git_provider.rate_limited import RateLimitedGitProvider
from meta_agent.infra.security.context import RequestContext, bind_context


class _RecordingAuditSink(AuditSink):
    def __init__(self, *, raise_on_append: BaseException | None = None) -> None:
        self.events: list[AuditEvent] = []
        self._raise = raise_on_append

    async def append(self, event: AuditEvent) -> None:
        if self._raise is not None:
            raise self._raise
        self.events.append(event)


class _ScriptedLimiter(RateLimiter):
    def __init__(
        self,
        *,
        outcomes: list[RateLimitDecision | RateLimiterBackendError] | None = None,
        default: RateLimitDecision | None = None,
    ) -> None:
        self._outcomes = list(outcomes or [])
        self._default = default or RateLimitDecision(allowed=True, remaining=999)
        self.keys: list[str] = []
        self.closed = False

    async def acquire(self, key: str, *, cost: int = 1) -> RateLimitDecision:
        self.keys.append(key)
        if not self._outcomes:
            return self._default
        nxt = self._outcomes.pop(0)
        if isinstance(nxt, RateLimiterBackendError):
            raise nxt
        return nxt

    async def close(self) -> None:
        self.closed = True


class _RecordingGitProvider(GitProvider):
    """Records every call; returns a deterministic PullRequestRef."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.closed = False

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
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "trace_id": trace_id,
                "repo_url": repo_url,
                "head_branch": head_branch,
                "head_commit_sha": head_commit_sha,
            }
        )
        return PullRequestRef(
            provider="recorder",
            pr_id="pr-1",
            url="https://example.test/pr/1",
            action="created",
            head_branch=head_branch,
            base_ref=base_ref,
            head_commit_sha=head_commit_sha,
        )

    async def close(self) -> None:
        self.closed = True


def _ctx(tenant_id: str = "t-1") -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id,
        principal_id="p-1",
        request_id="req-1",
        trace_id="trace-1",
        task_id="task-1",
        session_id="sess-1",
    )


def _kwargs(**overrides: str) -> dict[str, str]:
    base = {
        "tenant_id": "t-1",
        "trace_id": "trace-1",
        "repo_url": "https://github.com/acme/widget",
        "base_ref": "main",
        "head_branch": "fix/issue-42",
        "head_commit_sha": "deadbeef0123",
        "title": "Fix",
        "body": "body",
    }
    base.update(overrides)
    return base


async def test_allow_path_forwards_to_inner_and_uses_expected_key() -> None:
    inner = _RecordingGitProvider()
    limiter = _ScriptedLimiter()
    wrapped = RateLimitedGitProvider(inner, limiter, provider="github")
    ref = await wrapped.open_or_reuse_pr(**_kwargs())
    assert ref.action == "created"
    assert inner.calls == [
        {
            "tenant_id": "t-1",
            "trace_id": "trace-1",
            "repo_url": "https://github.com/acme/widget",
            "head_branch": "fix/issue-42",
            "head_commit_sha": "deadbeef0123",
        }
    ]
    assert limiter.keys == ["git:github:tenant=t-1:repo=https://github.com/acme/widget"]


async def test_deny_raises_transient_and_does_not_call_inner() -> None:
    inner = _RecordingGitProvider()
    limiter = _ScriptedLimiter(
        outcomes=[RateLimitDecision(allowed=False, remaining=0, retry_after_ms=500)],
    )
    wrapped = RateLimitedGitProvider(inner, limiter, provider="github")
    with pytest.raises(GitProviderTransientError, match="rate limit exceeded"):
        await wrapped.open_or_reuse_pr(**_kwargs())
    assert inner.calls == []


async def test_deny_emits_audit_event_with_context() -> None:
    inner = _RecordingGitProvider()
    limiter = _ScriptedLimiter(
        outcomes=[RateLimitDecision(allowed=False, remaining=0, retry_after_ms=750)],
    )
    sink = _RecordingAuditSink()
    fixed = datetime(2025, 1, 1, tzinfo=UTC)
    wrapped = RateLimitedGitProvider(
        inner,
        limiter,
        provider="github",
        audit_sink=sink,
        clock=lambda: fixed,
        event_id_factory=lambda: "ae-fixed",
    )
    with bind_context(_ctx()), pytest.raises(GitProviderTransientError):
        await wrapped.open_or_reuse_pr(**_kwargs())
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.event_id == "ae-fixed"
    assert event.tenant_id == "t-1"
    assert event.principal_id == "p-1"
    assert event.trace_id == "trace-1"
    assert event.task_id == "task-1"
    assert event.action == "git.rate_limited.denied"
    assert event.occurred_at == fixed
    assert event.payload["provider"] == "github"
    assert event.payload["repo_url"] == "https://github.com/acme/widget"
    assert event.payload["retry_after_ms"] == 750
    assert event.payload["remaining"] == 0


async def test_deny_without_bound_context_skips_audit() -> None:
    inner = _RecordingGitProvider()
    limiter = _ScriptedLimiter(
        outcomes=[RateLimitDecision(allowed=False, remaining=0)],
    )
    sink = _RecordingAuditSink()
    wrapped = RateLimitedGitProvider(inner, limiter, provider="github", audit_sink=sink)
    with pytest.raises(GitProviderTransientError):
        await wrapped.open_or_reuse_pr(**_kwargs())
    assert sink.events == []


async def test_audit_append_failure_does_not_mask_deny(caplog: pytest.LogCaptureFixture) -> None:
    inner = _RecordingGitProvider()
    limiter = _ScriptedLimiter(outcomes=[RateLimitDecision(allowed=False, remaining=0)])
    sink = _RecordingAuditSink(raise_on_append=RuntimeError("audit-down"))
    wrapped = RateLimitedGitProvider(inner, limiter, provider="github", audit_sink=sink)
    with (
        bind_context(_ctx()),
        caplog.at_level(logging.WARNING),
        pytest.raises(GitProviderTransientError),
    ):
        await wrapped.open_or_reuse_pr(**_kwargs())
    assert any("git.rate_limited.audit_append_failed" in r.message for r in caplog.records)


async def test_backend_error_fail_open_forwards_to_inner() -> None:
    inner = _RecordingGitProvider()
    limiter = _ScriptedLimiter(outcomes=[RateLimiterBackendError("redis-down")])
    wrapped = RateLimitedGitProvider(inner, limiter, provider="github")
    ref = await wrapped.open_or_reuse_pr(**_kwargs())
    assert ref.action == "created"
    assert len(inner.calls) == 1


async def test_backend_error_fail_closed_propagates() -> None:
    inner = _RecordingGitProvider()
    limiter = _ScriptedLimiter(outcomes=[RateLimiterBackendError("redis-down")])
    wrapped = RateLimitedGitProvider(inner, limiter, provider="github", fail_open=False)
    with pytest.raises(RateLimiterBackendError):
        await wrapped.open_or_reuse_pr(**_kwargs())
    assert inner.calls == []


async def test_keys_are_tenant_and_repo_scoped() -> None:
    inner = _RecordingGitProvider()
    limiter = _ScriptedLimiter()
    wrapped = RateLimitedGitProvider(inner, limiter, provider="github")
    await wrapped.open_or_reuse_pr(**_kwargs(tenant_id="t-1"))
    await wrapped.open_or_reuse_pr(**_kwargs(tenant_id="t-2"))
    await wrapped.open_or_reuse_pr(
        **_kwargs(tenant_id="t-1", repo_url="https://github.com/acme/other")
    )
    assert limiter.keys == [
        "git:github:tenant=t-1:repo=https://github.com/acme/widget",
        "git:github:tenant=t-2:repo=https://github.com/acme/widget",
        "git:github:tenant=t-1:repo=https://github.com/acme/other",
    ]


async def test_close_delegates_to_inner() -> None:
    inner = _RecordingGitProvider()
    limiter = _ScriptedLimiter()
    wrapped = RateLimitedGitProvider(inner, limiter, provider="github")
    await wrapped.close()
    assert inner.closed is True


def test_construction_rejects_empty_provider() -> None:
    with pytest.raises(ValueError, match="provider must be a non-empty string"):
        RateLimitedGitProvider(_RecordingGitProvider(), _ScriptedLimiter(), provider="")
