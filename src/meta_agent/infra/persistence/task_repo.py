"""PostgreSQL implementation of :class:`TaskRepository`."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import asyncpg

from meta_agent.core.domain.task import (
    BudgetPolicy,
    PermissionMode,
    Task,
    TaskState,
    TaskType,
)
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
        permission_mode=PermissionMode(row.get("permission_mode") or "auto"),
        budget_policy=BudgetPolicy(row.get("budget_policy") or "none"),
        budget_threshold_micros=row.get("budget_threshold_micros"),
        input_payload=row["input_payload"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PgTaskRepository(TaskRepository):
    """asyncpg-backed :class:`TaskRepository`."""

    _UPSERT = """
        INSERT INTO tasks (
            task_id, tenant_id, session_id, principal_id, trace_id,
            idempotency_key, task_type, graph_id, state,
            permission_mode, budget_policy, budget_threshold_micros,
            input_payload, created_at, updated_at
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9,
            $10, $11, $12,
            $13::jsonb, $14, $15
        )
        ON CONFLICT (task_id) DO UPDATE SET
            session_id = EXCLUDED.session_id,
            graph_id = EXCLUDED.graph_id,
            state = EXCLUDED.state,
            permission_mode = EXCLUDED.permission_mode,
            budget_policy = EXCLUDED.budget_policy,
            budget_threshold_micros = EXCLUDED.budget_threshold_micros,
            input_payload = EXCLUDED.input_payload,
            updated_at = EXCLUDED.updated_at
    """

    _GET = "SELECT * FROM tasks WHERE tenant_id = $1 AND task_id = $2"

    _LIST_BY_STATE = (
        "SELECT * FROM tasks WHERE tenant_id = $1 AND state = $2 ORDER BY created_at LIMIT $3"
    )

    # Cross-tenant scan used by worker startup to find tasks that were
    # mid-run when the worker died. No tenant_id filter on purpose: the
    # call site is the dispatcher, not a tenant-bound request handler.
    _LIST_BY_STATE_CROSS_TENANT = (
        "SELECT * FROM tasks WHERE state = $1 ORDER BY created_at LIMIT $2"
    )

    # γ-C sweeper: pick stale AWAITING_APPROVAL rows by updated_at.
    # The partial index ``ix_tasks_awaiting_approval`` (added in
    # migration 0008) covers this exactly: WHERE state='awaiting_approval'
    # with (tenant_id, updated_at) sort order. The cross-tenant scan
    # doesn't filter on tenant so it reads the index sequentially.
    _LIST_AWAITING_OLDER_THAN = (
        "SELECT * FROM tasks WHERE state = 'awaiting_approval' "
        "AND updated_at < $1 ORDER BY updated_at LIMIT $2"
    )

    _UPDATE_STATE = (
        "UPDATE tasks SET state = $3, updated_at = $4 WHERE tenant_id = $1 AND task_id = $2"
    )

    # Phase γ-A pause transition. Atomic guard on ``state = 'running'``
    # so a redelivered worker cannot pause a task that another worker
    # has already advanced past the gate or completed.
    _SET_AWAITING_APPROVAL = (
        "UPDATE tasks SET state = 'awaiting_approval', updated_at = $3 "
        "WHERE tenant_id = $1 AND task_id = $2 AND state = 'running'"
    )

    # Phase γ-A resume transition. Atomic guard on
    # ``state = 'awaiting_approval'`` so two operators racing through
    # the approve API cannot both flip the row to RUNNING (the second
    # write becomes a no-op and we surface it as an illegal
    # transition).
    _TRANSITION_FROM_AWAITING = (
        "UPDATE tasks SET state = $3, updated_at = $4 "
        "WHERE tenant_id = $1 AND task_id = $2 AND state = 'awaiting_approval'"
    )

    # Atomic terminal write. The guard ``state NOT IN ('succeeded',
    # 'failed', 'cancelled', 'expired')`` prevents a redelivered
    # worker from overwriting a result, and lets us treat
    # ``complete()`` as a true state-machine transition rather than a
    # blind upsert.
    _COMPLETE = (
        "UPDATE tasks "
        "SET state = $3, result_json = $4::jsonb, updated_at = $5 "
        "WHERE tenant_id = $1 AND task_id = $2 "
        "AND state NOT IN ('succeeded', 'failed', 'cancelled', 'expired')"
    )

    _GET_RESULT = "SELECT result_json FROM tasks WHERE tenant_id = $1 AND task_id = $2"

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

    async def upsert(self, task: Task) -> None:
        check_tenant(task.tenant_id)
        async with self._pool.acquire() as conn:
            await self.upsert_in_conn(task, conn)

    async def upsert_in_conn(
        self,
        task: Task,
        conn: asyncpg.Connection[Any],
    ) -> None:
        """Run :sql:`UPSERT` on an externally-supplied connection.

        Lets callers compose the task write with another statement inside
        a single PG transaction (e.g. the outbox row that makes submit
        atomic). The tenant guard still applies.
        """
        check_tenant(task.tenant_id)
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
            task.permission_mode.value,
            task.budget_policy.value,
            task.budget_threshold_micros,
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

    async def list_awaiting_approval_older_than(
        self,
        threshold_at: datetime,
        *,
        limit: int = 100,
    ) -> list[Task]:
        """Cross-tenant stale-pause scan (γ-C sweeper)."""

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(self._LIST_AWAITING_OLDER_THAN, threshold_at, limit)
        return [_row_to_task(dict(r)) for r in rows]

    async def list_running_for_resume(self, limit: int = 100) -> list[Task]:
        """Cross-tenant scan of tasks left in ``RUNNING`` by a crashed worker.

        Used at worker startup before any tenant context is bound, so
        the call deliberately skips :func:`check_tenant`. Callers must
        bind a per-task context before doing anything with each row.
        """

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                self._LIST_BY_STATE_CROSS_TENANT,
                TaskState.RUNNING.value,
                limit,
            )
        return [_row_to_task(dict(r)) for r in rows]

    async def set_awaiting_approval(
        self,
        tenant_id: str,
        task_id: str,
        updated_at: datetime,
    ) -> None:
        """Atomically transition ``RUNNING`` → ``AWAITING_APPROVAL``.

        Raises :class:`IllegalTaskTransitionError` when the WHERE guard
        rejects the write (row missing for this tenant, or task is no
        longer in ``RUNNING`` — e.g. another worker already finished
        it). The guard makes the pause transition safe under redelivery.
        """

        check_tenant(tenant_id)
        async with self._pool.acquire() as conn:
            status = await conn.execute(self._SET_AWAITING_APPROVAL, tenant_id, task_id, updated_at)
        if status.endswith(" 0"):
            raise IllegalTaskTransitionError(
                f"task {task_id!r} cannot transition to AWAITING_APPROVAL: "
                "row missing or not in RUNNING"
            )

    async def transition_from_awaiting_approval(
        self,
        tenant_id: str,
        task_id: str,
        new_state: TaskState,
        updated_at: datetime,
        *,
        conn: asyncpg.Connection[Any] | None = None,
    ) -> None:
        """Atomic transition out of ``AWAITING_APPROVAL``.

        ``new_state`` is whatever the resume path wants the task to
        become — typically ``RUNNING`` for approve, ``CANCELLED`` for
        abort, or ``EXPIRED`` for the long-tail sweeper.

        Raises :class:`IllegalTaskTransitionError` when the WHERE guard
        rejects the write (row not currently in
        ``AWAITING_APPROVAL`` — e.g. two operators raced approve and
        the loser sees the second write succeed against ``RUNNING``).
        """

        check_tenant(tenant_id)
        if conn is None:
            async with self._pool.acquire() as inner_conn:
                status = await inner_conn.execute(
                    self._TRANSITION_FROM_AWAITING,
                    tenant_id,
                    task_id,
                    new_state.value,
                    updated_at,
                )
        else:
            status = await conn.execute(
                self._TRANSITION_FROM_AWAITING,
                tenant_id,
                task_id,
                new_state.value,
                updated_at,
            )
        if status.endswith(" 0"):
            raise IllegalTaskTransitionError(
                f"task {task_id!r} cannot transition from AWAITING_APPROVAL to "
                f"{new_state.value!r}: row missing or in a different state"
            )

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
