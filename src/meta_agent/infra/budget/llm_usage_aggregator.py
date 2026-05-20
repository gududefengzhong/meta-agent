"""Calendar-month budget enforcer backed by :class:`LLMUsageRepository`.

【目标】对接 ``llm_usage_logs``：以租户当月（UTC）``total_tokens`` 累加为
准，超过 ``limit_tokens`` 即拒。``cost_usd_micros`` 也一并取回放进
:class:`BudgetUsage`，便于未来按金额上限收紧。

【当前】只对 tokens 设硬上限；``limit_tokens=None`` 等价 NoOp（永远 allow），
让运维侧可以先接装饰器再设阈值。

错误模型
========

仓储抛任何异常一律包装为 :class:`BudgetBackendError`，让 decorator 一处
决定 fail-open / fail-closed，不让具体仓储类型反向污染 LLM 热路径。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from meta_agent.core.ports.budget import (
    BudgetBackendError,
    BudgetDecision,
    BudgetEnforcer,
)
from meta_agent.core.ports.llm_usage import LLMUsageRepository


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _month_start(now: datetime) -> datetime:
    """First instant of the calendar month containing ``now`` (UTC)."""

    return datetime(now.year, now.month, 1, tzinfo=UTC)


class LLMUsageAggregatorBudgetEnforcer(BudgetEnforcer):
    """Aggregates per-tenant token usage from the LLM usage log.

    Parameters
    ----------
    usage_repo:
        Repository to query for the monthly sum.
    limit_tokens:
        Hard cap on ``total_tokens`` per tenant per calendar month.
        ``None`` disables enforcement (every call is allowed). Useful
        when wiring the decorator in environments without a configured
        budget while keeping the audit / observability path active.
    clock:
        Injected for tests. Returns timezone-aware UTC ``datetime``.
    """

    def __init__(
        self,
        usage_repo: LLMUsageRepository,
        *,
        limit_tokens: int | None,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        if limit_tokens is not None and limit_tokens < 0:
            raise ValueError("limit_tokens must be >= 0 or None")
        self._usage_repo = usage_repo
        self._limit_tokens = limit_tokens
        self._clock = clock

    async def check(self, tenant_id: str) -> BudgetDecision:
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        since = _month_start(self._clock())
        try:
            usage = await self._usage_repo.aggregate_since(tenant_id, since)
        except Exception as exc:
            raise BudgetBackendError(
                f"failed to aggregate llm_usage_logs for tenant={tenant_id!r}: {exc}"
            ) from exc
        allowed = self._limit_tokens is None or usage.tokens_used < self._limit_tokens
        return BudgetDecision(
            allowed=allowed,
            usage=usage,
            limit_tokens=self._limit_tokens,
        )


__all__ = ["LLMUsageAggregatorBudgetEnforcer"]
