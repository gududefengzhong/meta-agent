"""Unit tests for :func:`build_prior_messages`.

Covers the message-thread reconstruction contract:

* succeeded tasks contribute one ``user`` + one ``assistant`` message
* failed / non-terminal tasks are skipped (incomplete output)
* tasks missing ``user_prompt`` / ``assistant_message`` are skipped
* the currently-starting task is excluded so it doesn't see its
  own next prompt as already-said
* messages are ordered oldest → newest
* tenant scoping: a task belonging to another tenant never leaks
  through (enforced by the repository, exercised here for coverage)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from meta_agent.core.domain.task import (
    BudgetPolicy,
    PermissionMode,
    Task,
    TaskState,
    TaskType,
)
from meta_agent.core.orchestration.result import TaskResult
from meta_agent.core.orchestration.session_history import build_prior_messages
from tests.worker._fakes import FakeTaskRepo


def _task(
    *,
    task_id: str,
    tenant_id: str = "t-1",
    session_id: str | None = "s-1",
    state: TaskState = TaskState.SUCCEEDED,
    user_prompt: str | None = "hello",
    created_at: datetime | None = None,
) -> Task:
    payload: dict[str, Any] = {}
    if user_prompt is not None:
        payload["user_prompt"] = user_prompt
    return Task(
        task_id=task_id,
        tenant_id=tenant_id,
        principal_id="p-1",
        trace_id=f"tr-{task_id}",
        session_id=session_id,
        idempotency_key=f"idem-{task_id}",
        task_type=TaskType.SYSTEM_CHAT,
        graph_id=None,
        state=state,
        permission_mode=PermissionMode.AUTO,
        budget_policy=BudgetPolicy.NONE,
        input_payload=payload,
        created_at=created_at or datetime(2026, 6, 23, tzinfo=UTC),
        updated_at=created_at or datetime(2026, 6, 23, tzinfo=UTC),
    )


def _result(task: Task, *, assistant_message: str | None = "ok") -> TaskResult:
    output: dict[str, Any] = {}
    if assistant_message is not None:
        output["assistant_message"] = assistant_message
    return TaskResult(
        task_id=task.task_id,
        tenant_id=task.tenant_id,
        trace_id=task.trace_id,
        graph_id="builtin.simple_chat",
        status="succeeded",
        output=output,
        error=None,
        node_sequence=1,
        started_at=task.created_at,
        finished_at=task.created_at,
    )


async def test_returns_empty_when_session_has_no_prior_tasks() -> None:
    repo = FakeTaskRepo()
    messages = await build_prior_messages(
        repo, tenant_id="t-1", session_id="s-1", exclude_task_id="task-current"
    )
    assert messages == []


async def test_returns_user_assistant_pair_for_completed_prior_task() -> None:
    repo = FakeTaskRepo()
    prior = _task(task_id="task-1", user_prompt="add a button")
    await repo.upsert(prior)
    repo.results[(prior.tenant_id, prior.task_id)] = _result(
        prior, assistant_message="here is the button"
    )

    messages = await build_prior_messages(
        repo, tenant_id="t-1", session_id="s-1", exclude_task_id="task-current"
    )

    assert len(messages) == 2
    assert messages[0] == {"role": "user", "content": "add a button"}
    assert messages[1] == {"role": "assistant", "content": "here is the button"}


async def test_orders_messages_oldest_first() -> None:
    repo = FakeTaskRepo()
    t1 = _task(
        task_id="task-1",
        user_prompt="first ask",
        created_at=datetime(2026, 6, 23, 12, 0, tzinfo=UTC),
    )
    t2 = _task(
        task_id="task-2",
        user_prompt="second ask",
        created_at=datetime(2026, 6, 23, 12, 5, tzinfo=UTC),
    )
    await repo.upsert(t1)
    await repo.upsert(t2)
    repo.results[(t1.tenant_id, t1.task_id)] = _result(t1, assistant_message="first reply")
    repo.results[(t2.tenant_id, t2.task_id)] = _result(t2, assistant_message="second reply")

    messages = await build_prior_messages(
        repo, tenant_id="t-1", session_id="s-1", exclude_task_id="task-current"
    )

    contents = [m["content"] for m in messages]
    assert contents == ["first ask", "first reply", "second ask", "second reply"]


async def test_excludes_the_currently_starting_task() -> None:
    repo = FakeTaskRepo()
    prior = _task(task_id="task-1", user_prompt="earlier")
    current = _task(
        task_id="task-current",
        user_prompt="now",
        created_at=datetime(2026, 6, 23, 12, 5, tzinfo=UTC),
        state=TaskState.PENDING,  # not yet completed
    )
    await repo.upsert(prior)
    await repo.upsert(current)
    repo.results[(prior.tenant_id, prior.task_id)] = _result(prior, assistant_message="ok")

    messages = await build_prior_messages(
        repo, tenant_id="t-1", session_id="s-1", exclude_task_id="task-current"
    )

    # Only the prior task contributes; the current task is skipped
    # AND it isn't succeeded so it would be filtered anyway.
    assert len(messages) == 2
    assert messages[0]["content"] == "earlier"


async def test_skips_non_succeeded_prior_tasks() -> None:
    repo = FakeTaskRepo()
    succeeded = _task(task_id="task-ok", user_prompt="worked")
    failed = _task(task_id="task-fail", user_prompt="failed", state=TaskState.FAILED)
    await repo.upsert(succeeded)
    await repo.upsert(failed)
    repo.results[(succeeded.tenant_id, succeeded.task_id)] = _result(
        succeeded, assistant_message="all good"
    )

    messages = await build_prior_messages(
        repo, tenant_id="t-1", session_id="s-1", exclude_task_id="task-current"
    )
    assert [m["content"] for m in messages] == ["worked", "all good"]


async def test_skips_tasks_missing_assistant_message_in_result() -> None:
    repo = FakeTaskRepo()
    task = _task(task_id="task-1", user_prompt="hello")
    await repo.upsert(task)
    repo.results[(task.tenant_id, task.task_id)] = _result(task, assistant_message=None)

    messages = await build_prior_messages(
        repo, tenant_id="t-1", session_id="s-1", exclude_task_id="task-current"
    )
    assert messages == []


async def test_skips_tasks_missing_user_prompt_in_payload() -> None:
    repo = FakeTaskRepo()
    task = _task(task_id="task-1", user_prompt=None)
    await repo.upsert(task)
    repo.results[(task.tenant_id, task.task_id)] = _result(task, assistant_message="x")

    messages = await build_prior_messages(
        repo, tenant_id="t-1", session_id="s-1", exclude_task_id="task-current"
    )
    assert messages == []


async def test_tenant_isolated_via_repository() -> None:
    repo = FakeTaskRepo()
    same_session_other_tenant = _task(
        task_id="task-other-tenant", tenant_id="t-2", user_prompt="not yours"
    )
    same_tenant = _task(task_id="task-1", tenant_id="t-1", user_prompt="yours")
    await repo.upsert(same_session_other_tenant)
    await repo.upsert(same_tenant)
    repo.results[(same_tenant.tenant_id, same_tenant.task_id)] = _result(
        same_tenant, assistant_message="reply"
    )
    repo.results[(same_session_other_tenant.tenant_id, same_session_other_tenant.task_id)] = (
        _result(same_session_other_tenant, assistant_message="cross-tenant reply")
    )

    messages = await build_prior_messages(
        repo, tenant_id="t-1", session_id="s-1", exclude_task_id="task-current"
    )
    contents = [m["content"] for m in messages]
    assert "not yours" not in contents
    assert "cross-tenant reply" not in contents
    assert contents == ["yours", "reply"]


async def test_zero_limit_rejected() -> None:
    repo = FakeTaskRepo()
    with pytest.raises(ValueError, match="limit must be > 0"):
        await build_prior_messages(
            repo, tenant_id="t-1", session_id="s-1", exclude_task_id="task-current", limit=0
        )
