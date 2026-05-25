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


# ----------------------------------------------------------------- extract_patch


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


def test_extract_patch_empty_base_ref_rejected(tmp_path: Path) -> None:
    ws, _sha = _init_workspace(tmp_path)
    with pytest.raises(WorkspaceError, match="base_ref"):
        extract_patch(ws, "")


# ----------------------------------------------------------------- apply_test_patch


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


def test_apply_test_patch_commits_and_returns_new_head_sha(tmp_path: Path) -> None:
    """A non-empty patch commits and the function returns the post-commit SHA.

    This SHA is what callers thread into :func:`extract_patch` so
    the agent's later edits diff cleanly against the post-test_patch
    state (rather than against ``base_commit``, which would mix
    test_patch additions into the captured agent patch).
    """

    ws, base_sha = _init_workspace(tmp_path)
    patch = (
        "diff --git a/calc.py b/calc.py\n"
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(x, y):\n"
        "-    return x + y\n"
        "+    return x + y + 1\n"
    )
    returned_sha = apply_test_patch(ws, patch)
    assert returned_sha
    # Different from the pre-apply commit — a new commit was created.
    assert returned_sha != base_sha
    # HEAD now points at that SHA.
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ws, text=True).strip()
    assert head == returned_sha


def test_apply_test_patch_empty_is_noop_returns_current_head(tmp_path: Path) -> None:
    """Empty patch leaves the workspace + commit graph untouched
    but still returns the current HEAD SHA so callers have a
    uniform post-apply ref to thread through."""

    ws, base_sha = _init_workspace(tmp_path)
    returned_sha = apply_test_patch(ws, "")
    assert (ws / "calc.py").read_text() == "def add(x, y):\n    return x + y\n"
    # HEAD unchanged; the returned SHA equals the base SHA.
    assert returned_sha == base_sha


def test_apply_test_patch_malformed_raises(tmp_path: Path) -> None:
    ws, _sha = _init_workspace(tmp_path)
    with pytest.raises(WorkspaceError, match="apply test_patch"):
        apply_test_patch(ws, "this is not a patch at all\n")


def test_apply_test_patch_missing_workspace_raises() -> None:
    with pytest.raises(WorkspaceError, match="not a directory"):
        apply_test_patch(Path("/definitely/missing"), "diff --git a/x b/x\n")


# ----------------------------------------------------------------- round-trip


def test_extract_patch_against_post_test_patch_ref_excludes_test_patch(
    tmp_path: Path,
) -> None:
    """The bug this fixes: when ``extract_patch`` uses the
    ``base_commit`` SHA (pre-test_patch), the extracted diff
    contains the test_patch lines, which then re-conflict with the
    eval container's own test_patch application. Using the SHA
    returned by ``apply_test_patch`` instead, the diff captures
    only the agent's net edits.
    """

    ws, _base_sha = _init_workspace(tmp_path)
    test_patch = (
        "diff --git a/calc.py b/calc.py\n"
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(x, y):\n"
        "-    return x + y\n"
        "+    return x + y  # test marker\n"
    )
    post_test_patch_sha = apply_test_patch(ws, test_patch)

    # Simulate the agent making its own real edit on top.
    (ws / "calc.py").write_text("def add(x, y):\n    return x + y + 1  # agent fix; test marker\n")

    # Diff against the post-test_patch ref: only the agent's edit
    # appears, the test marker line is unchanged so doesn't show.
    extracted = extract_patch(ws, post_test_patch_sha)
    assert "+    return x + y + 1" in extracted or "agent fix" in extracted
    # The original test_patch line "+    return x + y  # test marker"
    # must NOT appear as an addition — that's the bug we're guarding.
    assert "+    return x + y  # test marker" not in extracted


def test_extract_patch_round_trip_with_apply(tmp_path: Path) -> None:
    """Apply a patch, then extract against the PRE-apply SHA. The
    captured diff should contain the same edit (this is the old
    behaviour, preserved so old callers that pass ``base_commit``
    still get sensible output even without the harness's
    apply-then-extract dance).
    """

    ws, pre_sha = _init_workspace(tmp_path)
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
    extracted = extract_patch(ws, pre_sha)
    assert "+    return x + y + 1" in extracted
    assert "-    return x + y" in extracted
