"""PostgreSQL implementation of :class:`LLMUsageRepository`.

Append-only, idempotent on ``record_id``. The tenant guard is enforced
on both writes (against the record's own ``tenant_id``) and reads
(against the caller-supplied ``tenant_id``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from meta_agent.core.domain.errors import ErrorCategory
from meta_agent.core.domain.llm_usage import LLMUsageRecord, LLMUsageStatus
from meta_agent.core.ports.budget import BudgetUsage
from meta_agent.core.ports.llm_usage import LLMUsageRepository
from meta_agent.infra.persistence._guard import check_tenant
from meta_agent.infra.persistence.pool import DatabasePool


def _row_to_record(row: dict[str, Any]) -> LLMUsageRecord:
    error_category_raw = row["error_category"]
    return LLMUsageRecord(
        record_id=row["record_id"],
        tenant_id=row["tenant_id"],
        trace_id=row["trace_id"],
        request_id=row["request_id"],
        principal_id=row["principal_id"],
        session_id=row["session_id"],
        task_id=row["task_id"],
        provider=row["provider"],
        model=row["model"],
        requested_model=row["requested_model"],
        prompt_tokens=row["prompt_tokens"],
        completion_tokens=row["completion_tokens"],
        total_tokens=row["total_tokens"],
        finish_reason=row["finish_reason"],
        provider_response_id=row["provider_response_id"],
        cost_usd_micros=row["cost_usd_micros"],
        latency_ms=row["latency_ms"],
        status=LLMUsageStatus(row["status"]),
        error_category=ErrorCategory(error_category_raw) if error_category_raw else None,
        error_message=row["error_message"],
        created_at=row["created_at"],
    )


class PgLLMUsageRepository(LLMUsageRepository):
    """asyncpg-backed append-only :class:`LLMUsageRepository`."""

    _INSERT = """
        INSERT INTO llm_usage_logs (
            record_id, tenant_id, trace_id, request_id, principal_id,
            session_id, task_id, provider, model, requested_model,
            prompt_tokens, completion_tokens, total_tokens,
            finish_reason, provider_response_id,
            cost_usd_micros, latency_ms,
            status, error_category, error_message, created_at
        )
        VALUES (
            $1, $2, $3, $4, $5,
            $6, $7, $8, $9, $10,
            $11, $12, $13,
            $14, $15,
            $16, $17,
            $18, $19, $20, $21
        )
        ON CONFLICT (record_id) DO NOTHING
    """

    _LIST_FOR_TASK = (
        "SELECT * FROM llm_usage_logs WHERE tenant_id = $1 AND task_id = $2 ORDER BY created_at ASC"
    )

    # COALESCE so a NULL token or cost column counts as 0 in the sum.
    # The `ix_llm_usage_tenant_created` index covers (tenant_id,
    # created_at DESC), so PG can use it to skip rows older than the
    # window start.
    _AGGREGATE_SINCE = """
        SELECT
            COALESCE(SUM(total_tokens), 0)::bigint AS tokens,
            COALESCE(SUM(cost_usd_micros), 0)::bigint AS cost_micros
        FROM llm_usage_logs
        WHERE tenant_id = $1
          AND created_at >= $2
    """

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

    async def record(self, record: LLMUsageRecord) -> None:
        check_tenant(record.tenant_id)
        async with self._pool.acquire() as conn:
            await conn.execute(
                self._INSERT,
                record.record_id,
                record.tenant_id,
                record.trace_id,
                record.request_id,
                record.principal_id,
                record.session_id,
                record.task_id,
                record.provider,
                record.model,
                record.requested_model,
                record.prompt_tokens,
                record.completion_tokens,
                record.total_tokens,
                record.finish_reason,
                record.provider_response_id,
                record.cost_usd_micros,
                record.latency_ms,
                record.status.value,
                record.error_category.value if record.error_category is not None else None,
                record.error_message,
                record.created_at,
            )

    async def list_for_task(
        self,
        tenant_id: str,
        task_id: str,
    ) -> list[LLMUsageRecord]:
        check_tenant(tenant_id)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(self._LIST_FOR_TASK, tenant_id, task_id)
        return [_row_to_record(dict(r)) for r in rows]

    async def aggregate_since(
        self,
        tenant_id: str,
        since: datetime,
    ) -> BudgetUsage:
        check_tenant(tenant_id)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(self._AGGREGATE_SINCE, tenant_id, since)
        if row is None:
            return BudgetUsage(tokens_used=0, cost_usd_micros_used=0)
        return BudgetUsage(
            tokens_used=int(row["tokens"]),
            cost_usd_micros_used=int(row["cost_micros"]),
        )
