"""Unit tests for :class:`LLMUsageAggregatorBudgetEnforcer`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from meta_agent.core.domain.llm_usage import LLMUsageRecord
from meta_agent.core.ports.budget import BudgetBackendError, BudgetUsage
from meta_agent.core.ports.llm_usage import (
    LLMUsageFilter,
    LLMUsageRepository,
    UsageAggregate,
    UsageGroupBy,
)
from meta_agent.infra.budget.llm_usage_aggregator import (
    LLMUsageAggregatorBudgetEnforcer,
)


class _FakeUsageRepo(LLMUsageRepository):
    """Scripts the ``aggregate_since`` outcome; raises on demand."""

    def __init__(
        self,
        *,
        usage: BudgetUsage | None = None,
        raise_on_aggregate: BaseException | None = None,
    ) -> None:
        self._usage = usage or BudgetUsage(tokens_used=0, cost_usd_micros_used=0)
        self._raise = raise_on_aggregate
        self.calls: list[tuple[str, datetime]] = []

    async def record(self, record: LLMUsageRecord) -> None:
        raise AssertionError("record not exercised by aggregator")

    async def list_for_task(self, tenant_id: str, task_id: str) -> list[LLMUsageRecord]:
        raise AssertionError("list_for_task not exercised by aggregator")

    async def aggregate_since(self, tenant_id: str, since: datetime) -> BudgetUsage:
        self.calls.append((tenant_id, since))
        if self._raise is not None:
            raise self._raise
        return self._usage

    async def list_filtered(
        self,
        tenant_id: str,
        filt: LLMUsageFilter,
    ) -> list[LLMUsageRecord]:
        raise AssertionError("list_filtered not exercised by aggregator")

    async def aggregate_grouped(
        self,
        tenant_id: str,
        since: datetime,
        until: datetime,
        group_by: UsageGroupBy,
    ) -> list[UsageAggregate]:
        raise AssertionError("aggregate_grouped not exercised by aggregator")

    async def aggregate_for_task(
        self,
        tenant_id: str,
        task_id: str,
        group_by: UsageGroupBy,
    ) -> list[UsageAggregate]:
        raise AssertionError("aggregate_for_task not exercised by aggregator")


def _clock_at(dt: datetime) -> datetime:
    return dt


async def test_allows_when_under_limit() -> None:
    repo = _FakeUsageRepo(usage=BudgetUsage(tokens_used=900, cost_usd_micros_used=0))
    enforcer = LLMUsageAggregatorBudgetEnforcer(
        repo,
        limit_tokens=1000,
        clock=lambda: datetime(2025, 3, 15, 12, 0, tzinfo=UTC),
    )
    decision = await enforcer.check("t-1")
    assert decision.allowed is True
    assert decision.usage.tokens_used == 900
    assert decision.limit_tokens == 1000
    # window must start at the first instant of March (UTC).
    assert repo.calls == [("t-1", datetime(2025, 3, 1, tzinfo=UTC))]


async def test_denies_when_at_or_above_limit() -> None:
    repo = _FakeUsageRepo(usage=BudgetUsage(tokens_used=1000, cost_usd_micros_used=0))
    enforcer = LLMUsageAggregatorBudgetEnforcer(
        repo,
        limit_tokens=1000,
        clock=lambda: datetime(2025, 3, 15, tzinfo=UTC),
    )
    decision = await enforcer.check("t-1")
    assert decision.allowed is False


async def test_no_limit_means_allow_all() -> None:
    repo = _FakeUsageRepo(
        usage=BudgetUsage(tokens_used=999_999, cost_usd_micros_used=0),
    )
    enforcer = LLMUsageAggregatorBudgetEnforcer(
        repo,
        limit_tokens=None,
        clock=lambda: datetime(2025, 1, 1, tzinfo=UTC),
    )
    decision = await enforcer.check("t-1")
    assert decision.allowed is True
    assert decision.limit_tokens is None


async def test_repository_error_wraps_as_backend_error() -> None:
    repo = _FakeUsageRepo(raise_on_aggregate=RuntimeError("db down"))
    enforcer = LLMUsageAggregatorBudgetEnforcer(
        repo,
        limit_tokens=1000,
        clock=lambda: datetime(2025, 3, 15, tzinfo=UTC),
    )
    with pytest.raises(BudgetBackendError, match="t-1"):
        await enforcer.check("t-1")


async def test_empty_tenant_id_rejected() -> None:
    repo = _FakeUsageRepo()
    enforcer = LLMUsageAggregatorBudgetEnforcer(
        repo,
        limit_tokens=None,
        clock=lambda: datetime(2025, 3, 15, tzinfo=UTC),
    )
    with pytest.raises(ValueError):
        await enforcer.check("")


def test_negative_limit_rejected() -> None:
    repo = _FakeUsageRepo()
    with pytest.raises(ValueError):
        LLMUsageAggregatorBudgetEnforcer(repo, limit_tokens=-1)


async def test_month_boundary_picks_first_instant_of_month() -> None:
    repo = _FakeUsageRepo(usage=BudgetUsage(tokens_used=0, cost_usd_micros_used=0))
    # Last instant of January → window starts Jan 1.
    enforcer = LLMUsageAggregatorBudgetEnforcer(
        repo,
        limit_tokens=10,
        clock=lambda: datetime(2025, 1, 31, 23, 59, 59, tzinfo=UTC),
    )
    await enforcer.check("t-1")
    assert repo.calls[-1][1] == datetime(2025, 1, 1, tzinfo=UTC)
