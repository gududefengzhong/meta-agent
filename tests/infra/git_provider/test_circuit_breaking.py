"""Unit tests for :class:`CircuitBreakingGitProvider`.

Cover the observable wrapper behaviours:

* allow path forwards to inner unchanged and computes the right key
* :class:`CircuitBreakerOpenError` is translated to
  :class:`GitProviderTransientError` (port contract preserved)
* ``open`` emits a ``git.circuit_breaker.open`` audit event under a
  bound context, and is skipped when no context is bound
* :class:`CircuitBreakerBackendError` fails open (default) or
  propagates when ``fail_open=False``
* ``should_count`` default excludes caller-side git errors so they do
  not trip the breaker
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TypeVar

import pytest

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
    GitProviderError,
    GitProviderInvalidRequestError,
    GitProviderTransientError,
    PullRequestRef,
)
from meta_agent.infra.git_provider.circuit_breaking import (
    CircuitBreakingGitProvider,
    _default_should_count,
)
from meta_agent.infra.security.context import RequestContext, bind_context

T = TypeVar("T")


class _ScriptedBreaker(CircuitBreaker):
    def __init__(
        self,
        *,
        outcomes: list[CircuitBreakerOpenError | CircuitBreakerBackendError] | None = None,
    ) -> None:
        self._outcomes = list(outcomes or [])
        self.keys: list[str] = []
        self.predicates: list[Callable[[BaseException], bool] | None] = []
        self.closed = False

    async def call(
        self,
        key: str,
        fn: Callable[[], Awaitable[T]],
        *,
        should_count: Callable[[BaseException], bool] | None = None,
    ) -> T:
        self.keys.append(key)
        self.predicates.append(should_count)
        if self._outcomes:
            raise self._outcomes.pop(0)
        return await fn()

    async def close(self) -> None:
        self.closed = True


class _RecordingAuditSink(AuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> None:
        self.events.append(event)


class _RecordingGitProvider(GitProvider):
    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.calls: list[dict[str, str]] = []
        self.closed = False
        self._raises = raises

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
        self.calls.append({"tenant_id": tenant_id, "repo_url": repo_url})
        if self._raises is not None:
            raise self._raises
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
    breaker = _ScriptedBreaker()
    wrapped = CircuitBreakingGitProvider(inner, breaker, provider="github")
    ref = await wrapped.open_or_reuse_pr(**_kwargs())
    assert ref.action == "created"
    assert breaker.keys == ["git:github:tenant=t-1:repo=https://github.com/acme/widget"]


async def test_open_error_translated_to_transient_and_audited() -> None:
    inner = _RecordingGitProvider()
    breaker = _ScriptedBreaker(
        outcomes=[CircuitBreakerOpenError("open", key="git:github:k", retry_after_ms=2500)]
    )
    sink = _RecordingAuditSink()
    fixed = datetime(2025, 1, 1, tzinfo=UTC)
    wrapped = CircuitBreakingGitProvider(
        inner,
        breaker,
        provider="github",
        audit_sink=sink,
        clock=lambda: fixed,
        event_id_factory=lambda: "ae-fixed",
    )
    with bind_context(_ctx()), pytest.raises(GitProviderTransientError, match="circuit breaker"):
        await wrapped.open_or_reuse_pr(**_kwargs())
    assert inner.calls == []
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.action == "git.circuit_breaker.open"
    assert event.tenant_id == "t-1"
    assert event.principal_id == "p-1"
    assert event.payload["provider"] == "github"
    assert event.payload["repo_url"] == "https://github.com/acme/widget"
    assert event.payload["key"] == "git:github:k"
    assert event.payload["retry_after_ms"] == 2500
    assert event.occurred_at == fixed
    assert event.event_id == "ae-fixed"


async def test_open_without_bound_context_skips_audit() -> None:
    inner = _RecordingGitProvider()
    breaker = _ScriptedBreaker(
        outcomes=[CircuitBreakerOpenError("open", key="git:github:k", retry_after_ms=100)]
    )
    sink = _RecordingAuditSink()
    wrapped = CircuitBreakingGitProvider(inner, breaker, provider="github", audit_sink=sink)
    with pytest.raises(GitProviderTransientError):
        await wrapped.open_or_reuse_pr(**_kwargs())
    assert sink.events == []


async def test_backend_error_fail_open_forwards_to_inner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    inner = _RecordingGitProvider()
    breaker = _ScriptedBreaker(outcomes=[CircuitBreakerBackendError("redis-down")])
    wrapped = CircuitBreakingGitProvider(inner, breaker, provider="github")
    with caplog.at_level(logging.WARNING):
        ref = await wrapped.open_or_reuse_pr(**_kwargs())
    assert ref.action == "created"
    assert len(inner.calls) == 1
    assert any("circuit_breaker.backend_error_fail_open" in r.message for r in caplog.records)


async def test_backend_error_fail_closed_propagates() -> None:
    inner = _RecordingGitProvider()
    breaker = _ScriptedBreaker(outcomes=[CircuitBreakerBackendError("redis-down")])
    wrapped = CircuitBreakingGitProvider(inner, breaker, provider="github", fail_open=False)
    with pytest.raises(CircuitBreakerBackendError):
        await wrapped.open_or_reuse_pr(**_kwargs())
    assert inner.calls == []


async def test_default_should_count_excludes_caller_side_errors() -> None:
    assert _default_should_count(GitProviderAuthError("revoked")) is False
    assert _default_should_count(GitProviderInvalidRequestError("bad url")) is False
    assert _default_should_count(GitProviderTransientError("5xx")) is True
    assert _default_should_count(GitProviderError("other")) is True
    assert _default_should_count(RuntimeError("boom")) is True


async def test_should_count_predicate_passed_to_breaker() -> None:
    inner = _RecordingGitProvider()
    breaker = _ScriptedBreaker()
    wrapped = CircuitBreakingGitProvider(inner, breaker, provider="github")
    await wrapped.open_or_reuse_pr(**_kwargs())
    assert len(breaker.predicates) == 1
    predicate = breaker.predicates[0]
    assert predicate is not None
    assert predicate(GitProviderAuthError("x")) is False
    assert predicate(GitProviderTransientError("x")) is True


async def test_custom_should_count_overrides_default() -> None:
    inner = _RecordingGitProvider()
    breaker = _ScriptedBreaker()

    def _always_false(_exc: BaseException) -> bool:
        return False

    wrapped = CircuitBreakingGitProvider(
        inner, breaker, provider="github", should_count=_always_false
    )
    await wrapped.open_or_reuse_pr(**_kwargs())
    predicate = breaker.predicates[0]
    assert predicate is _always_false


async def test_close_delegates_to_inner() -> None:
    inner = _RecordingGitProvider()
    breaker = _ScriptedBreaker()
    wrapped = CircuitBreakingGitProvider(inner, breaker, provider="github")
    await wrapped.close()
    assert inner.closed is True


def test_construction_rejects_empty_provider() -> None:
    with pytest.raises(ValueError, match="provider must be a non-empty string"):
        CircuitBreakingGitProvider(_RecordingGitProvider(), _ScriptedBreaker(), provider="")
