"""asyncpg-backed :class:`TrajectoryRepository` (Phase γ-B-1).

The merged trajectory is built from three independent SELECTs rather
than a SQL UNION because:

* Each source table has a different timestamp column name
  (``occurred_at`` / ``created_at`` / ``created_at``) and a different
  projected shape, so a UNION would need three SELECT-list adapters
  before it could even merge.
* The three queries hit different secondary indexes
  (``ix_audit_tenant_occurred``,
  ``ix_checkpoints_tenant_task``, ``ix_llm_usage_logs_tenant_task``),
  and Postgres treats each fetch as an independent index scan.
* Merging in Python keeps the merge logic colocated with the
  pydantic model construction, which keeps type-checking honest.

Pagination is intentionally **not** offered at γ-B-1: each source is
capped at ``limit_per_source`` rows and the page surfaces
``truncated=True`` when any cap was hit. The full drill-down APIs
land in γ-B-2 / γ-C.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from meta_agent.core.domain.trajectory import (
    TrajectoryAuditItem,
    TrajectoryCheckpointItem,
    TrajectoryItem,
    TrajectoryPage,
    TrajectoryUsageItem,
)
from meta_agent.core.ports.trajectory import TrajectoryRepository
from meta_agent.infra.persistence._guard import check_tenant
from meta_agent.infra.persistence.pool import DatabasePool


class PgTrajectoryRepository(TrajectoryRepository):
    """Read-only Postgres adapter merging audit / checkpoint / usage rows."""

    _AUDIT = """
        SELECT event_id, action, payload, occurred_at
        FROM audit_events
        WHERE tenant_id = $1 AND task_id = $2
          AND ($3::timestamptz IS NULL OR occurred_at > $3)
        ORDER BY occurred_at ASC
        LIMIT $4
    """

    _CHECKPOINT = """
        SELECT checkpoint_id, sequence, node_name, state_snapshot, created_at
        FROM task_checkpoints
        WHERE tenant_id = $1 AND task_id = $2
          AND ($3::timestamptz IS NULL OR created_at > $3)
        ORDER BY created_at ASC, sequence ASC
        LIMIT $4
    """

    _USAGE = """
        SELECT record_id, provider, model, requested_model,
               prompt_tokens, completion_tokens, total_tokens,
               cost_usd_micros, latency_ms, status,
               error_category, error_message,
               prompt_id, prompt_version, prompt_excerpt, step_kind,
               created_at
        FROM llm_usage_logs
        WHERE tenant_id = $1 AND task_id = $2
          AND ($3::timestamptz IS NULL OR created_at > $3)
        ORDER BY created_at ASC
        LIMIT $4
    """

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

    async def list_for_task(
        self,
        tenant_id: str,
        task_id: str,
        *,
        since: datetime | None = None,
        limit_per_source: int = 1000,
    ) -> TrajectoryPage:
        check_tenant(tenant_id)
        if limit_per_source <= 0:
            raise ValueError("limit_per_source must be positive")
        async with self._pool.acquire() as conn:
            audit_rows = await conn.fetch(self._AUDIT, tenant_id, task_id, since, limit_per_source)
            checkpoint_rows = await conn.fetch(
                self._CHECKPOINT, tenant_id, task_id, since, limit_per_source
            )
            usage_rows = await conn.fetch(self._USAGE, tenant_id, task_id, since, limit_per_source)
        truncated = (
            len(audit_rows) >= limit_per_source
            or len(checkpoint_rows) >= limit_per_source
            or len(usage_rows) >= limit_per_source
        )
        items: list[TrajectoryItem] = []
        items.extend(_audit_to_item(dict(r)) for r in audit_rows)
        items.extend(_checkpoint_to_item(dict(r)) for r in checkpoint_rows)
        items.extend(_usage_to_item(dict(r)) for r in usage_rows)
        # Stable sort: same-timestamp rows keep their source order so a
        # checkpoint written immediately before its audit hook does not
        # appear out of causal order in the rendered timeline.
        items.sort(key=lambda item: item.occurred_at)
        return TrajectoryPage(items=tuple(items), truncated=truncated)


def _audit_to_item(row: dict[str, Any]) -> TrajectoryAuditItem:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return TrajectoryAuditItem(
        occurred_at=row["occurred_at"],
        event_id=row["event_id"],
        action=row["action"],
        payload=payload or {},
    )


def _checkpoint_to_item(row: dict[str, Any]) -> TrajectoryCheckpointItem:
    snapshot = row["state_snapshot"]
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    snapshot = snapshot or {}
    return TrajectoryCheckpointItem(
        occurred_at=row["created_at"],
        checkpoint_id=row["checkpoint_id"],
        sequence=row["sequence"],
        node_name=row["node_name"],
        current_node=snapshot.get("current_node"),
        awaiting_approval=bool(snapshot.get("awaiting_approval", False)),
        finished=bool(snapshot.get("finished", False)),
    )


def _usage_to_item(row: dict[str, Any]) -> TrajectoryUsageItem:
    return TrajectoryUsageItem(
        occurred_at=row["created_at"],
        record_id=row["record_id"],
        provider=row["provider"],
        model=row["model"],
        requested_model=row["requested_model"],
        prompt_tokens=row["prompt_tokens"],
        completion_tokens=row["completion_tokens"],
        total_tokens=row["total_tokens"],
        cost_usd_micros=row["cost_usd_micros"],
        latency_ms=row["latency_ms"],
        status=row["status"],
        error_category=row["error_category"],
        error_message=row["error_message"],
        prompt_id=row["prompt_id"],
        prompt_version=row["prompt_version"],
        prompt_excerpt=row.get("prompt_excerpt"),
        step_kind=row["step_kind"],
    )
