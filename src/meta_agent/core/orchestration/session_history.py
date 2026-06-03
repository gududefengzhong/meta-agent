"""Reconstruct a prior-message thread from a session's task history.

Phase δ-1 multi-turn: when the worker starts a new task that
belongs to an existing session, it injects the prior conversation
into the graph state under ``_prior_messages`` so the model has
context for follow-ups like "now do the same for the other module".

Source of truth
===============
Messages are *derived* from the task table — we don't store them
twice:

* ``user`` content comes from each prior task's
  ``input_payload["user_prompt"]`` (the same key ``shell_agent``
  and ``bug_fix`` already read)
* ``assistant`` content comes from each prior task's
  :class:`TaskResult.output["assistant_message"]` (the same key
  the graphs already write)

Tasks without either field on either end (e.g. system_echo with no
prompt, or a failed task with no result) are skipped so the
returned thread stays clean. The list is ordered oldest → newest.

Cap
===
Default ``limit`` of 20 task pairs (40 messages) keeps the prompt
size bounded without doing per-token accounting; a future PR can
swap the cap for a token-budget walker once we see real prompt
sizes in production.
"""

from __future__ import annotations

from typing import Any

from meta_agent.core.domain.task import Task, TaskState
from meta_agent.core.ports.repository import TaskRepository

_DEFAULT_PRIOR_LIMIT = 20


async def build_prior_messages(
    task_repo: TaskRepository,
    *,
    tenant_id: str,
    session_id: str,
    exclude_task_id: str,
    limit: int = _DEFAULT_PRIOR_LIMIT,
) -> list[dict[str, str]]:
    """Return the prior (user, assistant) message thread for a session.

    ``exclude_task_id`` is the task currently starting — it must be
    omitted from the prior context (otherwise the model would see
    its own next prompt as already-said).

    Failures to load any one prior task's result are swallowed
    (logged at the call site) so a single broken row doesn't poison
    the whole context fetch.
    """

    if limit <= 0:
        raise ValueError("limit must be > 0")
    tasks = await task_repo.list_by_session(tenant_id, session_id, limit=limit + 1)
    messages: list[dict[str, str]] = []
    for prior in tasks:
        if prior.task_id == exclude_task_id:
            continue
        if prior.state != TaskState.SUCCEEDED:
            # Skip non-terminal / failed tasks; their output is
            # absent or unreliable and we'd rather give the model
            # less context than misleading context.
            continue
        user_content = _user_prompt_of(prior)
        assistant_content = await _assistant_message_of(task_repo, prior)
        if user_content is None or assistant_content is None:
            continue
        messages.append({"role": "user", "content": user_content})
        messages.append({"role": "assistant", "content": assistant_content})
    return messages


def _user_prompt_of(task: Task) -> str | None:
    raw: Any = task.input_payload.get("user_prompt")
    if isinstance(raw, str) and raw:
        return raw
    return None


async def _assistant_message_of(task_repo: TaskRepository, task: Task) -> str | None:
    result = await task_repo.get_result(task.tenant_id, task.task_id)
    if result is None or result.output is None:
        return None
    raw: Any = result.output.get("assistant_message")
    if isinstance(raw, str) and raw:
        return raw
    return None


__all__ = ["build_prior_messages"]
