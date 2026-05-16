"""Unit tests for the built-in ``builtin.bug_fix`` graph.

All tests stay unit-scoped: ``FakeLLMClient`` replaces the LLM port,
and a ``tmp_path`` git repo plays the role of the worktree the worker
would normally provision. ``ruff`` is invoked as a subprocess because
it is a build-time dependency, not an external service.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from meta_agent.core.orchestration import END, GraphError, TaskRunState
from meta_agent.core.orchestration.graphs.bug_fix import (
    BUG_FIX_GRAPH_ID,
    build_bug_fix_graph,
)
from meta_agent.core.ports.llm import LLMRequest, LLMResponse
from tests.core.orchestration._fakes import FakeLLMClient, fake_deps, make_response


def _run(*args: str) -> None:
    subprocess.run(args, check=True, capture_output=True)


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    """A fresh repo containing one ruff-clean Python file plus a marker file."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _run("git", "init", "--initial-branch=main", str(repo))
    _run("git", "-C", str(repo), "config", "user.email", "t@example.com")
    _run("git", "-C", str(repo), "config", "user.name", "test")
    (repo / "buggy.py").write_text('def greet(name):\n    return "hi " + name\n')
    (repo / "README.md").write_text("# tiny\n")
    _run("git", "-C", str(repo), "add", ".")
    _run("git", "-C", str(repo), "commit", "-m", "initial")
    _run("git", "-C", str(repo), "checkout", "-b", "agent/task-1")
    return repo


def _state(
    repo: Path,
    *,
    issue: str = "greet should add a punctuation mark",
    targets: list[str] | None = None,
    extra: dict[str, object] | None = None,
) -> TaskRunState:
    data: dict[str, object] = {
        "issue_description": issue,
        "target_files": targets if targets is not None else ["buggy.py"],
        "_workspace_path": str(repo),
        "_workspace_branch": "agent/task-1",
    }
    if extra:
        data.update(extra)
    return TaskRunState(
        task_id="task-1",
        tenant_id="tenant-1",
        trace_id="trace-1",
        graph_id=BUG_FIX_GRAPH_ID,
        data=data,
    )


def _two_step_handler(
    *, plan_text: str, patch_payload: dict[str, object] | str
) -> Callable[[LLMRequest], LLMResponse]:
    """Return a handler that branches on the system prompt's role."""

    body = patch_payload if isinstance(patch_payload, str) else json.dumps(patch_payload)

    def handler(request: LLMRequest) -> LLMResponse:
        system = request.messages[0].content
        if "code patcher" in system:
            return make_response(content=body)
        return make_response(content=plan_text)

    return handler


async def test_happy_path_writes_patch_and_verifies(tiny_repo: Path) -> None:
    patched = 'def greet(name: str) -> str:\n    return f"hi {name}!"\n'
    client = FakeLLMClient(
        handler=_two_step_handler(
            plan_text="add exclamation, annotate types",
            patch_payload={"files": [{"path": "buggy.py", "content": patched}]},
        )
    )
    graph = build_bug_fix_graph(fake_deps(client))

    final = await graph.run(_state(tiny_repo))

    assert final.current_node == END
    assert final.finished is True
    assert final.error is None
    output = final.data["output"]
    assert isinstance(output, dict)
    assert output["branch"] == "agent/task-1"
    assert output["files_changed"] == ["buggy.py"]
    assert isinstance(output["commit_sha"], str) and len(output["commit_sha"]) >= 7
    assert "buggy.py" in output["diff_stat"]
    assert output["verifier_passed"] is True
    assert output["verifier_output"] == ""
    # File was actually rewritten on disk inside the worktree.
    assert (tiny_repo / "buggy.py").read_text() == patched


async def test_patch_outside_allow_list_is_rejected(tiny_repo: Path) -> None:
    client = FakeLLMClient(
        handler=_two_step_handler(
            plan_text="plan",
            patch_payload={"files": [{"path": "README.md", "content": "# evil\n"}]},
        )
    )
    graph = build_bug_fix_graph(fake_deps(client))

    with pytest.raises(GraphError, match="not in target_files allow-list"):
        await graph.run(_state(tiny_repo, targets=["buggy.py"]))


async def test_patch_response_must_be_valid_json(tiny_repo: Path) -> None:
    client = FakeLLMClient(
        handler=_two_step_handler(
            plan_text="plan",
            patch_payload="not json at all",
        )
    )
    graph = build_bug_fix_graph(fake_deps(client))

    with pytest.raises(GraphError, match="not valid JSON"):
        await graph.run(_state(tiny_repo))


async def test_patch_entry_exceeding_size_limit_is_rejected(tiny_repo: Path) -> None:
    huge = "x = 1\n" * 5000  # ~30 KiB, above the 10 KiB cap
    client = FakeLLMClient(
        handler=_two_step_handler(
            plan_text="plan",
            patch_payload={"files": [{"path": "buggy.py", "content": huge}]},
        )
    )
    graph = build_bug_fix_graph(fake_deps(client))

    with pytest.raises(GraphError, match="exceeds max_file_bytes"):
        await graph.run(_state(tiny_repo))


async def test_too_many_files_in_patch_is_rejected(tiny_repo: Path) -> None:
    targets = ["a.py", "b.py", "c.py", "d.py"]  # 4 > _MAX_FILES (3)
    client = FakeLLMClient(handler=_two_step_handler(plan_text="p", patch_payload={}))
    graph = build_bug_fix_graph(fake_deps(client))

    with pytest.raises(GraphError, match="target_files exceeds max_files"):
        await graph.run(_state(tiny_repo, targets=targets))


async def test_empty_diff_marks_verifier_failed_but_task_succeeds(tiny_repo: Path) -> None:
    """LLM returns the exact current content: nothing to commit; succeed with verifier_passed=False."""

    current = (tiny_repo / "buggy.py").read_text()
    client = FakeLLMClient(
        handler=_two_step_handler(
            plan_text="no real change",
            patch_payload={"files": [{"path": "buggy.py", "content": current}]},
        )
    )
    graph = build_bug_fix_graph(fake_deps(client))

    final = await graph.run(_state(tiny_repo))

    assert final.finished is True
    assert final.error is None  # task is SUCCEEDED at the worker level
    output = final.data["output"]
    assert isinstance(output, dict)
    assert output["files_changed"] == []
    assert output["commit_sha"] is None
    assert output["verifier_passed"] is False
    assert "empty diff" in output["verifier_output"]


async def test_verifier_failure_reports_ruff_output_without_task_failure(
    tiny_repo: Path,
) -> None:
    # Introduce a ruff violation: undefined name `undef` (F821).
    broken = "def greet(name):\n    return undef + name\n"
    client = FakeLLMClient(
        handler=_two_step_handler(
            plan_text="break it",
            patch_payload={"files": [{"path": "buggy.py", "content": broken}]},
        )
    )
    graph = build_bug_fix_graph(fake_deps(client))

    final = await graph.run(_state(tiny_repo))

    assert final.finished is True
    assert final.error is None
    output = final.data["output"]
    assert isinstance(output, dict)
    assert output["files_changed"] == ["buggy.py"]
    assert output["verifier_passed"] is False
    # ruff emits the rule code (F821) for undefined names.
    assert "F821" in output["verifier_output"]


async def test_missing_workspace_path_raises(tiny_repo: Path) -> None:
    graph = build_bug_fix_graph(fake_deps(FakeLLMClient()))
    bad = TaskRunState(
        task_id="task-1",
        tenant_id="tenant-1",
        trace_id="trace-1",
        graph_id=BUG_FIX_GRAPH_ID,
        data={
            "issue_description": "x",
            "target_files": ["buggy.py"],
            "_workspace_branch": "agent/task-1",
            # _workspace_path intentionally missing
        },
    )

    with pytest.raises(GraphError, match="_workspace_path"):
        await graph.run(bad)


async def test_target_files_must_be_repo_relative(tiny_repo: Path) -> None:
    graph = build_bug_fix_graph(fake_deps(FakeLLMClient()))

    with pytest.raises(GraphError, match="must be repo-relative"):
        await graph.run(_state(tiny_repo, targets=["/etc/passwd"]))

    with pytest.raises(GraphError, match="must be repo-relative"):
        await graph.run(_state(tiny_repo, targets=["../escape.py"]))
