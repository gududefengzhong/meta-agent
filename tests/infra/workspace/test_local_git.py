"""End-to-end tests for the LocalGitWorkspaceManager adapter.

These tests shell out to the local ``git`` binary and operate on
disk under ``tmp_path``. They are unit-scoped (no testcontainers,
no network) and exercise the contract from the
:class:`WorkspaceManager` port: provisioning yields a writable
worktree on a feature branch, cleanup is idempotent, and error
output never leaks URL credentials.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from meta_agent.core.ports.workspace import WorkspaceError
from meta_agent.infra.workspace import LocalGitConfig, LocalGitWorkspaceManager


def _run(*args: str) -> None:
    subprocess.run(args, check=True, capture_output=True)


@pytest.fixture
def upstream_repo(tmp_path: Path) -> Path:
    """Create a small non-bare repo to serve as a clonable upstream."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _run("git", "init", "--initial-branch=main", str(upstream))
    _run("git", "-C", str(upstream), "config", "user.email", "t@example.com")
    _run("git", "-C", str(upstream), "config", "user.name", "test")
    (upstream / "README.md").write_text("hello\n")
    _run("git", "-C", str(upstream), "add", ".")
    _run("git", "-C", str(upstream), "commit", "-m", "initial")
    return upstream


@pytest.fixture
def manager(tmp_path: Path) -> LocalGitWorkspaceManager:
    root = tmp_path / "workspaces"
    root.mkdir()
    return LocalGitWorkspaceManager(LocalGitConfig(root_dir=root))


async def test_provision_clones_repo_and_checks_out_branch(
    manager: LocalGitWorkspaceManager, upstream_repo: Path
) -> None:
    ws = await manager.provision(
        tenant_id="t-1",
        task_id="task-1",
        trace_id="trace-1",
        branch="agent/task-1",
        repo_url=str(upstream_repo),
        base_ref="main",
    )
    worktree = Path(ws.worktree_path)
    assert worktree.is_dir()
    assert (worktree / "README.md").read_text() == "hello\n"
    head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == "agent/task-1"


async def test_provision_without_repo_url_initialises_empty_repo(
    manager: LocalGitWorkspaceManager,
) -> None:
    ws = await manager.provision(
        tenant_id="t-1",
        task_id="task-2",
        trace_id="trace-2",
        branch="agent/task-2",
    )
    worktree = Path(ws.worktree_path)
    assert worktree.is_dir()
    head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == "agent/task-2"


async def test_provision_with_invalid_url_redacts_credentials_and_cleans_up(
    manager: LocalGitWorkspaceManager, tmp_path: Path
) -> None:
    bogus = "https://user:supersecret@127.0.0.1:1/does-not-exist.git"
    with pytest.raises(WorkspaceError) as excinfo:
        await manager.provision(
            tenant_id="t-1",
            task_id="task-3",
            trace_id="trace-3",
            branch="agent/task-3",
            repo_url=bogus,
            base_ref="main",
        )
    # The secret never appears in the surfaced error message.
    assert "supersecret" not in str(excinfo.value)
    assert "<redacted>" in str(excinfo.value)
    # Half-provisioned root was cleaned: nothing residual under workspaces/.
    leftovers = [p for p in (tmp_path / "workspaces").iterdir()]
    assert leftovers == []


async def test_cleanup_removes_workspace_and_is_idempotent(
    manager: LocalGitWorkspaceManager, upstream_repo: Path
) -> None:
    ws = await manager.provision(
        tenant_id="t-1",
        task_id="task-4",
        trace_id="trace-4",
        branch="agent/task-4",
        repo_url=str(upstream_repo),
        base_ref="main",
    )
    root = Path(ws.worktree_path).parent
    assert root.exists()
    await manager.cleanup(ws)
    assert not root.exists()
    # Second cleanup is a no-op.
    await manager.cleanup(ws)


async def test_cleanup_refuses_path_outside_root(
    manager: LocalGitWorkspaceManager, tmp_path: Path
) -> None:
    from datetime import UTC, datetime

    from meta_agent.core.domain.workspace import Workspace

    rogue_root = tmp_path / "elsewhere"
    rogue_root.mkdir()
    rogue_worktree = rogue_root / "feature"
    rogue_worktree.mkdir()
    ws = Workspace(
        workspace_id="ws-rogue",
        tenant_id="t-1",
        task_id="task-rogue",
        trace_id="trace-rogue",
        branch="agent/task-rogue",
        worktree_path=str(rogue_worktree),
        created_at=datetime.now(UTC),
    )
    with pytest.raises(WorkspaceError, match="outside configured root"):
        await manager.cleanup(ws)
    # The rogue path was NOT touched.
    assert rogue_worktree.exists()
    shutil.rmtree(rogue_root)
