"""Unit tests for TaskCheckpoint model."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from meta_agent.core.domain import TaskCheckpoint


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def test_checkpoint_requires_non_negative_sequence() -> None:
    with pytest.raises(ValidationError):
        TaskCheckpoint(
            checkpoint_id="cp-1",
            task_id="task-1",
            tenant_id="t-1",
            trace_id="trace-1",
            node_name="plan",
            sequence=-1,
            state_snapshot={},
            created_at=_now(),
        )


def test_checkpoint_carries_snapshot() -> None:
    snapshot: dict[str, object] = {"step": 3, "messages": ["a", "b"]}
    cp = TaskCheckpoint(
        checkpoint_id="cp-1",
        task_id="task-1",
        tenant_id="t-1",
        trace_id="trace-1",
        node_name="plan",
        sequence=0,
        state_snapshot=snapshot,
        created_at=_now(),
    )
    assert cp.state_snapshot == snapshot
