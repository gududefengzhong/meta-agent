"""Unit tests for the built-in ``builtin.bug_fix`` graph.

All tests stay unit-scoped: ``FakeLLMClient`` replaces the LLM port,
and a ``tmp_path`` git repo plays the role of the worktree the worker
would normally provision. ``ruff`` is invoked as a subprocess because
it is a build-time dependency, not an external service.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

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


def _scripted_handler(
    *,
    plan_texts: list[str],
    patch_payloads: list[dict[str, object] | str],
) -> Callable[[LLMRequest], LLMResponse]:
    """Return a handler that replies with the i-th canned response per role.

    Used by replan tests where the 1st patch must produce a *different*
    body than the 2nd, so the loop's inputs and outputs stay distinct.
    The handler clamps to the last entry once exhausted to avoid index
    errors if a node calls more times than expected.
    """

    bodies = [p if isinstance(p, str) else json.dumps(p) for p in patch_payloads]
    counters = {"plan": 0, "patch": 0}

    def handler(request: LLMRequest) -> LLMResponse:
        system = request.messages[0].content
        if "code patcher" in system:
            idx = min(counters["patch"], len(bodies) - 1)
            counters["patch"] += 1
            return make_response(content=bodies[idx])
        idx = min(counters["plan"], len(plan_texts) - 1)
        counters["plan"] += 1
        return make_response(content=plan_texts[idx])

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
    # Handoff fields populated even when no remote is configured; the
    # push node skips cleanly and downstream graphs can observe why.
    assert output["head_branch"] == "agent/task-1"
    assert output["head_commit_sha"] == output["commit_sha"]
    assert output["repo_url"] is None
    assert output["base_ref"] is None
    assert output["pushed"] is False
    assert output["push_skip_reason"] == "no_repo_url"
    # First verify passed; no replan happened.
    assert output["attempts"] == 1
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
    """LLM returns the exact current content: nothing to commit; succeed with verifier_passed=False.

    The empty-diff verifier failure triggers a replan, but a fixed
    handler keeps returning the same content, so the second attempt
    also produces an empty diff. The task still succeeds with
    ``verifier_passed=False`` and ``attempts=2``.
    """

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
    assert output["attempts"] == 2


async def test_verifier_failure_reports_ruff_output_without_task_failure(
    tiny_repo: Path,
) -> None:
    # Both attempts introduce a ruff F821 violation, but with different
    # symbol names so the 2nd patch is still a real diff vs the 1st
    # commit and ruff is invoked on a non-empty change set both times.
    broken_v1 = "def greet(name):\n    return undef + name\n"
    broken_v2 = "def greet(name):\n    return other_undef + name\n"
    client = FakeLLMClient(
        handler=_scripted_handler(
            plan_texts=["break it", "break it differently"],
            patch_payloads=[
                {"files": [{"path": "buggy.py", "content": broken_v1}]},
                {"files": [{"path": "buggy.py", "content": broken_v2}]},
            ],
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
    # Replan happened: the 1st verifier failure routed back to plan.
    assert output["attempts"] == 2


async def test_replan_succeeds_after_first_verify_failure(tiny_repo: Path) -> None:
    """1st patch fails ruff, replan + 2nd patch passes; feedback reaches LLM."""

    broken = "def greet(name):\n    return undef + name\n"
    fixed = 'def greet(name: str) -> str:\n    return f"hi {name}!"\n'
    fake = FakeLLMClient(
        handler=_scripted_handler(
            plan_texts=["initial plan", "refined plan after feedback"],
            patch_payloads=[
                {"files": [{"path": "buggy.py", "content": broken}]},
                {"files": [{"path": "buggy.py", "content": fixed}]},
            ],
        )
    )
    graph = build_bug_fix_graph(fake_deps(fake))

    final = await graph.run(_state(tiny_repo))

    assert final.finished is True
    assert final.error is None
    output = final.data["output"]
    assert isinstance(output, dict)
    assert output["verifier_passed"] is True
    assert output["verifier_output"] == ""
    assert output["attempts"] == 2
    assert output["files_changed"] == ["buggy.py"]
    # The 2nd plan call must carry verifier feedback from attempt 1.
    plan_calls = [c for c in fake.calls if "code repair agent" in c.messages[0].content]
    assert len(plan_calls) == 2
    second_plan_user_msg = plan_calls[1].messages[1].content
    assert "previous attempt failed verification" in second_plan_user_msg.lower()
    assert "F821" in second_plan_user_msg
    assert "initial plan" in second_plan_user_msg
    # File on disk reflects the final, fixed content.
    assert (tiny_repo / "buggy.py").read_text() == fixed


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


# ---------------------------------------------------------------------------
# ``push`` node coverage: skip × 3, happy, failure.
# ---------------------------------------------------------------------------


_PATCHED_BODY = 'def greet(name: str) -> str:\n    return f"hi {name}!"\n'


def _working_handler() -> Callable[[LLMRequest], LLMResponse]:
    return _two_step_handler(
        plan_text="add exclamation, annotate types",
        patch_payload={"files": [{"path": "buggy.py", "content": _PATCHED_BODY}]},
    )


@pytest.fixture
def tiny_repo_with_remote(tiny_repo: Path) -> tuple[Path, Path]:
    """``tiny_repo`` augmented with a sibling bare repo wired as ``origin``.

    Returning both paths lets tests assert on the remote independently
    (e.g. that the feature branch reached it).
    """

    remote = tiny_repo.parent / "remote.git"
    _run("git", "init", "--bare", "--initial-branch=main", str(remote))
    _run("git", "-C", str(tiny_repo), "remote", "add", "origin", str(remote))
    return tiny_repo, remote


async def test_push_skips_when_no_repo_url(tiny_repo: Path) -> None:
    client = FakeLLMClient(handler=_working_handler())
    graph = build_bug_fix_graph(fake_deps(client, git_push_token="ghp_secret"))

    final = await graph.run(_state(tiny_repo))

    output = final.data["output"]
    assert isinstance(output, dict)
    assert output["pushed"] is False
    assert output["push_skip_reason"] == "no_repo_url"
    assert output["verifier_passed"] is True


async def test_push_skips_when_verifier_failed(tiny_repo_with_remote: tuple[Path, Path]) -> None:
    repo, remote = tiny_repo_with_remote
    # Two distinct broken patches so the 2nd commit is non-empty and
    # ``push`` reaches the ``verifier_failed`` skip rather than the
    # ``no_commit`` short-circuit.
    broken_v1 = "def greet(name):\n    return undef + name\n"
    broken_v2 = "def greet(name):\n    return other_undef + name\n"
    client = FakeLLMClient(
        handler=_scripted_handler(
            plan_texts=["break it", "still broken"],
            patch_payloads=[
                {"files": [{"path": "buggy.py", "content": broken_v1}]},
                {"files": [{"path": "buggy.py", "content": broken_v2}]},
            ],
        )
    )
    graph = build_bug_fix_graph(fake_deps(client, git_push_token="ghp_secret"))

    final = await graph.run(_state(repo, extra={"repo_url": str(remote), "base_ref": "main"}))

    output = final.data["output"]
    assert isinstance(output, dict)
    assert output["verifier_passed"] is False
    assert output["pushed"] is False
    assert output["push_skip_reason"] == "verifier_failed"
    # Remote must still have only the seed commit on ``main``.
    branches = subprocess.run(
        ["git", "-C", str(remote), "branch", "--list"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "agent/task-1" not in branches


async def test_push_skips_when_no_token(tiny_repo_with_remote: tuple[Path, Path]) -> None:
    repo, remote = tiny_repo_with_remote
    client = FakeLLMClient(handler=_working_handler())
    graph = build_bug_fix_graph(fake_deps(client, git_push_token=None))

    final = await graph.run(_state(repo, extra={"repo_url": str(remote), "base_ref": "main"}))

    output = final.data["output"]
    assert isinstance(output, dict)
    assert output["pushed"] is False
    assert output["push_skip_reason"] == "no_token"
    assert output["repo_url"] == str(remote)
    assert output["base_ref"] == "main"


async def test_push_invokes_git_with_token_only_in_env(
    tiny_repo_with_remote: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token value must never appear in subprocess argv."""

    repo, remote = tiny_repo_with_remote
    secret = "ghp_super_secret_should_not_leak"
    captured: list[dict[str, Any]] = []
    real_create = asyncio.create_subprocess_exec

    async def recorder(*args: Any, **kwargs: Any) -> Any:
        if "push" in args:
            captured.append({"argv": list(args), "env": dict(kwargs.get("env") or {})})
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

    client = FakeLLMClient(handler=_working_handler())
    graph = build_bug_fix_graph(fake_deps(client, git_push_token=secret))

    final = await graph.run(_state(repo, extra={"repo_url": str(remote), "base_ref": "main"}))

    output = final.data["output"]
    assert isinstance(output, dict)
    assert output["pushed"] is True
    assert output["push_skip_reason"] is None
    assert len(captured) == 1
    push_argv = captured[0]["argv"]
    push_env = captured[0]["env"]
    # The secret value must travel via the environment, never argv.
    for token in push_argv:
        assert secret not in str(token)
    assert push_env.get("AGENT_GIT_PUSH_TOKEN") == secret
    # The branch did reach the bare ``origin`` repo.
    branches = subprocess.run(
        ["git", "-C", str(remote), "branch", "--list"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "agent/task-1" in branches


async def test_push_failure_raises_graph_error(
    tiny_repo_with_remote: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero exit from ``git push`` surfaces as a graph error."""

    repo, _remote = tiny_repo_with_remote
    real_create = asyncio.create_subprocess_exec

    class _FailingProc:
        returncode = 128

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"fatal: unable to access 'https://example/': not found\n"

    async def patched(*args: Any, **kwargs: Any) -> Any:
        if "push" in args:
            return _FailingProc()
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", patched)

    client = FakeLLMClient(handler=_working_handler())
    graph = build_bug_fix_graph(fake_deps(client, git_push_token="ghp_secret"))

    with pytest.raises(GraphError, match="git push failed"):
        await graph.run(
            _state(repo, extra={"repo_url": "https://example/repo", "base_ref": "main"})
        )


async def test_push_error_message_redacts_credentials_in_url(
    tiny_repo_with_remote: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If git surfaces a URL with embedded credentials, they must be stripped."""

    repo, _remote = tiny_repo_with_remote

    class _FailingProc:
        returncode = 128

        async def communicate(self) -> tuple[bytes, bytes]:
            return (
                b"",
                b"fatal: unable to access 'https://user:tok@example/repo/': boom\n",
            )

    real_create = asyncio.create_subprocess_exec

    async def route(*args: Any, **kwargs: Any) -> Any:
        if "push" in args:
            return _FailingProc()
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", route)

    client = FakeLLMClient(handler=_working_handler())
    graph = build_bug_fix_graph(fake_deps(client, git_push_token="ghp_secret"))

    with pytest.raises(GraphError) as exc_info:
        await graph.run(
            _state(repo, extra={"repo_url": "https://example/repo", "base_ref": "main"})
        )

    message = str(exc_info.value)
    assert "user:tok" not in message
    assert "<redacted>" in message
