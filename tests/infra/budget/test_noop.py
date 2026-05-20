"""Unit tests for :class:`NoopBudgetEnforcer`."""

from __future__ import annotations

from meta_agent.infra.budget.noop import NoopBudgetEnforcer


async def test_noop_always_allows() -> None:
    enforcer = NoopBudgetEnforcer()
    decision = await enforcer.check("t-1")
    assert decision.allowed is True
    assert decision.usage.tokens_used == 0
    assert decision.usage.cost_usd_micros_used == 0
    assert decision.limit_tokens is None


async def test_noop_close_is_noop() -> None:
    enforcer = NoopBudgetEnforcer()
    await enforcer.close()
