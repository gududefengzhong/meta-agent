"""Unit tests for the built-in ``builtin.bug_fix_v2`` graph."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from meta_agent.core.capabilities.registry import ToolRegistry
from meta_agent.core.orchestration import END, TaskRunState
from meta_agent.core.orchestration.graphs.bug_fix_v2 import (
    BUG_FIX_V2_GRAPH_ID,
    build_bug_fix_v2_graph,
)
from meta_agent.core.ports.tools import ToolCall
from meta_agent.infra.tools import (
    DockerWorkspaceEditTool,
    DockerWorkspaceFileSystemTool,
    DockerWorkspaceShellTool,
    DockerWorkspaceTestTool,
    LocalWorkspaceEditTool,
    LocalWorkspaceFileSystemTool,
    LocalWorkspaceShellTool,
    LocalWorkspaceTestTool,
    register_local_workspace_tools,
)
from tests.core.orchestration._fakes import FakeLLMClient, fake_deps, make_response

pytestmark = pytest.mark.asyncio


def _run(*args: str) -> None:
    subprocess.run(args, check=True, capture_output=True)


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run("git", "init", "--initial-branch=main", str(repo))
    _run("git", "-C", str(repo), "config", "user.email", "t@example.com")
    _run("git", "-C", str(repo), "config", "user.name", "test")
    (repo / "buggy.py").write_text('def greet(name):\n    return "hi " + name\n')
    _run("git", "-C", str(repo), "add", ".")
    _run("git", "-C", str(repo), "commit", "-m", "initial")
    _run("git", "-C", str(repo), "checkout", "-b", "agent/task-1")
    return repo


@pytest.fixture
def tiny_repo_with_remote(tiny_repo: Path) -> tuple[Path, Path]:
    remote = tiny_repo.parent / "remote.git"
    _run("git", "init", "--bare", "--initial-branch=main", str(remote))
    _run("git", "-C", str(tiny_repo), "remote", "add", "origin", str(remote))
    return tiny_repo, remote


def _state(repo: Path, *, extra: dict[str, object] | None = None) -> TaskRunState:
    data: dict[str, object] = {
        "issue_description": "greet should add a punctuation mark",
        "target_files": ["buggy.py"],
        "_workspace_path": str(repo),
        "_workspace_branch": "agent/task-1",
    }
    if extra:
        data.update(extra)
    return TaskRunState(
        task_id="task-1",
        tenant_id="tenant-1",
        trace_id="trace-1",
        graph_id=BUG_FIX_V2_GRAPH_ID,
        data=data,
    )


def _tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_local_workspace_tools(
        registry,
        fs=LocalWorkspaceFileSystemTool(),
        edit=LocalWorkspaceEditTool(),
        shell=LocalWorkspaceShellTool(),
        test=LocalWorkspaceTestTool(),
    )
    return registry


def _docker_tool_registry(workspace_root: Path) -> ToolRegistry:
    registry = ToolRegistry()
    register_local_workspace_tools(
        registry,
        fs=DockerWorkspaceFileSystemTool(workspace_root=workspace_root),
        edit=DockerWorkspaceEditTool(workspace_root=workspace_root),
        shell=DockerWorkspaceShellTool(workspace_root=workspace_root),
        test=DockerWorkspaceTestTool(workspace_root=workspace_root),
    )
    return registry


async def test_happy_path_edits_file_and_verifies(tiny_repo: Path) -> None:
    patched = 'def greet(name: str) -> str:\n    return f"hi {name}!"\n'
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="edit_write",
                        arguments={"path": "buggy.py", "content": patched},
                    ),
                ),
                finish_reason="tool_call",
            ),
            make_response(content="updated greeting"),
        ]
    )
    graph = build_bug_fix_v2_graph(fake_deps(client, tool_registry=_tool_registry()))

    final = await graph.run(_state(tiny_repo))

    assert final.current_node == END
    output = final.data["output"]
    assert isinstance(output, dict)
    assert output["verifier_passed"] is True
    assert output["attempts"] == 1
    assert output["files_changed"] == ["buggy.py"]
    assert output["pushed"] is False
    assert output["push_skip_reason"] == "no_repo_url"
    assert "suite=python_lint" in output["verifier_output"]
    assert isinstance(output["commit_sha"], str) and len(output["commit_sha"]) >= 7
    assert output["head_commit_sha"] == output["commit_sha"]
    assert output["tool_invocations"] == 1
    assert (tiny_repo / "buggy.py").read_text(encoding="utf-8") == patched


async def test_verify_failure_triggers_single_replan(tiny_repo: Path) -> None:
    bad = 'def greet(name)\n    return "hi"\n'
    good = 'def greet(name: str) -> str:\n    return f"hi {name}!"\n'
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="edit_write",
                        arguments={"path": "buggy.py", "content": bad},
                    ),
                ),
                finish_reason="tool_call",
            ),
            make_response(content="first try complete"),
            make_response(
                content="",
                tool_calls=(
                    ToolCall(
                        id="c2",
                        name="edit_write",
                        arguments={"path": "buggy.py", "content": good},
                    ),
                ),
                finish_reason="tool_call",
            ),
            make_response(content="fixed after verifier"),
        ]
    )
    graph = build_bug_fix_v2_graph(fake_deps(client, tool_registry=_tool_registry()))

    final = await graph.run(_state(tiny_repo))

    output = final.data["output"]
    assert output["verifier_passed"] is True
    assert output["attempts"] == 2
    assert output["assistant_message"] == "fixed after verifier"
    # The second inner shell_agent run should have seen verifier feedback.
    assert "Verifier output:" in client.calls[2].messages[1].content


async def test_push_skips_when_no_token(tiny_repo_with_remote: tuple[Path, Path]) -> None:
    repo, remote = tiny_repo_with_remote
    patched = 'def greet(name: str) -> str:\n    return f"hi {name}!"\n'
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="edit_write",
                        arguments={"path": "buggy.py", "content": patched},
                    ),
                ),
                finish_reason="tool_call",
            ),
            make_response(content="updated greeting"),
        ]
    )
    graph = build_bug_fix_v2_graph(
        fake_deps(client, git_push_token=None, tool_registry=_tool_registry())
    )

    final = await graph.run(_state(repo, extra={"repo_url": str(remote), "base_ref": "main"}))

    output = final.data["output"]
    assert output["verifier_passed"] is True
    assert output["pushed"] is False
    assert output["push_skip_reason"] == "no_token"
    assert isinstance(output["commit_sha"], str) and len(output["commit_sha"]) >= 7
    assert output["repo_url"] == str(remote)
    assert output["base_ref"] == "main"


async def test_push_invokes_git_with_token_only_in_env(
    tiny_repo_with_remote: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, remote = tiny_repo_with_remote
    patched = 'def greet(name: str) -> str:\n    return f"hi {name}!"\n'
    secret = "ghp_super_secret_should_not_leak"
    captured: list[dict[str, Any]] = []
    real_create = asyncio.create_subprocess_exec

    async def recorder(*args: Any, **kwargs: Any) -> Any:
        if "push" in args:
            captured.append({"argv": list(args), "env": dict(kwargs.get("env") or {})})
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="edit_write",
                        arguments={"path": "buggy.py", "content": patched},
                    ),
                ),
                finish_reason="tool_call",
            ),
            make_response(content="updated greeting"),
        ]
    )
    graph = build_bug_fix_v2_graph(
        fake_deps(client, git_push_token=secret, tool_registry=_tool_registry())
    )

    final = await graph.run(_state(repo, extra={"repo_url": str(remote), "base_ref": "main"}))

    output = final.data["output"]
    assert output["pushed"] is True
    assert output["push_skip_reason"] is None
    assert len(captured) == 1
    for token in captured[0]["argv"]:
        assert secret not in str(token)
    assert captured[0]["env"].get("AGENT_GIT_PUSH_TOKEN") == secret
    branches = subprocess.run(
        ["git", "-C", str(remote), "branch", "--list"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "agent/task-1" in branches


async def test_happy_path_with_docker_tool_stack_uses_docker_exec(
    tiny_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched = 'def greet(name: str) -> str:\n    return f"hi {name}!"\n'
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="edit_write",
                        arguments={"path": "buggy.py", "content": patched},
                    ),
                ),
                finish_reason="tool_call",
            ),
            make_response(content="updated greeting"),
        ]
    )
    docker_calls: list[list[str]] = []
    real_create = asyncio.create_subprocess_exec

    async def recorder(*args: Any, **kwargs: Any) -> Any:
        if args and args[0] == "docker":
            docker_calls.append([str(part) for part in args])
            workspace = tiny_repo
            argv = [str(part) for part in args]

            class _Proc:
                returncode = 0

                async def communicate(self, stdin: bytes | None = None) -> tuple[bytes, bytes]:
                    if "python3" in argv and any("tmp.replace(target)" in part for part in argv):
                        target = workspace / argv[-1]
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(stdin or b"")
                        size = len(stdin or b"")
                        body = f'{{"files_changed":["{argv[-1]}"],"bytes_written":{size}}}'.encode()
                        return (body, b"")
                    if "python3" in argv and any("read_bytes" in part for part in argv):
                        target = workspace / argv[-1]
                        return (target.read_bytes(), b"")
                    return (b"", b"")

            return _Proc()
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

    graph = build_bug_fix_v2_graph(
        fake_deps(client, tool_registry=_docker_tool_registry(tiny_repo.parent))
    )

    final = await graph.run(_state(tiny_repo))

    output = final.data["output"]
    assert output["verifier_passed"] is True
    assert output["tool_invocations"] == 1
    assert (tiny_repo / "buggy.py").read_text(encoding="utf-8") == patched
    assert docker_calls
    first_call = docker_calls[0]
    assert first_call[:2] == ["docker", "exec"]
    assert "/workspace/repo" in first_call
    assert f"meta-agent-ws-{tiny_repo.parent.name}" in first_call


async def test_verify_suite_override_flows_to_test_tool(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run("git", "init", "--initial-branch=main", str(repo))
    _run("git", "-C", str(repo), "config", "user.email", "t@example.com")
    _run("git", "-C", str(repo), "config", "user.name", "test")
    (repo / "buggy.ts").write_text("export const greet = (name: string) => 'hi ' + name;\n")
    _run("git", "-C", str(repo), "add", ".")
    _run("git", "-C", str(repo), "commit", "-m", "initial")
    _run("git", "-C", str(repo), "checkout", "-b", "agent/task-1")

    patched = "export const greet = (name: string) => `hi ${name}!`;\n"
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="edit_write",
                        arguments={"path": "buggy.ts", "content": patched},
                    ),
                ),
                finish_reason="tool_call",
            ),
            make_response(content="updated ts greeting"),
        ]
    )
    registry = ToolRegistry()
    register_local_workspace_tools(
        registry,
        fs=LocalWorkspaceFileSystemTool(),
        edit=LocalWorkspaceEditTool(),
        shell=LocalWorkspaceShellTool(),
        test=LocalWorkspaceTestTool(
            suites={"typescript_typecheck": (sys.executable, "-c", "print('ts-ok')")},
        ),
    )
    graph = build_bug_fix_v2_graph(fake_deps(client, tool_registry=registry))

    final = await graph.run(
        _state(
            repo,
            extra={
                "target_files": ["buggy.ts"],
                "verify_suite": "typescript_typecheck",
            },
        )
    )

    output = final.data["output"]
    assert output["verifier_passed"] is True
    assert "suite=typescript_typecheck" in output["verifier_output"]
    assert "ts-ok" in output["verifier_output"]
    assert output["files_changed"] == ["buggy.ts"]


async def test_verify_suite_typescript_test_with_docker_tool_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run("git", "init", "--initial-branch=main", str(repo))
    _run("git", "-C", str(repo), "config", "user.email", "t@example.com")
    _run("git", "-C", str(repo), "config", "user.name", "test")
    (repo / "buggy.ts").write_text("export const greet = (name: string) => 'hi ' + name;\n")
    _run("git", "-C", str(repo), "add", ".")
    _run("git", "-C", str(repo), "commit", "-m", "initial")
    _run("git", "-C", str(repo), "checkout", "-b", "agent/task-1")

    patched = "export const greet = (name: string) => `hi ${name}!`;\n"
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="edit_write",
                        arguments={"path": "buggy.ts", "content": patched},
                    ),
                ),
                finish_reason="tool_call",
            ),
            make_response(content="updated ts greeting"),
        ]
    )
    docker_calls: list[list[str]] = []
    real_create = asyncio.create_subprocess_exec

    async def recorder(*args: Any, **kwargs: Any) -> Any:
        if args and args[0] == "docker":
            docker_calls.append([str(part) for part in args])
            workspace = repo
            argv = [str(part) for part in args]

            class _Proc:
                returncode = 0

                async def communicate(self, stdin: bytes | None = None) -> tuple[bytes, bytes]:
                    if "python3" in argv and any("tmp.replace(target)" in part for part in argv):
                        target = workspace / argv[-1]
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(stdin or b"")
                        size = len(stdin or b"")
                        body = f'{{"files_changed":["{argv[-1]}"],"bytes_written":{size}}}'.encode()
                        return (body, b"")
                    if "npx" in argv and "vitest" in argv and "run" in argv:
                        return (b"vitest-pass", b"")
                    return (b"", b"")

            return _Proc()
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

    graph = build_bug_fix_v2_graph(
        fake_deps(client, tool_registry=_docker_tool_registry(repo.parent))
    )

    final = await graph.run(
        _state(
            repo,
            extra={
                "target_files": ["buggy.ts"],
                "verify_suite": "typescript_test",
            },
        )
    )

    output = final.data["output"]
    assert output["verifier_passed"] is True
    assert "suite=typescript_test" in output["verifier_output"]
    assert "vitest-pass" in output["verifier_output"]
    assert any(call[5:8] == ["npx", "vitest", "run"] for call in docker_calls)
