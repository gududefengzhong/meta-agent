"""Budget-enforcer adapters.

【目标】基于 ``llm_usage_logs`` 当月聚合的 token 上限闸门；
NoOp 兜底；env-driven factory。

【当前】NoOp + LLMUsageAggregator + env factory。
"""

from meta_agent.infra.budget.config import (
    Backend,
    BudgetConfig,
    build_budget_enforcer_from_config,
)
from meta_agent.infra.budget.llm_usage_aggregator import LLMUsageAggregatorBudgetEnforcer
from meta_agent.infra.budget.noop import NoopBudgetEnforcer

__all__ = [
    "Backend",
    "BudgetConfig",
    "LLMUsageAggregatorBudgetEnforcer",
    "NoopBudgetEnforcer",
    "build_budget_enforcer_from_config",
]
