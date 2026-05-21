"""Unit tests for Task model."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from meta_agent.core.domain import Task, TaskState, TaskType


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def _task(**overrides: object) -> Task:
    base: dict[str, object] = {
        "task_id": "task-1",
        "tenant_id": "t-1",
        "principal_id": "p-1",
        "trace_id": "trace-1",
        "task_type": TaskType.BUG_FIX,
        "created_at": _now(),
        "updated_at": _now(),
    }
    base.update(overrides)
    return Task(**base)


def test_task_defaults_to_pending() -> None:
    task = _task()
    assert task.state is TaskState.PENDING
    assert task.session_id is None
    assert task.idempotency_key is None
    assert task.graph_id is None
    assert task.input_payload == {}


def test_task_requires_trace_id() -> None:
    with pytest.raises(ValidationError):
        _task(trace_id="")


def test_task_accepts_known_task_types() -> None:
    for task_type in (
        TaskType.BUG_FIX,
        TaskType.CODE_REVIEW,
        TaskType.AUTO_PR,
        TaskType.SYSTEM_ECHO,
    ):
        assert _task(task_type=task_type).task_type is task_type


def test_task_rejects_empty_graph_id() -> None:
    with pytest.raises(ValidationError):
        _task(graph_id="")


def test_task_accepts_explicit_graph_id() -> None:
    assert _task(graph_id="builtin.echo").graph_id == "builtin.echo"


def test_task_state_set_contains_expected_members() -> None:
    expected = {
        "pending",
        "running",
        "awaiting_human",
        "succeeded",
        "failed",
        "cancelled",
    }
    assert {s.value for s in TaskState} == expected


def test_task_type_set_contains_expected_members() -> None:
    assert {t.value for t in TaskType} == {
        "bug_fix",
        "code_review",
        "auto_pr",
        "system_echo",
        "system_chat",
        "system_git_inspect",
        "system_shell_agent",
    }
