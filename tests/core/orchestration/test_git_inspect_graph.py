"""Unit tests for the built-in git-inspect graph.

The graph shells out to the local ``git`` binary against a fixture
repo set up under ``tmp_path``. These tests stay unit-scoped because
``git`` is a build-time dependency, not an external service.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from meta_agent.core.orchestration import END, TaskRunState
from meta_agent.core.orchestration.graph import GraphError
from meta_agent.core.orchestration.graphs import (
    GIT_INSPECT_GRAPH_ID,
    build_git_inspect_graph,
)


def _run(*args: str) -> None:
    subprocess.run(args, check=True, capture_output=True)


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    """A small repo with three commits and a ``feature`` branch."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _run("git", "init", "--initial-branch=main", str(repo))
    _run("git", "-C", str(repo), "config", "user.email", "t@example.com")
    _run("git", "-C", str(repo), "config", "user.name", "test")
    for i in range(3):
        (repo / f"f{i}.txt").write_text(f"v{i}\n")
        _run("git", "-C", str(repo), "add", ".")
        _run("git", "-C", str(repo), "commit", "-m", f"c{i}")
    _run("git", "-C", str(repo), "checkout", "-b", "agent/task-1")
    return repo


def _state(workspace_path: str | None, branch: str | None = "agent/task-1") -> TaskRunState:
    data: dict[str, object] = {}
    if workspace_path is not None:
        data["_workspace_path"] = workspace_path
    if branch is not None:
        data["_workspace_branch"] = branch
    return TaskRunState(
        task_id="task-1",
        tenant_id="t-1",
        trace_id="trace-1",
        graph_id=GIT_INSPECT_GRAPH_ID,
        data=data,
    )


async def test_git_inspect_collects_recent_commits(tiny_repo: Path) -> None:
    g = build_git_inspect_graph()
    final = await g.run(_state(str(tiny_repo)))
    assert final.current_node == END
    assert final.finished is True
    output = final.data["output"]
    assert isinstance(output, dict)
    assert output["branch"] == "agent/task-1"
    assert output["commit_count"] == 3
    # Most recent commit first; subject line is ``c2``.
    assert isinstance(output["head"], str) and output["head"].endswith(" c2")
    log = output["log"]
    assert isinstance(log, list) and len(log) == 3


async def test_git_inspect_rejects_missing_workspace_path() -> None:
    g = build_git_inspect_graph()
    with pytest.raises(GraphError, match="_workspace_path missing"):
        await g.run(_state(workspace_path=None))


async def test_git_inspect_surfaces_git_failure(tmp_path: Path) -> None:
    # ``tmp_path`` is not a git repo; ``git log`` will exit non-zero.
    g = build_git_inspect_graph()
    with pytest.raises(GraphError, match="git log failed"):
        await g.run(_state(str(tmp_path)))
