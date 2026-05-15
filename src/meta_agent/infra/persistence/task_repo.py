"""PostgreSQL implementation of :class:`TaskRepository`."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from meta_agent.core.domain.task import Task, TaskState, TaskType
from meta_agent.core.orchestration.result import TaskResult
from meta_agent.core.ports.repository import (
    TERMINAL_TASK_STATES,
    IllegalTaskTransitionError,
    TaskRepository,
)
from meta_agent.infra.persistence._guard import check_tenant
from meta_agent.infra.persistence.pool import DatabasePool


def _row_to_task(row: dict[str, Any]) -> Task:
    return Task(
        task_id=row["task_id"],
        tenant_id=row["tenant_id"],
        session_id=row["session_id"],
        principal_id=row["principal_id"],
        trace_id=row["trace_id"],
        idempotency_key=row["idempotency_key"],
        task_type=TaskType(row["task_type"]),
        graph_id=row["graph_id"],
        state=TaskState(row["state"]),
        input_payload=row["input_payload"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PgTaskRepository(TaskRepository):
    """asyncpg-backed :class:`TaskRepository`."""

    _UPSERT = """
        INSERT INTO tasks (
            task_id, tenant_id, session_id, principal_id, trace_id,
            idempotency_key, task_type, graph_id, state, input_payload,
            created_at, updated_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12)
        ON CONFLICT (task_id) DO UPDATE SET
            session_id = EXCLUDED.session_id,
            graph_id = EXCLUDED.graph_id,
            state = EXCLUDED.state,
            input_payload = EXCLUDED.input_payload,
            updated_at = EXCLUDED.updated_at
    """

    _GET = "SELECT * FROM tasks WHERE tenant_id = $1 AND task_id = $2"

    _LIST_BY_STATE = (
        "SELECT * FROM tasks WHERE tenant_id = $1 AND state = $2 ORDER BY created_at LIMIT $3"
    )

    _UPDATE_STATE = (
        "UPDATE tasks SET state = $3, updated_at = $4 WHERE tenant_id = $1 AND task_id = $2"
    )

    # Atomic terminal write. The guard ``state NOT IN ('succeeded',
    # 'failed', 'cancelled')`` prevents a redelivered worker from
    # overwriting a result, and lets us treat ``complete()`` as a true
    # state-machine transition rather than a blind upsert.
    _COMPLETE = (
        "UPDATE tasks "
        "SET state = $3, result_json = $4::jsonb, updated_at = $5 "
        "WHERE tenant_id = $1 AND task_id = $2 "
        "AND state NOT IN ('succeeded', 'failed', 'cancelled')"
    )

    _GET_RESULT = "SELECT result_json FROM tasks WHERE tenant_id = $1 AND task_id = $2"

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

    async def upsert(self, task: Task) -> None:
        check_tenant(task.tenant_id)
        async with self._pool.acquire() as conn:
            await conn.execute(
                self._UPSERT,
                task.task_id,
                task.tenant_id,
                task.session_id,
                task.principal_id,
                task.trace_id,
                task.idempotency_key,
                task.task_type.value,
                task.graph_id,
                task.state.value,
                task.input_payload,
                task.created_at,
                task.updated_at,
            )

    async def get(self, tenant_id: str, task_id: str) -> Task | None:
        check_tenant(tenant_id)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(self._GET, tenant_id, task_id)
        return _row_to_task(dict(row)) if row else None

    async def list_by_state(
        self,
        tenant_id: str,
        state: TaskState,
        limit: int = 100,
    ) -> list[Task]:
        check_tenant(tenant_id)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(self._LIST_BY_STATE, tenant_id, state.value, limit)
        return [_row_to_task(dict(r)) for r in rows]

    async def update_state(
        self,
        tenant_id: str,
        task_id: str,
        new_state: TaskState,
        updated_at: datetime,
    ) -> None:
        check_tenant(tenant_id)
        async with self._pool.acquire() as conn:
            await conn.execute(self._UPDATE_STATE, tenant_id, task_id, new_state.value, updated_at)

    async def complete(
        self,
        tenant_id: str,
        task_id: str,
        *,
        result: TaskResult,
        terminal_state: TaskState,
        updated_at: datetime,
    ) -> None:
        check_tenant(tenant_id)
        if terminal_state not in TERMINAL_TASK_STATES:
            raise IllegalTaskTransitionError(
                f"complete() requires a terminal state, got {terminal_state.value!r}"
            )
        payload = json.dumps(result.model_dump(mode="json"))
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                self._COMPLETE,
                tenant_id,
                task_id,
                terminal_state.value,
                payload,
                updated_at,
            )
        # asyncpg returns "UPDATE <n>"; 0 means the WHERE-guard rejected
        # the write (already terminal, or row missing for this tenant).
        if status.endswith(" 0"):
            raise IllegalTaskTransitionError(
                f"task {task_id!r} cannot transition to {terminal_state.value!r}: "
                "row missing or already in a terminal state"
            )

    async def get_result(self, tenant_id: str, task_id: str) -> TaskResult | None:
        check_tenant(tenant_id)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(self._GET_RESULT, tenant_id, task_id)
        if row is None or row["result_json"] is None:
            return None
        raw = row["result_json"]
        # asyncpg may surface JSONB as ``str`` depending on codecs.
        if isinstance(raw, str):
            raw = json.loads(raw)
        return TaskResult.model_validate(raw)
