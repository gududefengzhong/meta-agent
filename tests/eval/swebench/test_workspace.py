"""Unit tests for :mod:`eval.swebench.workspace` against a hermetic git remote.

Pattern: create a bare git repo in ``tmp_path``, commit some
history, then point :func:`prepare_workspace` at it via a
``file://`` URL. No network, no mocks — the real ``git`` binary
exercises the actual subprocess + error paths.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from eval.swebench.instances import SWEBenchInstance
from eval.swebench.workspace import WorkspaceError, prepare_workspace


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_remote(tmp_path: Path) -> tuple[str, str, str]:
    """Build a bare repo with two commits; return (url, sha1, sha2)."""

    bare = tmp_path / "remote.git"
    work = tmp_path / "seed-work"
    bare.mkdir()
    work.mkdir()
    _git(bare, "init", "--bare", "--quiet")
    _git(work, "init", "--quiet", "--initial-branch=main")
    _git(work, "config", "user.email", "a@b")
    _git(work, "config", "user.name", "a")
    (work / "a.py").write_text("def add(x, y):\n    return x + y\n")
    _git(work, "add", "a.py")
    _git(work, "commit", "-q", "-m", "initial")
    sha1 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work, text=True).strip()
    (work / "b.py").write_text("def sub(x, y):\n    return x - y\n")
    _git(work, "add", "b.py")
    _git(work, "commit", "-q", "-m", "add b")
    sha2 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work, text=True).strip()
    _git(work, "remote", "add", "origin", f"file://{bare}")
    _git(work, "push", "-q", "origin", "main")
    return f"file://{bare}", sha1, sha2


def _instance(repo: str = "test/repo", base_commit: str = "DEADBEEF") -> SWEBenchInstance:
    return SWEBenchInstance(
        instance_id="test__repo-1",
        repo=repo,
        base_commit=base_commit,
    )


def test_prepare_workspace_clones_and_checks_out_base_commit(
    tmp_path: Path,
) -> None:
    url, sha1, _sha2 = _make_remote(tmp_path)
    workspace = tmp_path / "ws"
    inst = _instance(base_commit=sha1)

    result = prepare_workspace(inst, workspace, remote_url=url)

    assert result == workspace.resolve()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, text=True).strip()
    assert head == sha1
    # First commit only — second file does not exist at sha1.
    assert (workspace / "a.py").exists()
    assert not (workspace / "b.py").exists()


def test_prepare_workspace_can_checkout_later_commit(tmp_path: Path) -> None:
    url, _sha1, sha2 = _make_remote(tmp_path)
    workspace = tmp_path / "ws"
    inst = _instance(base_commit=sha2)
    prepare_workspace(inst, workspace, remote_url=url)
    assert (workspace / "a.py").exists()
    assert (workspace / "b.py").exists()


def test_prepare_workspace_existing_dest_rejected(tmp_path: Path) -> None:
    url, sha1, _ = _make_remote(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "junk").write_text("x")
    inst = _instance(base_commit=sha1)
    with pytest.raises(WorkspaceError, match="already exists"):
        prepare_workspace(inst, workspace, remote_url=url)


def test_prepare_workspace_overwrite_replaces_existing(tmp_path: Path) -> None:
    url, sha1, _ = _make_remote(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "junk").write_text("x")
    inst = _instance(base_commit=sha1)
    prepare_workspace(inst, workspace, remote_url=url, overwrite=True)
    assert (workspace / "a.py").exists()
    assert not (workspace / "junk").exists()


def test_prepare_workspace_bad_commit_raises(tmp_path: Path) -> None:
    url, _sha1, _sha2 = _make_remote(tmp_path)
    workspace = tmp_path / "ws"
    inst = _instance(base_commit="0" * 40)  # SHA-shaped but missing
    with pytest.raises(WorkspaceError, match="checkout 0000"):
        prepare_workspace(inst, workspace, remote_url=url)


def test_prepare_workspace_bad_remote_raises(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    inst = _instance(base_commit="abc123")
    with pytest.raises(WorkspaceError, match="clone test/repo"):
        prepare_workspace(inst, workspace, remote_url=f"file://{tmp_path}/does-not-exist")
