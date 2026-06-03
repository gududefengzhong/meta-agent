"""Task API response mappers.

Keeps HTTP response shaping out of the routers so command/query
handlers stay focused on orchestration and error handling.
"""

from __future__ import annotations

from meta_agent.api.schemas import TaskResponse, TaskResultResponse
from meta_agent.core.domain.task import Task
from meta_agent.core.orchestration.result import TaskResult


def to_task_response(task: Task) -> TaskResponse:
    return TaskResponse(
        task_id=task.task_id,
        tenant_id=task.tenant_id,
        state=task.state,
        task_type=task.task_type,
        trace_id=task.trace_id,
        session_id=task.session_id,
        permission_mode=task.permission_mode,
        budget_policy=task.budget_policy,
        budget_threshold_micros=task.budget_threshold_micros,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def to_task_result_response(result: TaskResult) -> TaskResultResponse:
    return TaskResultResponse(
        task_id=result.task_id,
        status=result.status,
        graph_id=result.graph_id,
        output=result.output,
        error=result.error,
        node_sequence=result.node_sequence,
        started_at=result.started_at,
        finished_at=result.finished_at,
    )
