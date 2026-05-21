"""Unit tests for the DockerWorkspaceManager adapter.

These tests do not require a real Docker daemon. They exercise the
adapter's orchestration logic by stubbing the inner local-git manager
and the docker CLI subprocess wrapper.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from meta_agent.core.domain.workspace import Workspace
from meta_agent.core.ports.workspace import WorkspaceError
from meta_agent.infra.workspace import (
    DockerWorkspaceConfig,
    DockerWorkspaceManager,
    LocalGitConfig,
)


class _StubInner:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.provision_calls: list[dict[str, object]] = []
        self.cleaned: list[Workspace] = []

    async def provision(self, **kwargs: object) -> Workspace:
        self.provision_calls.append(dict(kwargs))
        return self.workspace

    async def cleanup(self, workspace: Workspace) -> None:
        self.cleaned.append(workspace)


def _workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "workspaces" / "ws-1"
    feature = root / "feature"
    feature.mkdir(parents=True)
    return Workspace(
        workspace_id="ws-1",
        tenant_id="tenant-1",
        task_id="task-1",
        trace_id="trace-1",
        branch="agent/task-1",
        worktree_path=str(feature),
        created_at=datetime(2026, 5, 21, tzinfo=UTC),
    )


def _manager(tmp_path: Path, inner: _StubInner) -> DockerWorkspaceManager:
    return DockerWorkspaceManager(
        DockerWorkspaceConfig(
            local_git=LocalGitConfig(root_dir=tmp_path / "workspaces"),
            image="python:3.12-slim",
            network="meta-agent",
        ),
        inner=inner,  # type: ignore[arg-type]
    )


async def test_provision_starts_container_with_bind_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _workspace(tmp_path)
    inner = _StubInner(ws)
    manager = _manager(tmp_path, inner)
    captured: list[list[str]] = []

    async def fake_run(args: object) -> str:
        assert isinstance(args, list)
        captured.append([str(item) for item in args])
        return "container-id"

    monkeypatch.setattr(manager, "_run", fake_run)

    out = await manager.provision(
        tenant_id="tenant-1",
        task_id="task-1",
        trace_id="trace-1",
        branch="agent/task-1",
        repo_url="https://example/repo.git",
        base_ref="main",
    )

    assert out == ws
    assert len(captured) == 1
    args = captured[0]
    assert args[:4] == ["docker", "run", "-d", "--rm"]
    assert "--network" in args and "meta-agent" in args
    assert f"{Path(ws.worktree_path).parent.resolve()}:/workspace" in args
    assert "/workspace/feature" in args


async def test_cleanup_stops_container_then_cleans_inner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path)
    inner = _StubInner(ws)
    manager = _manager(tmp_path, inner)
    manager._containers[ws.workspace_id] = "meta-agent-ws-ws-1"
    captured: list[list[str]] = []

    async def fake_run(args: object) -> str:
        assert isinstance(args, list)
        captured.append([str(item) for item in args])
        return ""

    monkeypatch.setattr(manager, "_run", fake_run)

    await manager.cleanup(ws)

    assert captured == [["docker", "rm", "-f", "meta-agent-ws-ws-1"]]
    assert inner.cleaned == [ws]


async def test_cleanup_ignores_missing_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _workspace(tmp_path)
    inner = _StubInner(ws)
    manager = _manager(tmp_path, inner)

    async def fake_run(args: object) -> str:
        raise WorkspaceError("docker command failed (exit=1): docker rm -f meta-agent-ws-ws-1\nNo such container")

    monkeypatch.setattr(manager, "_run", fake_run)

    await manager.cleanup(ws)

    assert inner.cleaned == [ws]


async def test_provision_failure_triggers_inner_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path)
    inner = _StubInner(ws)
    manager = _manager(tmp_path, inner)

    async def fake_run(args: object) -> str:
        raise WorkspaceError("docker boom")

    monkeypatch.setattr(manager, "_run", fake_run)

    with pytest.raises(WorkspaceError, match="docker boom"):
        await manager.provision(
            tenant_id="tenant-1",
            task_id="task-1",
            trace_id="trace-1",
            branch="agent/task-1",
        )

    assert inner.cleaned == [ws]
