"""Unit tests for the local-worktree FS/Edit adapters."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from meta_agent.core.ports.tools import (
    ToolContext,
    ToolExecutionError,
    ToolPermissionError,
    ToolValidationError,
)
from meta_agent.infra.tools.local_workspace import (
    LocalWorkspaceEditTool,
    LocalWorkspaceFileSystemTool,
)


def _ctx(workspace: Path, *, output_byte_cap: int = 65536) -> ToolContext:
    return ToolContext(
        tenant_id="t",
        task_id="task",
        trace_id="trace",
        workspace_path=workspace,
        output_byte_cap=output_byte_cap,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


async def test_read_returns_utf8_slice(workspace: Path) -> None:
    target = workspace / "hello.txt"
    target.write_text("hello world", encoding="utf-8")
    fs = LocalWorkspaceFileSystemTool()
    result = await fs.read(_ctx(workspace), path="hello.txt")
    assert result == "hello world"


async def test_read_respects_offset_and_max_bytes(workspace: Path) -> None:
    (workspace / "f.txt").write_text("abcdef", encoding="utf-8")
    fs = LocalWorkspaceFileSystemTool()
    out = await fs.read(_ctx(workspace), path="f.txt", offset=2, max_bytes=3)
    assert out == "cde"


async def test_read_rejects_dotdot_escape(workspace: Path) -> None:
    fs = LocalWorkspaceFileSystemTool()
    with pytest.raises(ToolPermissionError):
        await fs.read(_ctx(workspace), path="../etc/passwd")


async def test_read_rejects_absolute_outside_workspace(workspace: Path) -> None:
    fs = LocalWorkspaceFileSystemTool()
    with pytest.raises(ToolPermissionError):
        await fs.read(_ctx(workspace), path="/etc/passwd")


async def test_read_rejects_empty_path(workspace: Path) -> None:
    fs = LocalWorkspaceFileSystemTool()
    with pytest.raises(ToolValidationError):
        await fs.read(_ctx(workspace), path="")


async def test_read_rejects_directory(workspace: Path) -> None:
    (workspace / "sub").mkdir()
    fs = LocalWorkspaceFileSystemTool()
    with pytest.raises(ToolExecutionError):
        await fs.read(_ctx(workspace), path="sub")


async def test_read_requires_workspace_in_context() -> None:
    fs = LocalWorkspaceFileSystemTool()
    ctx = ToolContext(tenant_id="t", task_id="x", trace_id="r")
    with pytest.raises(ToolPermissionError):
        await fs.read(ctx, path="anything.txt")


async def test_read_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    fs = LocalWorkspaceFileSystemTool()
    with pytest.raises(ToolPermissionError):
        await fs.read(_ctx(workspace), path="link.txt")


async def test_list_dir_sorted_non_recursive(workspace: Path) -> None:
    (workspace / "b.txt").write_text("b", encoding="utf-8")
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    (workspace / "sub").mkdir()
    fs = LocalWorkspaceFileSystemTool()
    entries = await fs.list_dir(_ctx(workspace), path="")
    assert entries == ("a.txt", "b.txt", "sub/")


async def test_list_dir_recursive(workspace: Path) -> None:
    (workspace / "sub").mkdir()
    (workspace / "sub" / "nested.txt").write_text("x", encoding="utf-8")
    fs = LocalWorkspaceFileSystemTool()
    entries = await fs.list_dir(_ctx(workspace), path="", recursive=True)
    assert "sub/" in entries
    assert "sub/nested.txt" in entries


async def test_list_dir_rejects_non_dir(workspace: Path) -> None:
    (workspace / "f.txt").write_text("x", encoding="utf-8")
    fs = LocalWorkspaceFileSystemTool()
    with pytest.raises(ToolExecutionError):
        await fs.list_dir(_ctx(workspace), path="f.txt")


async def test_grep_returns_matches(workspace: Path) -> None:
    (workspace / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    (workspace / "b.py").write_text("def bar():\n    return 1\n", encoding="utf-8")
    fs = LocalWorkspaceFileSystemTool()
    hits = await fs.grep(_ctx(workspace), pattern=r"^def [a-z]+", path_globs=("**/*.py",))
    paths = sorted(h.path for h in hits)
    assert paths == ["a.py", "b.py"]
    assert all(h.line_no == 1 for h in hits)


async def test_grep_caps_at_max_matches(workspace: Path) -> None:
    body = "\n".join(["match"] * 10) + "\n"
    (workspace / "many.txt").write_text(body, encoding="utf-8")
    fs = LocalWorkspaceFileSystemTool()
    hits = await fs.grep(
        _ctx(workspace), pattern=r"match", path_globs=("**/*.txt",), max_matches=3
    )
    assert len(hits) == 3


async def test_grep_rejects_bad_regex(workspace: Path) -> None:
    fs = LocalWorkspaceFileSystemTool()
    with pytest.raises(ToolValidationError):
        await fs.grep(_ctx(workspace), pattern="(unclosed")


async def test_edit_write_atomic_creates_parents(workspace: Path) -> None:
    edit = LocalWorkspaceEditTool()
    outcome = await edit.write(_ctx(workspace), path="nested/dir/out.txt", content="hi")
    assert outcome.files_changed == ("nested/dir/out.txt",)
    assert outcome.bytes_written == 2
    assert (workspace / "nested/dir/out.txt").read_text(encoding="utf-8") == "hi"


async def test_edit_write_rejects_escape(workspace: Path) -> None:
    edit = LocalWorkspaceEditTool()
    with pytest.raises(ToolPermissionError):
        await edit.write(_ctx(workspace), path="../escape.txt", content="x")


async def test_edit_write_overwrites_existing(workspace: Path) -> None:
    (workspace / "a.txt").write_text("old", encoding="utf-8")
    edit = LocalWorkspaceEditTool()
    await edit.write(_ctx(workspace), path="a.txt", content="new")
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "new"


def _has_git() -> bool:
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


@pytest.mark.skipif(not _has_git(), reason="git binary not available")
async def test_patch_apply_happy_path(workspace: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=workspace, check=True)
    (workspace / "f.txt").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=workspace, check=True)
    diff = (
        "diff --git a/f.txt b/f.txt\n"
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    edit = LocalWorkspaceEditTool()
    outcome = await edit.patch_apply(_ctx(workspace), unified_diff=diff)
    assert outcome.files_changed == ("f.txt",)
    assert (workspace / "f.txt").read_text(encoding="utf-8") == "new\n"


async def test_patch_apply_rejects_empty_diff(workspace: Path) -> None:
    edit = LocalWorkspaceEditTool()
    with pytest.raises(ToolValidationError):
        await edit.patch_apply(_ctx(workspace), unified_diff="   \n")


@pytest.mark.skipif(not _has_git(), reason="git binary not available")
async def test_patch_apply_surfaces_non_zero_exit(workspace: Path) -> None:
    # No git repo and no file: ``git apply`` will fail loudly.
    edit = LocalWorkspaceEditTool()
    bad_diff = (
        "diff --git a/missing.txt b/missing.txt\n"
        "--- a/missing.txt\n"
        "+++ b/missing.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    with pytest.raises(ToolExecutionError):
        await edit.patch_apply(_ctx(workspace), unified_diff=bad_diff)
