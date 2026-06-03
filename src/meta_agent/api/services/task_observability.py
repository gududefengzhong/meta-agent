"""Task-level observability read model builder."""

from __future__ import annotations

from datetime import datetime

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.domain.task import Task
from meta_agent.core.domain.task_observability import (
    TaskObservabilitySummary,
    build_task_observability_summary,
)
from meta_agent.core.ports.llm_usage import LLMUsageRepository, UsageGroupBy
from meta_agent.core.ports.repository import AuditRepository, TaskRepository

_AUDIT_PAGE_SIZE = 500


async def build_task_observability(
    *,
    tenant_id: str,
    task: Task,
    tasks: TaskRepository,
    audits: AuditRepository,
    llm_usage: LLMUsageRepository,
) -> TaskObservabilitySummary:
    """Load and aggregate the persisted telemetry for ``task``."""

    result = await tasks.get_result(tenant_id, task.task_id)
    usage_rows = await llm_usage.list_for_task(tenant_id, task.task_id)
    usage_buckets = await llm_usage.aggregate_for_task(
        tenant_id, task.task_id, UsageGroupBy.STEP_KIND
    )
    audit_rows = await _list_all_task_audits(audits, tenant_id=tenant_id, task_id=task.task_id)
    auto_pr_child_status = await _auto_pr_child_status(
        tasks=tasks,
        task=task,
        audits=audit_rows,
    )
    return build_task_observability_summary(
        task=task,
        result=result,
        usages=usage_rows,
        usage_buckets=usage_buckets,
        audits=audit_rows,
        auto_pr_child_status=auto_pr_child_status,
    )


async def _list_all_task_audits(
    audits: AuditRepository,
    *,
    tenant_id: str,
    task_id: str,
) -> list[AuditEvent]:
    rows: list[AuditEvent] = []
    cursor: tuple[datetime, str] | None = None
    while True:
        page = await audits.list_for_task_since(
            tenant_id,
            task_id,
            after=cursor,
            limit=_AUDIT_PAGE_SIZE,
        )
        if not page:
            break
        rows.extend(page)
        if len(page) < _AUDIT_PAGE_SIZE:
            break
        last = page[-1]
        cursor = (last.occurred_at, last.event_id)
    return rows


async def _auto_pr_child_status(
    *,
    tasks: TaskRepository,
    task: Task,
    audits: list[AuditEvent],
) -> str | None:
    if task.task_type.value != "bug_fix":
        return None
    for event in audits:
        if event.action == "task.chain_failed" and event.payload.get("follow_up_type") == "auto_pr":
            return "chain_failed"
        if (
            event.action == "task.chain_skipped"
            and event.payload.get("follow_up_type") == "auto_pr"
        ):
            reason = event.payload.get("reason")
            if reason == "duplicate":
                return "duplicate"
            return "not_enqueued"
        if (
            event.action != "task.chain_enqueued"
            or event.payload.get("follow_up_type") != "auto_pr"
        ):
            continue
        child_task_id = event.payload.get("child_task_id")
        if not isinstance(child_task_id, str) or not child_task_id:
            return "enqueued"
        child_task = await tasks.get(task.tenant_id, child_task_id)
        if child_task is None:
            return "enqueued"
        child_result = await tasks.get_result(task.tenant_id, child_task_id)
        if child_result is None:
            return "enqueued"
        child_output = child_result.output if isinstance(child_result.output, dict) else {}
        action = child_output.get("action")
        if (
            child_result.status == "succeeded"
            and isinstance(action, str)
            and action in {"created", "reused", "skipped"}
        ):
            return action
        if child_result.status == "failed":
            return "failed"
        return "enqueued"
    return "not_enqueued"
