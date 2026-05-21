"""Unit tests for the Docker companion tool adapters."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from meta_agent.core.ports.tools import (
    ToolContext,
    ToolExecutionError,
    ToolPermissionError,
)
from meta_agent.infra.tools import (
    DockerWorkspaceEditTool,
    DockerWorkspaceFileSystemTool,
    DockerWorkspaceShellTool,
    DockerWorkspaceTestTool,
)


def _ctx(workspace: Path, *, output_byte_cap: int = 65536) -> ToolContext:
    return ToolContext(
        tenant_id="t",
        task_id="task",
        trace_id="trace",
        workspace_path=workspace,
        output_byte_cap=output_byte_cap,
    )


async def test_docker_shell_run_uses_docker_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "root" / "ws-1" / "feature"
    workspace.mkdir(parents=True)
    shell = DockerWorkspaceShellTool(workspace_root=tmp_path / "root")
    captured: dict[str, Any] = {}

    class _Proc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"hello\n", b"")

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        captured["argv"] = list(args)
        captured["kwargs"] = dict(kwargs)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    outcome = await shell.run(_ctx(workspace), argv=("python", "-V"))

    assert outcome.exit_code == 0
    assert "hello" in outcome.stdout
    assert captured["argv"][:5] == [
        "docker",
        "exec",
        "-w",
        "/workspace/feature",
        "meta-agent-ws-ws-1",
    ]
    assert captured["argv"][5:] == ["python", "-V"]


async def test_docker_fs_read_uses_container_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "root" / "ws-1" / "feature"
    workspace.mkdir(parents=True)
    (workspace / "a.txt").write_text("hello", encoding="utf-8")
    fs = DockerWorkspaceFileSystemTool(workspace_root=tmp_path / "root")
    captured: dict[str, Any] = {}

    class _Proc:
        returncode = 0

        async def communicate(self, stdin: bytes | None = None) -> tuple[bytes, bytes]:
            return (b"hello", b"")

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        captured["argv"] = list(args)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    out = await fs.read(_ctx(workspace), path="a.txt")

    assert out == "hello"
    assert captured["argv"][:5] == [
        "docker",
        "exec",
        "-w",
        "/workspace/feature",
        "meta-agent-ws-ws-1",
    ]
    assert captured["argv"][5] == "python3"
    assert captured["argv"][6] == "-c"
    assert "read_bytes" in captured["argv"][7]


async def test_docker_edit_write_uses_container_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "root" / "ws-1" / "feature"
    workspace.mkdir(parents=True)
    edit = DockerWorkspaceEditTool(workspace_root=tmp_path / "root")
    captured: dict[str, Any] = {}

    class _Proc:
        returncode = 0

        async def communicate(self, stdin: bytes | None = None) -> tuple[bytes, bytes]:
            captured["stdin"] = stdin
            return (b'{"files_changed":["a.txt"],"bytes_written":5}', b"")

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        captured["argv"] = list(args)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    outcome = await edit.write(_ctx(workspace), path="a.txt", content="hello")

    assert outcome.files_changed == ("a.txt",)
    assert outcome.bytes_written == 5
    assert captured["argv"][:6] == [
        "docker",
        "exec",
        "-i",
        "-w",
        "/workspace/feature",
        "meta-agent-ws-ws-1",
    ]
    assert captured["stdin"] == b"hello"


async def test_docker_edit_patch_apply_uses_git_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "root" / "ws-1" / "feature"
    workspace.mkdir(parents=True)
    edit = DockerWorkspaceEditTool(workspace_root=tmp_path / "root")
    captured: dict[str, Any] = {}

    class _Proc:
        returncode = 0

        async def communicate(self, stdin: bytes | None = None) -> tuple[bytes, bytes]:
            captured["stdin"] = stdin
            return (b"", b"")

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        captured["argv"] = list(args)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    diff = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n"
    outcome = await edit.patch_apply(_ctx(workspace), unified_diff=diff)

    assert outcome.files_changed == ("a.txt",)
    assert outcome.bytes_written == len(diff.encode("utf-8"))
    assert captured["argv"][:6] == [
        "docker",
        "exec",
        "-i",
        "-w",
        "/workspace/feature",
        "meta-agent-ws-ws-1",
    ]
    assert captured["argv"][6:] == ["git", "apply", "--whitespace=nowarn", "-"]
    assert captured["stdin"] == diff.encode("utf-8")


async def test_docker_shell_rejects_workspace_outside_root(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "elsewhere" / "ws-1" / "feature"
    workspace.mkdir(parents=True)
    shell = DockerWorkspaceShellTool(workspace_root=tmp_path / "root")
    with pytest.raises(ToolPermissionError):
        await shell.run(_ctx(workspace), argv=("python", "-V"))


async def test_docker_shell_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "root" / "ws-1" / "feature"
    workspace.mkdir(parents=True)
    shell = DockerWorkspaceShellTool(workspace_root=tmp_path / "root")

    class _Proc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(0.2)
            return (b"", b"")

        def kill(self) -> None:
            return None

        async def wait(self) -> None:
            return None

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(ToolExecutionError):
        await shell.run(_ctx(workspace), argv=("python", "-V"), timeout_seconds=0.05)


async def test_docker_test_run_uses_container_suite_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "root" / "ws-1" / "feature"
    workspace.mkdir(parents=True)
    test_tool = DockerWorkspaceTestTool(workspace_root=tmp_path / "root")
    captured: dict[str, Any] = {}

    class _Proc:
        returncode = 0

        async def communicate(self, stdin: bytes | None = None) -> tuple[bytes, bytes]:
            return (b"ok", b"")

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        captured["argv"] = list(args)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    outcome = await test_tool.run(
        _ctx(workspace),
        suite="python_lint",
        targets=("pkg/test_me.py",),
    )

    assert outcome.suite == "python_lint"
    assert outcome.exit_code == 0
    assert captured["argv"][:5] == [
        "docker",
        "exec",
        "-w",
        "/workspace/feature",
        "meta-agent-ws-ws-1",
    ]
    assert captured["argv"][5:] == [
        "python3",
        "-m",
        "ruff",
        "check",
        "--",
        "pkg/test_me.py",
    ]


async def test_docker_test_run_supports_typescript_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "root" / "ws-1" / "feature"
    workspace.mkdir(parents=True)
    test_tool = DockerWorkspaceTestTool(workspace_root=tmp_path / "root")
    captured: dict[str, Any] = {}

    class _Proc:
        returncode = 0

        async def communicate(self, stdin: bytes | None = None) -> tuple[bytes, bytes]:
            return (b"ts-ok", b"")

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        captured["argv"] = list(args)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    outcome = await test_tool.run(
        _ctx(workspace),
        suite="typescript_typecheck",
        targets=("src/index.ts",),
    )

    assert outcome.suite == "typescript_typecheck"
    assert outcome.exit_code == 0
    assert captured["argv"][5:] == [
        "npx",
        "tsc",
        "--noEmit",
        "--pretty",
        "false",
        "--",
        "src/index.ts",
    ]


async def test_docker_test_run_supports_typescript_test_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "root" / "ws-1" / "feature"
    workspace.mkdir(parents=True)
    test_tool = DockerWorkspaceTestTool(workspace_root=tmp_path / "root")
    captured: dict[str, Any] = {}

    class _Proc:
        returncode = 0

        async def communicate(self, stdin: bytes | None = None) -> tuple[bytes, bytes]:
            return (b"vitest-ok", b"")

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        captured["argv"] = list(args)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    outcome = await test_tool.run(
        _ctx(workspace),
        suite="typescript_test",
        targets=("src/index.test.ts",),
    )

    assert outcome.suite == "typescript_test"
    assert outcome.exit_code == 0
    assert captured["argv"][5:] == [
        "npx",
        "vitest",
        "run",
        "--globals",
        "--",
    ]
