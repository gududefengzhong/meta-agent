"""Unit tests for :mod:`meta_agent.infra.budget.config`."""

from __future__ import annotations

from datetime import datetime

import pytest

from meta_agent.core.domain.llm_usage import LLMUsageRecord
from meta_agent.core.ports.budget import BudgetUsage
from meta_agent.core.ports.llm_usage import (
    LLMUsageFilter,
    LLMUsageRepository,
    UsageAggregate,
    UsageGroupBy,
)
from meta_agent.infra.budget.config import (
    BudgetConfig,
    build_budget_enforcer_from_config,
)
from meta_agent.infra.budget.llm_usage_aggregator import (
    LLMUsageAggregatorBudgetEnforcer,
)
from meta_agent.infra.budget.noop import NoopBudgetEnforcer


class _StubUsageRepo(LLMUsageRepository):
    async def record(self, record: LLMUsageRecord) -> None:
        raise AssertionError

    async def list_for_task(self, tenant_id: str, task_id: str) -> list[LLMUsageRecord]:
        raise AssertionError

    async def aggregate_since(self, tenant_id: str, since: datetime) -> BudgetUsage:
        return BudgetUsage(tokens_used=0, cost_usd_micros_used=0)

    async def list_filtered(
        self,
        tenant_id: str,
        filt: LLMUsageFilter,
    ) -> list[LLMUsageRecord]:
        raise AssertionError

    async def aggregate_grouped(
        self,
        tenant_id: str,
        since: datetime,
        until: datetime,
        group_by: UsageGroupBy,
    ) -> list[UsageAggregate]:
        raise AssertionError

    async def aggregate_for_task(
        self,
        tenant_id: str,
        task_id: str,
        group_by: UsageGroupBy,
    ) -> list[UsageAggregate]:
        raise AssertionError


def test_defaults_are_noop_with_no_cap() -> None:
    cfg = BudgetConfig.from_env({})
    assert cfg.backend == "noop"
    assert cfg.max_tokens_per_month == 0
    assert cfg.cache_ttl_s == 10.0
    assert cfg.fail_open is True


def test_llm_usage_backend_with_cap_and_overrides() -> None:
    cfg = BudgetConfig.from_env(
        {
            "META_AGENT_BUDGET_BACKEND": "llm_usage",
            "META_AGENT_BUDGET_MAX_TOKENS": "5000000",
            "META_AGENT_BUDGET_CACHE_TTL_S": "0",
            "META_AGENT_BUDGET_FAIL_OPEN": "false",
        }
    )
    assert cfg.backend == "llm_usage"
    assert cfg.max_tokens_per_month == 5_000_000
    assert cfg.cache_ttl_s == 0.0
    assert cfg.fail_open is False


@pytest.mark.parametrize(
    "env",
    [
        {"META_AGENT_BUDGET_BACKEND": "redis"},
        {"META_AGENT_BUDGET_MAX_TOKENS": "abc"},
        {"META_AGENT_BUDGET_MAX_TOKENS": "-1"},
        {"META_AGENT_BUDGET_CACHE_TTL_S": "xyz"},
        {"META_AGENT_BUDGET_CACHE_TTL_S": "-1"},
        {"META_AGENT_BUDGET_FAIL_OPEN": "maybe"},
    ],
)
def test_invalid_env_raises(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        BudgetConfig.from_env(env)


def test_factory_noop_ignores_usage_repo() -> None:
    cfg = BudgetConfig(backend="noop", max_tokens_per_month=0, cache_ttl_s=10.0, fail_open=True)
    enforcer = build_budget_enforcer_from_config(cfg, usage_repo=None)
    assert isinstance(enforcer, NoopBudgetEnforcer)


def test_factory_llm_usage_requires_repo() -> None:
    cfg = BudgetConfig(
        backend="llm_usage",
        max_tokens_per_month=1000,
        cache_ttl_s=10.0,
        fail_open=True,
    )
    with pytest.raises(ValueError):
        build_budget_enforcer_from_config(cfg, usage_repo=None)


def test_factory_llm_usage_builds_aggregator() -> None:
    cfg = BudgetConfig(
        backend="llm_usage",
        max_tokens_per_month=1000,
        cache_ttl_s=10.0,
        fail_open=True,
    )
    enforcer = build_budget_enforcer_from_config(cfg, usage_repo=_StubUsageRepo())
    assert isinstance(enforcer, LLMUsageAggregatorBudgetEnforcer)


async def test_factory_zero_cap_means_no_limit() -> None:
    cfg = BudgetConfig(
        backend="llm_usage",
        max_tokens_per_month=0,
        cache_ttl_s=10.0,
        fail_open=True,
    )
    enforcer = build_budget_enforcer_from_config(cfg, usage_repo=_StubUsageRepo())
    assert isinstance(enforcer, LLMUsageAggregatorBudgetEnforcer)
    decision = await enforcer.check("t-1")
    assert decision.allowed is True
    assert decision.limit_tokens is None
