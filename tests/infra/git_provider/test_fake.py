"""Unit tests for :class:`FakeGitProvider`.

These tests pin the dedup-key shape and cross-tenant isolation
invariants the future GitHub adapter must also satisfy.
"""

from __future__ import annotations

from meta_agent.infra.git_provider import FakeGitProvider


def _kwargs(**overrides: str) -> dict[str, str]:
    base: dict[str, str] = {
        "tenant_id": "tenant-1",
        "trace_id": "trace-1",
        "repo_url": "https://example.test/acme/widget.git",
        "base_ref": "main",
        "head_branch": "fix/issue-42",
        "head_commit_sha": "deadbeef0123",
        "title": "Fix: issue 42",
        "body": "body",
    }
    base.update(overrides)
    return base


async def test_first_call_creates_pr_with_injected_id() -> None:
    provider = FakeGitProvider(id_factory=lambda: "pr-stable")
    ref = await provider.open_or_reuse_pr(**_kwargs())
    assert ref.action == "created"
    assert ref.pr_id == "pr-stable"
    assert ref.url == "fake://fake/tenant-1/pr-stable"
    assert ref.head_branch == "fix/issue-42"
    assert ref.head_commit_sha == "deadbeef0123"


async def test_same_key_same_commit_returns_reused() -> None:
    provider = FakeGitProvider(id_factory=lambda: "pr-stable")
    first = await provider.open_or_reuse_pr(**_kwargs())
    second = await provider.open_or_reuse_pr(**_kwargs())
    assert first.action == "created"
    assert second.action == "reused"
    assert second.pr_id == first.pr_id
    assert second.url == first.url


async def test_same_key_new_commit_creates_new_pr() -> None:
    ids = iter(["pr-a", "pr-b"])
    provider = FakeGitProvider(id_factory=lambda: next(ids))
    first = await provider.open_or_reuse_pr(**_kwargs())
    second = await provider.open_or_reuse_pr(**_kwargs(head_commit_sha="cafebabe9876"))
    assert first.pr_id == "pr-a"
    assert second.pr_id == "pr-b"
    assert second.action == "created"


async def test_cross_tenant_isolation_does_not_dedup() -> None:
    ids = iter(["pr-a", "pr-b"])
    provider = FakeGitProvider(id_factory=lambda: next(ids))
    first = await provider.open_or_reuse_pr(**_kwargs(tenant_id="tenant-a"))
    second = await provider.open_or_reuse_pr(**_kwargs(tenant_id="tenant-b"))
    assert first.action == "created"
    assert second.action == "created"
    assert first.pr_id != second.pr_id
    assert first.url != second.url


async def test_different_branch_creates_independent_pr() -> None:
    ids = iter(["pr-a", "pr-b"])
    provider = FakeGitProvider(id_factory=lambda: next(ids))
    first = await provider.open_or_reuse_pr(**_kwargs(head_branch="fix/issue-42"))
    second = await provider.open_or_reuse_pr(**_kwargs(head_branch="fix/issue-43"))
    assert first.action == "created"
    assert second.action == "created"
    assert first.pr_id != second.pr_id


async def test_close_marks_provider_closed_and_is_idempotent() -> None:
    provider = FakeGitProvider()
    await provider.close()
    await provider.close()
    assert provider.closed is True
