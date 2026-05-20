"""Always-allow budget enforcer.

Default in dev / test wiring and the safe fallback when no
backend has been explicitly configured. Reports zero usage with no
configured cap so callers that surface the numbers do not need a
special case.
"""

from __future__ import annotations

from meta_agent.core.ports.budget import BudgetDecision, BudgetEnforcer, BudgetUsage

_ZERO_USAGE = BudgetUsage(tokens_used=0, cost_usd_micros_used=0)


class NoopBudgetEnforcer(BudgetEnforcer):
    """Permits every call; never raises."""

    async def check(self, tenant_id: str) -> BudgetDecision:
        return BudgetDecision(allowed=True, usage=_ZERO_USAGE, limit_tokens=None)


__all__ = ["NoopBudgetEnforcer"]
