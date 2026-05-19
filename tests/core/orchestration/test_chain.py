"""Unit tests for :mod:`meta_agent.core.orchestration.chain`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from meta_agent.core.domain.task import Task, TaskState, TaskType
from meta_agent.core.orchestration.chain import (
    TaskChainRegistry,
    bug_fix_to_auto_pr_policy,
)
from meta_agent.core.orchestration.result import TaskResult

_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)


def _make_parent(
    *,
    task_type: TaskType = TaskType.BUG_FIX,
    issue_description: str = "greet should add a punctuation mark\n\nLong details follow.",
) -> Task:
    return Task(
        task_id="parent-1",
        tenant_id="tenant-1",
        principal_id="user-1",
        trace_id="trace-1",
        idempotency_key="parent-idem",
        task_type=task_type,
        state=TaskState.SUCCEEDED,
        input_payload={
            "issue_description": issue_description,
            "target_files": ["buggy.py"],
            "repo_url": "https://github.com/example/repo.git",
            "base_ref": "main",
        },
        created_at=_NOW,
        updated_at=_NOW,
    )


def _make_result(
    *,
    status: str = "succeeded",
    output: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> TaskResult:
    # The model validator forbids ``error=None`` when status='failed',
    # so failure cases supply a TaskError; the policy still only reads
    # ``status`` + ``output`` so the exact error code is irrelevant.
    from meta_agent.core.orchestration.result import TaskError, TaskErrorCode

    err = None
    if status == "failed":
        err = TaskError(
            code=TaskErrorCode(error_code or TaskErrorCode.GRAPH_ERROR.value),
            message="boom",
        )
    return TaskResult(
        task_id="parent-1",
        tenant_id="tenant-1",
        trace_id="trace-1",
        graph_id="builtin.bug_fix",
        status=status,
        output=output,
        error=err,
        node_sequence=5,
        started_at=_NOW,
        finished_at=_NOW,
    )


def _default_output(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "branch": "agent/parent-1",
        "commit_sha": "abc123",
        "files_changed": ["buggy.py"],
        "diff_stat": " 1 file changed, 1 insertion(+)",
        "verifier_passed": True,
        "verifier_output": "All checks passed",
        "repo_url": "https://github.com/example/repo.git",
        "base_ref": "main",
        "head_branch": "agent/parent-1",
        "head_commit_sha": "abc123",
        "pushed": True,
        "push_skip_reason": None,
    }
    base.update(overrides)
    return base


def test_bug_fix_pushed_yields_auto_pr_spec() -> None:
    parent = _make_parent()
    result = _make_result(output=_default_output())

    spec = bug_fix_to_auto_pr_policy(parent, result)

    assert spec is not None
    assert spec.task_type is TaskType.AUTO_PR
    assert spec.idempotency_key == "chain:parent-1:auto_pr"
    assert spec.topic == "task.commands"
    payload = spec.input_payload
    assert payload["repo_url"] == "https://github.com/example/repo.git"
    assert payload["head_commit_sha"] == "abc123"
    assert payload["head_branch"] == "agent/parent-1"
    assert payload["base_ref"] == "main"
    assert payload["verifier_passed"] is True
    assert payload["issue_title"] == "greet should add a punctuation mark"
    assert payload["_parent_task_id"] == "parent-1"


def test_skip_when_parent_is_not_bug_fix() -> None:
    parent = _make_parent(task_type=TaskType.CODE_REVIEW)
    assert bug_fix_to_auto_pr_policy(parent, _make_result(output=_default_output())) is None


def test_skip_when_status_failed() -> None:
    parent = _make_parent()
    result = _make_result(status="failed", output=None)
    assert bug_fix_to_auto_pr_policy(parent, result) is None


def test_skip_when_pushed_false() -> None:
    parent = _make_parent()
    out = _default_output(pushed=False, push_skip_reason="no_token")
    assert bug_fix_to_auto_pr_policy(parent, _make_result(output=out)) is None


def test_skip_when_repo_url_missing() -> None:
    parent = _make_parent()
    out = _default_output(repo_url=None)
    assert bug_fix_to_auto_pr_policy(parent, _make_result(output=out)) is None


def test_skip_when_head_commit_sha_missing() -> None:
    parent = _make_parent()
    out = _default_output(head_commit_sha=None)
    assert bug_fix_to_auto_pr_policy(parent, _make_result(output=out)) is None


def test_issue_title_truncated_to_72_chars() -> None:
    long_issue = "x" * 200
    parent = _make_parent(issue_description=long_issue)
    spec = bug_fix_to_auto_pr_policy(parent, _make_result(output=_default_output()))
    assert spec is not None
    assert len(spec.input_payload["issue_title"]) == 72  # type: ignore[arg-type]


def test_issue_title_fallback_when_description_empty() -> None:
    parent = _make_parent(issue_description="")
    spec = bug_fix_to_auto_pr_policy(parent, _make_result(output=_default_output()))
    assert spec is not None
    assert spec.input_payload["issue_title"] == "bug fix"


def test_registry_routes_by_task_type() -> None:
    registry = TaskChainRegistry()
    registry.register(TaskType.BUG_FIX, bug_fix_to_auto_pr_policy)

    parent = _make_parent()
    spec = registry.derive(parent, _make_result(output=_default_output()))
    assert spec is not None and spec.task_type is TaskType.AUTO_PR


def test_registry_returns_none_for_unregistered_type() -> None:
    registry = TaskChainRegistry()
    parent = _make_parent()
    assert registry.derive(parent, _make_result(output=_default_output())) is None


@pytest.mark.parametrize(
    "field",
    ["head_branch", "base_ref"],
)
def test_skip_when_handoff_field_missing(field: str) -> None:
    parent = _make_parent()
    out = _default_output(**{field: None})
    assert bug_fix_to_auto_pr_policy(parent, _make_result(output=out)) is None
