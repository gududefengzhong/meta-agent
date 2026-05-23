"""Unit tests for :mod:`eval.swebench.patches` against real git."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from eval.swebench.patches import apply_test_patch, extract_patch
from eval.swebench.workspace import WorkspaceError


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_workspace(tmp_path: Path) -> tuple[Path, str]:
    """Create a workspace with one committed file; return (path, sha)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "--quiet", "--initial-branch=main")
    _git(ws, "config", "user.email", "a@b")
    _git(ws, "config", "user.name", "a")
    (ws / "calc.py").write_text("def add(x, y):\n    return x + y\n")
    _git(ws, "add", "calc.py")
    _git(ws, "commit", "-q", "-m", "initial")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ws, text=True).strip()
    return ws, sha


def test_extract_patch_empty_when_workspace_unchanged(tmp_path: Path) -> None:
    ws, sha = _init_workspace(tmp_path)
    assert extract_patch(ws, sha) == ""


def test_extract_patch_captures_unstaged_changes(tmp_path: Path) -> None:
    ws, sha = _init_workspace(tmp_path)
    (ws / "calc.py").write_text("def add(x, y):\n    return x + y + 1\n")
    diff = extract_patch(ws, sha)
    assert "calc.py" in diff
    assert "+    return x + y + 1" in diff


def test_extract_patch_captures_staged_changes(tmp_path: Path) -> None:
    ws, sha = _init_workspace(tmp_path)
    (ws / "calc.py").write_text("def add(x, y):\n    return 0\n")
    _git(ws, "add", "calc.py")
    diff = extract_patch(ws, sha)
    assert "calc.py" in diff
    assert "+    return 0" in diff


def test_extract_patch_captures_new_commits(tmp_path: Path) -> None:
    ws, sha = _init_workspace(tmp_path)
    (ws / "sub.py").write_text("def sub(x, y):\n    return x - y\n")
    _git(ws, "add", "sub.py")
    _git(ws, "commit", "-q", "-m", "add sub")
    diff = extract_patch(ws, sha)
    assert "sub.py" in diff


def test_extract_patch_missing_workspace_raises() -> None:
    with pytest.raises(WorkspaceError, match="not a directory"):
        extract_patch(Path("/definitely/missing"), "abc")


def test_extract_patch_empty_base_commit_rejected(tmp_path: Path) -> None:
    ws, _sha = _init_workspace(tmp_path)
    with pytest.raises(WorkspaceError, match="base_commit"):
        extract_patch(ws, "")


def test_apply_test_patch_modifies_workspace_files(tmp_path: Path) -> None:
    ws, _sha = _init_workspace(tmp_path)
    patch = (
        "diff --git a/calc.py b/calc.py\n"
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(x, y):\n"
        "-    return x + y\n"
        "+    return x + y + 999\n"
    )
    apply_test_patch(ws, patch)
    assert "+ 999" in (ws / "calc.py").read_text()


def test_apply_test_patch_empty_is_noop(tmp_path: Path) -> None:
    ws, _sha = _init_workspace(tmp_path)
    apply_test_patch(ws, "")
    # File unchanged.
    assert (ws / "calc.py").read_text() == "def add(x, y):\n    return x + y\n"


def test_apply_test_patch_malformed_raises(tmp_path: Path) -> None:
    ws, _sha = _init_workspace(tmp_path)
    with pytest.raises(WorkspaceError, match="apply test_patch"):
        apply_test_patch(ws, "this is not a patch at all\n")


def test_apply_test_patch_missing_workspace_raises() -> None:
    with pytest.raises(WorkspaceError, match="not a directory"):
        apply_test_patch(Path("/definitely/missing"), "diff --git a/x b/x\n")


def test_extract_patch_round_trip_with_apply(tmp_path: Path) -> None:
    """Apply a known patch, extract it back, assert the diff text matches semantically.

    Git emits the diff with its own formatting (no `b/` prefix tweaks
    we did when writing the input), so we don't assert byte-equality —
    just that the changed line ends up in the extracted diff.
    """

    ws, sha = _init_workspace(tmp_path)
    input_patch = (
        "diff --git a/calc.py b/calc.py\n"
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(x, y):\n"
        "-    return x + y\n"
        "+    return x + y + 1\n"
    )
    apply_test_patch(ws, input_patch)
    extracted = extract_patch(ws, sha)
    assert "+    return x + y + 1" in extracted
    assert "-    return x + y" in extracted
