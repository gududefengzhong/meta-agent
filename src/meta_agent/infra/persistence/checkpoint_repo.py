"""PostgreSQL implementation of :class:`CheckpointRepository`."""

from __future__ import annotations

from typing import Any

from meta_agent.core.domain.checkpoint import TaskCheckpoint
from meta_agent.core.ports.repository import CheckpointRepository
from meta_agent.infra.persistence._guard import check_tenant
from meta_agent.infra.persistence.pool import DatabasePool


def _row_to_checkpoint(row: dict[str, Any]) -> TaskCheckpoint:
    return TaskCheckpoint(
        checkpoint_id=row["checkpoint_id"],
        task_id=row["task_id"],
        tenant_id=row["tenant_id"],
        trace_id=row["trace_id"],
        node_name=row["node_name"],
        sequence=row["sequence"],
        state_snapshot=row["state_snapshot"],
        created_at=row["created_at"],
    )


class PgCheckpointRepository(CheckpointRepository):
    """asyncpg-backed append-only :class:`CheckpointRepository`."""

    _APPEND = """
        INSERT INTO task_checkpoints (
            checkpoint_id, task_id, tenant_id, trace_id, node_name,
            sequence, state_snapshot, created_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
    """

    _LATEST = (
        "SELECT * FROM task_checkpoints WHERE tenant_id = $1 AND task_id = $2 "
        "ORDER BY sequence DESC LIMIT 1"
    )

    _LIST_FOR_TASK = (
        "SELECT * FROM task_checkpoints WHERE tenant_id = $1 AND task_id = $2 ORDER BY sequence"
    )

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

    async def append(self, checkpoint: TaskCheckpoint) -> None:
        check_tenant(checkpoint.tenant_id)
        async with self._pool.acquire() as conn:
            await conn.execute(
                self._APPEND,
                checkpoint.checkpoint_id,
                checkpoint.task_id,
                checkpoint.tenant_id,
                checkpoint.trace_id,
                checkpoint.node_name,
                checkpoint.sequence,
                checkpoint.state_snapshot,
                checkpoint.created_at,
            )

    async def latest(self, tenant_id: str, task_id: str) -> TaskCheckpoint | None:
        check_tenant(tenant_id)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(self._LATEST, tenant_id, task_id)
        return _row_to_checkpoint(dict(row)) if row else None

    async def list_for_task(
        self,
        tenant_id: str,
        task_id: str,
    ) -> list[TaskCheckpoint]:
        check_tenant(tenant_id)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(self._LIST_FOR_TASK, tenant_id, task_id)
        return [_row_to_checkpoint(dict(r)) for r in rows]
