"""Unit tests for the Workspace domain model."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from meta_agent.core.domain import Workspace


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def test_workspace_carries_lifecycle_attribution() -> None:
    ws = Workspace(
        workspace_id="ws-1",
        tenant_id="t-1",
        task_id="task-1",
        trace_id="trace-1",
        repo_url="https://example.com/org/repo.git",
        base_ref="main",
        branch="agent/task-1",
        worktree_path="/var/agent/workspaces/ws-1",
        created_at=_now(),
    )
    assert ws.branch == "agent/task-1"
    assert ws.worktree_path == "/var/agent/workspaces/ws-1"
    assert ws.repo_url == "https://example.com/org/repo.git"
    assert ws.base_ref == "main"


def test_workspace_allows_no_upstream_repo() -> None:
    ws = Workspace(
        workspace_id="ws-2",
        tenant_id="t-1",
        task_id="task-2",
        trace_id="trace-2",
        branch="agent/task-2",
        worktree_path="/var/agent/workspaces/ws-2",
        created_at=_now(),
    )
    assert ws.repo_url is None
    assert ws.base_ref is None


def test_workspace_rejects_empty_branch_and_path() -> None:
    with pytest.raises(ValidationError):
        Workspace(
            workspace_id="ws-3",
            tenant_id="t-1",
            task_id="task-3",
            trace_id="trace-3",
            branch="",
            worktree_path="/tmp/ws-3",
            created_at=_now(),
        )
    with pytest.raises(ValidationError):
        Workspace(
            workspace_id="ws-3",
            tenant_id="t-1",
            task_id="task-3",
            trace_id="trace-3",
            branch="agent/task-3",
            worktree_path="",
            created_at=_now(),
        )
