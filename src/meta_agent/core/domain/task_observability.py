"""Derived observability summary for one task.

This module turns the raw append-only task telemetry already persisted
by the system into a compact read model that product surfaces can
reuse:

* ``audit_events``      -> tool/human-intervention counters
* ``llm_usage_logs``    -> call/token/cost/latency counters
* ``tasks.result_json`` -> verifier / failure / patch outcome

The summary is intentionally product-shaped for the current bug-fix
agent: enough to answer "did it work?", "what did it cost?", and
"where did it fail?" without replaying the full trajectory.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.domain.llm_usage import LLMUsageRecord, LLMUsageStatus
from meta_agent.core.domain.task import BudgetPolicy, Task, TaskState, TaskType
from meta_agent.core.orchestration.result import TaskResult, TaskResultStatus
from meta_agent.core.ports.llm_usage import UsageAggregate

_HUMAN_INTERVENTION_ACTIONS = frozenset({"task.awaiting_approval", "permission.prompted"})
_AUTO_PR_ACTIONS = frozenset({"created", "reused", "skipped"})


@dataclass(frozen=True, slots=True)
class TaskObservabilitySummary:
    """Compact per-task telemetry summary."""

    task_id: str
    state: TaskState
    result_status: TaskResultStatus | None
    verifier_passed: bool | None
    failure_category: str | None
    failure_kind: str | None
    attempts: int | None
    files_changed: tuple[str, ...]
    patch_present: bool
    llm_calls: int
    llm_failures: int
    total_tokens: int
    total_cost_usd_micros: int
    total_latency_ms: int
    tool_events: int
    tool_failures: int
    human_interventions: int
    budget_outcome: str
    auto_pr_child_status: str
    cost_by_step_kind: dict[str, int]
    models: tuple[str, ...]


def build_task_observability_summary(
    *,
    task: Task,
    result: TaskResult | None,
    usages: Sequence[LLMUsageRecord],
    usage_buckets: Sequence[UsageAggregate],
    audits: Sequence[AuditEvent],
    auto_pr_child_status: str | None = None,
) -> TaskObservabilitySummary:
    """Project raw task telemetry into a compact summary."""

    output = result.output if result is not None and isinstance(result.output, dict) else {}
    files_changed_raw = output.get("files_changed")
    files_changed = (
        tuple(item for item in files_changed_raw if isinstance(item, str))
        if isinstance(files_changed_raw, list)
        else ()
    )
    verifier_passed = output.get("verifier_passed")
    attempts = output.get("attempts")
    result_status = result.status if result is not None else None
    tool_events = 0
    tool_failures = 0
    human_interventions = 0
    for event in audits:
        if event.action.startswith("tool."):
            tool_events += 1
            if event.action == "tool.failed":
                tool_failures += 1
        if event.action in _HUMAN_INTERVENTION_ACTIONS:
            human_interventions += 1
    models = tuple(
        sorted(
            {
                model
                for row in usages
                for model in (row.model, row.requested_model)
                if isinstance(model, str) and model.strip()
            }
        )
    )
    return TaskObservabilitySummary(
        task_id=task.task_id,
        state=task.state,
        result_status=result_status,
        verifier_passed=verifier_passed if isinstance(verifier_passed, bool) else None,
        failure_category=_failure_category(result),
        failure_kind=_failure_kind(result),
        attempts=attempts if isinstance(attempts, int) else None,
        files_changed=files_changed,
        patch_present=bool(output.get("patch")),
        llm_calls=len(usages),
        llm_failures=sum(1 for row in usages if row.status is not LLMUsageStatus.OK),
        total_tokens=sum(_int_or_zero(row.total_tokens) for row in usages),
        total_cost_usd_micros=sum(_int_or_zero(row.cost_usd_micros) for row in usages),
        total_latency_ms=sum(_int_or_zero(row.latency_ms) for row in usages),
        tool_events=tool_events,
        tool_failures=tool_failures,
        human_interventions=human_interventions,
        budget_outcome=_budget_outcome(task=task, result=result, audits=audits),
        auto_pr_child_status=auto_pr_child_status or _default_auto_pr_child_status(task),
        cost_by_step_kind={
            bucket.key: bucket.cost_usd_micros
            for bucket in usage_buckets
            if bucket.cost_usd_micros > 0
        },
        models=models,
    )


def build_eval_aggregate(rows: Sequence[TaskObservabilitySummary]) -> dict[str, float | int]:
    """Aggregate per-task summaries into baseline-friendly metrics."""

    cases = len(rows)
    passed = sum(1 for row in rows if row.verifier_passed is True)
    total_tokens = sum(row.total_tokens for row in rows)
    total_cost = sum(row.total_cost_usd_micros for row in rows)
    return {
        "success_rate": passed / cases if cases else 0.0,
        "average_tokens_per_case": total_tokens / cases if cases else 0.0,
        "average_cost_usd_micros_per_case": total_cost / cases if cases else 0.0,
        "tool_failures": sum(row.tool_failures for row in rows),
        "verifier_failures": sum(1 for row in rows if row.verifier_passed is False),
        "human_interventions": sum(row.human_interventions for row in rows),
        "llm_failures": sum(row.llm_failures for row in rows),
    }


def summary_to_json_dict(summary: TaskObservabilitySummary) -> dict[str, Any]:
    """Render a summary into a JSON-safe dict for API / eval output."""

    return {
        "task_id": summary.task_id,
        "state": summary.state.value,
        "result_status": summary.result_status,
        "verifier_passed": summary.verifier_passed,
        "failure_category": summary.failure_category,
        "failure_kind": summary.failure_kind,
        "attempts": summary.attempts,
        "files_changed": list(summary.files_changed),
        "patch_present": summary.patch_present,
        "llm_calls": summary.llm_calls,
        "llm_failures": summary.llm_failures,
        "total_tokens": summary.total_tokens,
        "total_cost_usd_micros": summary.total_cost_usd_micros,
        "total_latency_ms": summary.total_latency_ms,
        "tool_events": summary.tool_events,
        "tool_failures": summary.tool_failures,
        "human_interventions": summary.human_interventions,
        "budget_outcome": summary.budget_outcome,
        "auto_pr_child_status": summary.auto_pr_child_status,
        "cost_by_step_kind": dict(summary.cost_by_step_kind),
        "models": list(summary.models),
    }


def _failure_category(result: TaskResult | None) -> str | None:
    if result is None:
        return None
    output = result.output if isinstance(result.output, dict) else {}
    failure = output.get("failure_explanation")
    if isinstance(failure, Mapping):
        category = failure.get("category")
        if isinstance(category, str) and category.strip():
            return category
    if result.error is not None and isinstance(result.error.details, Mapping):
        category = result.error.details.get("failure_category")
        if isinstance(category, str) and category.strip():
            return category
    if result.error is not None:
        return result.error.code.value
    return None


def _failure_kind(result: TaskResult | None) -> str | None:
    if result is None:
        return None
    output = result.output if isinstance(result.output, dict) else {}
    failure = output.get("failure_explanation")
    if isinstance(failure, Mapping):
        details = failure.get("details")
        if isinstance(details, Mapping):
            kind = details.get("failure_kind")
            if isinstance(kind, str) and kind.strip():
                return kind
    if result.error is not None and isinstance(result.error.details, Mapping):
        kind = result.error.details.get("failure_kind")
        if isinstance(kind, str) and kind.strip():
            return kind
    return None


def _default_auto_pr_child_status(task: Task) -> str:
    if task.task_type is not TaskType.BUG_FIX:
        return "not_applicable"
    return "not_enqueued"


def _budget_outcome(
    *,
    task: Task,
    result: TaskResult | None,
    audits: Sequence[AuditEvent],
) -> str:
    if task.budget_policy is BudgetPolicy.NONE or task.budget_threshold_micros is None:
        return "not_enabled"
    budget_gate_seen = any(
        event.action == "task.awaiting_approval" and event.payload.get("gate_id") == "budget"
        for event in audits
    )
    if task.state is TaskState.AWAITING_APPROVAL and budget_gate_seen:
        return "awaiting_approval"
    error_message = (
        result.error.message if result is not None and result.error is not None else None
    )
    if isinstance(error_message, str):
        if "budget gate rejected by operator" in error_message:
            return "rejected"
        if error_message.startswith("budget exceeded:"):
            return "aborted"
    if budget_gate_seen:
        return "approved"
    return "within_budget"


def _int_or_zero(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0
