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
from meta_agent.core.ports.llm_usage import (
    LLMUsageFilter,
    LLMUsageRepository,
    UsageAggregate,
    UsageGroupBy,
)
from meta_agent.infra.persistence._guard import check_tenant
from meta_agent.infra.persistence.pool import DatabasePool

# ``aggregate_grouped`` SQL is templated by ``group_by``; the key
# expression is fixed per bucket (NULL collapses to a sentinel so
# unattributed rows still surface), every other column / WHERE clause
# is identical, so a single string-format template is safer than a
# generic builder. The ``key_expr`` values are server-side identifiers
# / literals — they never carry caller input.
_AGG_KEY_EXPRS: dict[UsageGroupBy, str] = {
    UsageGroupBy.MODEL: "COALESCE(model, 'unknown')",
    UsageGroupBy.DAY: "to_char(date_trunc('day', created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD')",
    UsageGroupBy.TASK: "COALESCE(task_id, 'unattributed')",
    UsageGroupBy.PRINCIPAL: "COALESCE(principal_id, 'unattributed')",
    UsageGroupBy.STEP_KIND: "COALESCE(step_kind, 'unspecified')",
}


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
        prompt_id=row.get("prompt_id"),
        prompt_version=row.get("prompt_version"),
        step_kind=row.get("step_kind"),
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
            prompt_id, prompt_version, step_kind,
            cost_usd_micros, latency_ms,
            status, error_category, error_message, created_at
        )
        VALUES (
            $1, $2, $3, $4, $5,
            $6, $7, $8, $9, $10,
            $11, $12, $13,
            $14, $15,
            $16, $17, $18,
            $19, $20,
            $21, $22, $23, $24
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
                record.prompt_id,
                record.prompt_version,
                record.step_kind,
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

    async def list_filtered(
        self,
        tenant_id: str,
        filt: LLMUsageFilter,
    ) -> list[LLMUsageRecord]:
        check_tenant(tenant_id)
        # ``$1..$3`` always bind ``tenant_id`` / ``since`` / ``until``;
        # optional filters and the keyset cursor append further params
        # so the ix_llm_usage_tenant_created index can still drive the
        # query plan from a fixed prefix.
        params: list[Any] = [tenant_id, filt.since, filt.until]
        clauses: list[str] = ["tenant_id = $1", "created_at >= $2", "created_at < $3"]
        if filt.model is not None:
            params.append(filt.model)
            clauses.append(f"model = ${len(params)}")
        if filt.task_id is not None:
            params.append(filt.task_id)
            clauses.append(f"task_id = ${len(params)}")
        if filt.status is not None:
            params.append(filt.status.value)
            clauses.append(f"status = ${len(params)}")
        if filt.before is not None:
            cursor_at, cursor_id = filt.before
            params.append(cursor_at)
            cursor_at_n = len(params)
            params.append(cursor_id)
            cursor_id_n = len(params)
            # Strict keyset (DESC): next page starts before the previous
            # row, breaking ties on record_id to keep ordering stable.
            clauses.append(
                f"(created_at < ${cursor_at_n} "
                f"OR (created_at = ${cursor_at_n} AND record_id < ${cursor_id_n}))"
            )
        params.append(filt.limit)
        limit_n = len(params)
        sql = (
            f"SELECT * FROM llm_usage_logs WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at DESC, record_id DESC LIMIT ${limit_n}"
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [_row_to_record(dict(r)) for r in rows]

    async def aggregate_for_task(
        self,
        tenant_id: str,
        task_id: str,
        group_by: UsageGroupBy,
    ) -> list[UsageAggregate]:
        check_tenant(tenant_id)
        key_expr = _AGG_KEY_EXPRS[group_by]
        sql = f"""
            SELECT
                {key_expr} AS key,
                COALESCE(SUM(total_tokens), 0)::bigint AS tokens,
                COALESCE(SUM(cost_usd_micros), 0)::bigint AS cost_micros,
                COUNT(*)::bigint AS calls
            FROM llm_usage_logs
            WHERE tenant_id = $1 AND task_id = $2
            GROUP BY {key_expr}
            ORDER BY tokens DESC, key ASC
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, tenant_id, task_id)
        return [
            UsageAggregate(
                key=str(r["key"]),
                tokens=int(r["tokens"]),
                cost_usd_micros=int(r["cost_micros"]),
                calls=int(r["calls"]),
            )
            for r in rows
        ]

    async def aggregate_grouped(
        self,
        tenant_id: str,
        since: datetime,
        until: datetime,
        group_by: UsageGroupBy,
    ) -> list[UsageAggregate]:
        check_tenant(tenant_id)
        key_expr = _AGG_KEY_EXPRS[group_by]
        sql = f"""
            SELECT
                {key_expr} AS key,
                COALESCE(SUM(total_tokens), 0)::bigint AS tokens,
                COALESCE(SUM(cost_usd_micros), 0)::bigint AS cost_micros,
                COUNT(*)::bigint AS calls
            FROM llm_usage_logs
            WHERE tenant_id = $1
              AND created_at >= $2
              AND created_at < $3
            GROUP BY {key_expr}
            ORDER BY tokens DESC, key ASC
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, tenant_id, since, until)
        return [
            UsageAggregate(
                key=str(r["key"]),
                tokens=int(r["tokens"]),
                cost_usd_micros=int(r["cost_micros"]),
                calls=int(r["calls"]),
            )
            for r in rows
        ]
