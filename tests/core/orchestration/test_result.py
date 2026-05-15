"""Unit tests for the :class:`TaskResult` / :class:`TaskError` contract.

These cover the schema invariants enforced by the pydantic models. The
projection from :class:`TaskRunState` to :class:`TaskResult` lives in
:class:`meta_agent.worker.runner.WorkerLoop` and is covered by
``tests/worker/test_runner.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from meta_agent.core.orchestration.result import (
    TaskError,
    TaskErrorCode,
    TaskResult,
)

_T0 = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 5, 15, 12, 0, 1, tzinfo=UTC)


def _ok(**overrides: object) -> TaskResult:
    base: dict[str, object] = {
        "task_id": "task-1",
        "tenant_id": "tenant-1",
        "trace_id": "trace-1",
        "graph_id": "builtin.echo",
        "status": "succeeded",
        "output": {"echo": "hi"},
        "error": None,
        "node_sequence": 3,
        "started_at": _T0,
        "finished_at": _T1,
    }
    base.update(overrides)
    return TaskResult.model_validate(base)


def test_succeeded_result_round_trips_through_json() -> None:
    result = _ok()
    dumped = result.model_dump(mode="json")
    restored = TaskResult.model_validate(dumped)
    assert restored == result
    assert dumped["status"] == "succeeded"
    assert dumped["output"] == {"echo": "hi"}
    assert dumped["error"] is None


def test_failed_result_requires_error() -> None:
    with pytest.raises(ValidationError, match="error is required when status='failed'"):
        _ok(status="failed", error=None, output=None)


def test_succeeded_result_forbids_error() -> None:
    err = TaskError(code=TaskErrorCode.GRAPH_ERROR, message="x")
    with pytest.raises(ValidationError, match="error must be None when status='succeeded'"):
        _ok(status="succeeded", error=err)


def test_finished_at_must_not_precede_started_at() -> None:
    with pytest.raises(ValidationError, match="finished_at must be >= started_at"):
        _ok(started_at=_T1, finished_at=_T0)


def test_status_literal_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        _ok(status="cancelled")


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskResult.model_validate(
            {
                "task_id": "t",
                "tenant_id": "ten",
                "trace_id": "tr",
                "graph_id": "g",
                "status": "succeeded",
                "output": None,
                "error": None,
                "node_sequence": 0,
                "started_at": _T0,
                "finished_at": _T0,
                "unexpected": "boom",
            }
        )


def test_model_is_frozen() -> None:
    result = _ok()
    with pytest.raises(ValidationError):
        result.task_id = "other"  # type: ignore[misc]


def test_task_error_requires_non_empty_message() -> None:
    with pytest.raises(ValidationError):
        TaskError(code=TaskErrorCode.INTERNAL, message="")


def test_task_error_serialises_code_as_string() -> None:
    err = TaskError(
        code=TaskErrorCode.ABANDONED,
        message="exhausted",
        details={"delivery_count": 4},
    )
    dumped = err.model_dump(mode="json")
    assert dumped["code"] == "abandoned"
    assert dumped["details"] == {"delivery_count": 4}


def test_failed_result_with_error_round_trips() -> None:
    err = TaskError(code=TaskErrorCode.GRAPH_ERROR, message="explode")
    result = _ok(status="failed", output=None, error=err)
    dumped = result.model_dump(mode="json")
    restored = TaskResult.model_validate(dumped)
    assert restored == result
    assert dumped["error"]["code"] == "graph_error"


def test_zero_node_sequence_allowed_for_pre_step_abandon() -> None:
    err = TaskError(code=TaskErrorCode.ABANDONED, message="redelivery exhausted")
    result = _ok(status="failed", output=None, error=err, node_sequence=0)
    assert result.node_sequence == 0


def test_negative_node_sequence_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _ok(node_sequence=-1)


def test_output_accepts_arbitrary_json_dict() -> None:
    payload = {
        "assistant_message": "ok",
        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        "tools": ["search", "shell"],
    }
    result = _ok(output=payload)
    assert result.output == payload


def test_task_error_codes_are_stable_strings() -> None:
    assert TaskErrorCode.GRAPH_ERROR.value == "graph_error"
    assert TaskErrorCode.ABANDONED.value == "abandoned"
    assert TaskErrorCode.INTERNAL.value == "internal"
