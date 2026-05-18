"""Unit tests for :class:`GitHubGitProvider`.

The adapter is exercised against an :class:`httpx.MockTransport` so the
tests stay fast, deterministic, and never touch the network. A live
integration test against github.com is intentionally out of scope for
v1; it can land later under ``tests/live`` once a throwaway repo and
PAT are wired into the live test config.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from meta_agent.core.ports.git_provider import (
    GitProviderInvalidRequestError,
)
from meta_agent.infra.git_provider import GitHubGitProvider, GitHubGitProviderConfig


def _config() -> GitHubGitProviderConfig:
    return GitHubGitProviderConfig(
        token="ghp_test_token",
        base_url="https://api.github.com",
        max_retries=2,
        initial_backoff_seconds=0.0,
        max_backoff_seconds=0.0,
    )


def _kwargs(**overrides: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "tenant_id": "tenant-1",
        "trace_id": "trace-1",
        "repo_url": "https://github.com/acme/widget",
        "base_ref": "main",
        "head_branch": "fix/issue-42",
        "head_commit_sha": "deadbeef0123",
        "title": "Fix: issue 42",
        "body": "body",
    }
    base.update(overrides)
    return base


def _pr_payload(*, number: int, sha: str, branch: str = "fix/issue-42") -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com/acme/widget/pull/{number}",
        "head": {"sha": sha, "ref": branch},
        "base": {"ref": "main"},
        "state": "open",
    }


async def test_open_or_reuse_creates_pr_when_none_exists() -> None:
    """Happy path: search returns empty list → POST create → action=created."""

    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/repos/acme/widget/pulls":
            assert request.url.params.get("head") == "acme:fix/issue-42"
            assert request.url.params.get("state") == "open"
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == "/repos/acme/widget/pulls":
            return httpx.Response(201, json=_pr_payload(number=7, sha="deadbeef0123"))
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = GitHubGitProvider(_config(), transport=httpx.MockTransport(handler))
    try:
        ref = await provider.open_or_reuse_pr(**_kwargs())
    finally:
        await provider.close()

    assert ref.action == "created"
    assert ref.pr_id == "7"
    assert ref.url == "https://github.com/acme/widget/pull/7"
    assert ref.head_branch == "fix/issue-42"
    assert ref.base_ref == "main"
    assert ref.head_commit_sha == "deadbeef0123"
    assert calls == [
        ("GET", "/repos/acme/widget/pulls"),
        ("POST", "/repos/acme/widget/pulls"),
    ]


async def test_open_or_reuse_returns_reused_when_sha_matches() -> None:
    """Idempotency: existing open PR with same head sha → action=reused, no POST."""

    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/repos/acme/widget/pulls":
            return httpx.Response(200, json=[_pr_payload(number=11, sha="deadbeef0123")])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = GitHubGitProvider(_config(), transport=httpx.MockTransport(handler))
    try:
        ref = await provider.open_or_reuse_pr(**_kwargs())
    finally:
        await provider.close()

    assert ref.action == "reused"
    assert ref.pr_id == "11"
    assert ref.url == "https://github.com/acme/widget/pull/11"
    # The adapter must NOT call POST on a reuse path.
    assert all(method == "GET" for method, _ in calls)


async def test_open_or_reuse_raises_invalid_request_on_stale_head() -> None:
    """v1 has no update surface: existing PR with different sha → invalid request."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/repos/acme/widget/pulls":
            return httpx.Response(200, json=[_pr_payload(number=11, sha="cafef00d9999")])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = GitHubGitProvider(_config(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(GitProviderInvalidRequestError):
            await provider.open_or_reuse_pr(**_kwargs())
    finally:
        await provider.close()


async def test_open_or_reuse_retries_then_succeeds_on_transient_5xx() -> None:
    """Transport-level transient 503 must be retried and ultimately succeed."""

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    responses = iter(
        [
            httpx.Response(503, text="upstream busy"),
            httpx.Response(200, json=[]),
            httpx.Response(201, json=_pr_payload(number=42, sha="deadbeef0123")),
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    provider = GitHubGitProvider(
        _config(),
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
    )
    try:
        ref = await provider.open_or_reuse_pr(**_kwargs())
    finally:
        await provider.close()

    assert ref.action == "created"
    assert ref.pr_id == "42"
    # Exactly one backoff slept between the 503 and the retry that
    # returned 200; the search-then-create POST does not sleep.
    assert sleeps == [0.0]
