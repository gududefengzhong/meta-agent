"""Task-chain policies and registry.

A chain policy decides, given a parent :class:`Task` plus its terminal
:class:`TaskResult`, whether to enqueue a follow-up task and what its
input payload looks like. The runtime side of the chain
(persistence, idempotency, audit) lives in
:mod:`meta_agent.core.ports.task_submitter` and its adapters; this
module is policy-only and free of side effects.

The v1 chain shipped here is ``BUG_FIX`` ‚Üí ``AUTO_PR``: once a bug-fix
run pushes its commit successfully, an ``AUTO_PR`` follow-up is
materialised so the platform can open the pull request without an
operator round-trip.
"""

from __future__ import annotations

from collections.abc import Callable

from meta_agent.core.domain.task import Task, TaskType
from meta_agent.core.orchestration.result import TaskResult
from meta_agent.core.ports.task_submitter import FollowUpSpec

TaskChainPolicy = Callable[[Task, TaskResult], FollowUpSpec | None]
"""A pure function mapping ``(parent, result)`` to an optional follow-up."""

_DEFAULT_TASK_TOPIC = "task.commands"
_MAX_ISSUE_TITLE_LEN = 72


def _derive_issue_title(issue_description: str) -> str:
    """Pick a commit-message-style title from a free-form issue body.

    ``AUTO_PR`` requires a non-empty ``issue_title`` but ``BUG_FIX``
    does not collect one upstream. Reusing the same first-line/72-char
    convention that ``bug_fix.patch`` already applies to its synthetic
    commit messages keeps the title shape consistent across both
    surfaces.
    """
    first_line = issue_description.splitlines()[0].strip() if issue_description else ""
    if not first_line:
        first_line = "bug fix"
    return first_line[:_MAX_ISSUE_TITLE_LEN]


def bug_fix_to_auto_pr_policy(parent: Task, result: TaskResult) -> FollowUpSpec | None:
    """Spawn an ``AUTO_PR`` follow-up when ``BUG_FIX`` pushed cleanly.

    The policy filters out parents that ``AUTO_PR`` would itself skip
    (missing repo URL, missing commit SHA, failed verifier) so the
    chain does not create tasks whose only purpose is to no-op. The
    parent task id is threaded into ``input_payload._parent_task_id``
    as the v1 link convention; a future schema migration may promote
    it to a first-class column.
    """
    if parent.task_type is not TaskType.BUG_FIX:
        return None
    if result.status != "succeeded" or result.output is None:
        return None
    output = result.output
    if not bool(output.get("pushed")):
        return None
    repo_url = output.get("repo_url")
    head_commit_sha = output.get("head_commit_sha")
    head_branch = output.get("head_branch")
    base_ref = output.get("base_ref")
    if not isinstance(repo_url, str) or not repo_url:
        return None
    if not isinstance(head_commit_sha, str) or not head_commit_sha:
        return None
    if not isinstance(head_branch, str) or not head_branch:
        return None
    if not isinstance(base_ref, str) or not base_ref:
        return None

    raw_issue = parent.input_payload.get("issue_description", "")
    issue_description = raw_issue if isinstance(raw_issue, str) else ""
    issue_title = _derive_issue_title(issue_description)

    payload: dict[str, object] = {
        "repo_url": repo_url,
        "base_ref": base_ref,
        "head_branch": head_branch,
        "head_commit_sha": head_commit_sha,
        "verifier_passed": bool(output.get("verifier_passed", False)),
        "verifier_output": str(output.get("verifier_output", "")),
        "diff_stat": str(output.get("diff_stat", "")),
        "issue_title": issue_title,
        "issue_description": issue_description,
        "_parent_task_id": parent.task_id,
    }
    return FollowUpSpec(
        task_type=TaskType.AUTO_PR,
        input_payload=payload,
        idempotency_key=f"chain:{parent.task_id}:auto_pr",
        topic=_DEFAULT_TASK_TOPIC,
    )


class TaskChainRegistry:
    """Maps a completed parent's :class:`TaskType` to a chain policy.

    Empty by default; callers register policies during process wiring.
    Resolution is single-policy-per-type (no fan-out in v1): if a
    parent type is not registered, the runner skips the chain hook
    entirely.
    """

    def __init__(self) -> None:
        self._policies: dict[TaskType, TaskChainPolicy] = {}

    def register(self, task_type: TaskType, policy: TaskChainPolicy) -> None:
        """Register ``policy`` as the follow-up source for ``task_type``."""
        self._policies[task_type] = policy

    def derive(self, parent: Task, result: TaskResult) -> FollowUpSpec | None:
        """Return the follow-up spec, or ``None`` if no chain fires."""
        policy = self._policies.get(parent.task_type)
        if policy is None:
            return None
        return policy(parent, result)
