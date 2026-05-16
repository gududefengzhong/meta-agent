"""Unit tests for the immutable orchestration state model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from meta_agent.core.orchestration import END, START, TaskRunState


def _state(**overrides: object) -> TaskRunState:
    base: dict[str, object] = {
        "task_id": "task-1",
        "tenant_id": "t-1",
        "trace_id": "trace-1",
        "graph_id": "builtin.echo",
    }
    base.update(overrides)
    return TaskRunState(**base)


def test_state_defaults_to_start_cursor() -> None:
    s = _state()
    assert s.current_node == START
    assert s.sequence == 0
    assert s.finished is False
    assert s.error is None
    assert s.data == {}


def test_state_is_frozen() -> None:
    s = _state()
    with pytest.raises(ValidationError):
        s.sequence = 5  # type: ignore[misc]


def test_state_requires_non_empty_ids() -> None:
    with pytest.raises(ValidationError):
        _state(tenant_id="")


def test_advance_merges_data_and_bumps_sequence() -> None:
    s = _state(data={"keep": 1})
    n = s.advance(next_node="plan", data_update={"new": "value"})
    assert n.current_node == "plan"
    assert n.sequence == 1
    assert n.data == {"keep": 1, "new": "value"}
    assert n.finished is False
    assert s.data == {"keep": 1}


def test_advance_to_end_marks_finished() -> None:
    s = _state()
    n = s.advance(next_node=END)
    assert n.current_node == END
    assert n.finished is True


def test_advance_explicit_finished_overrides_default() -> None:
    s = _state()
    n = s.advance(next_node="plan", finished=True)
    assert n.finished is True
    assert n.current_node == "plan"


def test_state_round_trips_via_model_dump() -> None:
    s = _state(data={"msg": "hi"}).advance(next_node="plan")
    dumped = s.model_dump(mode="json")
    restored = TaskRunState.model_validate(dumped)
    assert restored == s
