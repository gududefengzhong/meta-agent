"""Unit tests for the built-in ``builtin.auto_pr`` graph.

Every test drives a :class:`FakeGitProvider` so the graph is exercised
end-to-end through node execution without touching any network. The
tests focus on three contract surfaces:

* deterministic title / body rendering from upstream task output
* the three skip rules in :data:`SkipReason` and their Scheme-X result
* created vs reused PR semantics through the provider key
"""

from __future__ import annotations

import pytest

from meta_agent.core.orchestration import END, GraphError, TaskRunState
from meta_agent.core.orchestration.deps import GraphDeps
from meta_agent.core.orchestration.graphs.auto_pr import (
    _MAX_BODY_CHARS,
    _MAX_VERIFIER_OUTPUT_CHARS,
    AUTO_PR_GRAPH_ID,
    build_auto_pr_graph,
)
from meta_agent.infra.git_provider import FakeGitProvider
from tests.core.orchestration._fakes import FakeLLMClient


class _GitHubNamedFakeProvider(FakeGitProvider):
    PROVIDER_NAME = "github"


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "repo_url": "https://example.test/acme/widget.git",
        "base_ref": "main",
        "head_branch": "fix/issue-42",
        "head_commit_sha": "deadbeef0123",
        "issue_title": "Greet drops the comma",
        "issue_description": "The comma was lost in #42.",
        "diff_stat": " greet.py | 2 +-\n 1 file changed",
        "verifier_passed": True,
        "verifier_output": "ruff: ok\npytest: 1 passed",
    }
    base.update(overrides)
    return base


def _state(
    *,
    tenant_id: str = "tenant-1",
    data: dict[str, object] | None = None,
) -> TaskRunState:
    return TaskRunState(
        task_id="task-1",
        tenant_id=tenant_id,
        trace_id="trace-1",
        graph_id=AUTO_PR_GRAPH_ID,
        data=data if data is not None else _payload(),
    )


def _deps(provider: FakeGitProvider | None = None) -> GraphDeps:
    return GraphDeps(
        llm=FakeLLMClient(),
        git_provider=provider if provider is not None else FakeGitProvider(),
    )


async def test_happy_path_creates_pr_with_rendered_title_and_body() -> None:
    provider = FakeGitProvider(id_factory=lambda: "pr-001")
    graph = build_auto_pr_graph(_deps(provider))
    state = await graph.run(_state())
    assert state.finished and state.current_node == END
    out = state.data["output"]
    assert isinstance(out, dict)
    assert out["action"] == "created"
    assert out["provider"] == "fake"
    assert out["pr_id"] == "pr-001"
    assert out["pr_ref"] == "fake://fake/tenant-1/pr-001"
    assert out["reason"] is None
    assert out["title"] == "Fix: Greet drops the comma"
    assert "## Greet drops the comma" in out["body"]
    assert "fix/issue-42" in out["body"]
    assert "deadbeef0123" in out["body"]
    assert "ruff: ok" in out["body"]


async def test_pr_title_override_wins_over_default_template() -> None:
    graph = build_auto_pr_graph(_deps())
    state = await graph.run(_state(data=_payload(pr_title_override="chore: punctuation polish")))
    assert state.data["output"]["title"] == "chore: punctuation polish"  # type: ignore[index]


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"repo_url": None}, "no_repo_url"),
        ({"head_commit_sha": None}, "no_commit_sha"),
        ({"verifier_passed": False}, "verifier_failed"),
    ],
)
async def test_skip_rules_succeed_under_scheme_x(override: dict[str, object], reason: str) -> None:
    provider = FakeGitProvider()
    graph = build_auto_pr_graph(_deps(provider))
    state = await graph.run(_state(data=_payload(**override)))
    out = state.data["output"]
    assert isinstance(out, dict)
    assert out["action"] == "skipped"
    assert out["reason"] == reason
    assert out["pr_ref"] is None
    assert out["pr_id"] is None
    assert provider.calls == []


async def test_skip_path_reports_configured_provider_not_hard_coded_fake() -> None:
    provider = _GitHubNamedFakeProvider()
    graph = build_auto_pr_graph(_deps(provider))
    state = await graph.run(_state(data=_payload(verifier_passed=False)))
    out = state.data["output"]
    assert isinstance(out, dict)
    assert out["action"] == "skipped"
    assert out["provider"] == "github"


async def test_reuse_returns_same_pr_for_same_commit() -> None:
    provider = FakeGitProvider(id_factory=lambda: "pr-stable")
    graph = build_auto_pr_graph(_deps(provider))
    first = await graph.run(_state())
    second = await graph.run(_state())
    assert first.data["output"]["pr_id"] == "pr-stable"  # type: ignore[index]
    assert second.data["output"]["action"] == "reused"  # type: ignore[index]
    assert second.data["output"]["pr_id"] == "pr-stable"  # type: ignore[index]
    assert len(provider.calls) == 2


async def test_missing_git_provider_raises_graph_error() -> None:
    graph = build_auto_pr_graph(GraphDeps(llm=FakeLLMClient()))
    with pytest.raises(GraphError, match="git_provider is required"):
        await graph.run(_state())


async def test_missing_required_field_raises() -> None:
    graph = build_auto_pr_graph(_deps())
    bad = _payload()
    del bad["head_branch"]
    with pytest.raises(GraphError, match="head_branch"):
        await graph.run(_state(data=bad))


async def test_body_truncates_very_large_verifier_output() -> None:
    big = "x" * (_MAX_VERIFIER_OUTPUT_CHARS * 4)
    graph = build_auto_pr_graph(_deps())
    state = await graph.run(_state(data=_payload(verifier_output=big)))
    body = state.data["output"]["body"]  # type: ignore[index]
    assert isinstance(body, str)
    assert len(body) <= _MAX_BODY_CHARS
    assert "[truncated]" in body
