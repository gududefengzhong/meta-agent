"""Unit tests for :func:`check_budget_policy`."""

from __future__ import annotations

from datetime import datetime

from meta_agent.core.domain.llm_usage import LLMUsageRecord
from meta_agent.core.orchestration import END, TaskRunState
from meta_agent.core.orchestration.budget_gate import BUDGET_GATE_ID, check_budget_policy
from meta_agent.core.orchestration.human_gate import (
    HUMAN_DECISION_KEY,
    HUMAN_GATE_AT_KEY,
)
from meta_agent.core.ports.budget import BudgetUsage
from meta_agent.core.ports.llm_usage import (
    LLMUsageFilter,
    LLMUsageRepository,
    UsageAggregate,
    UsageGroupBy,
)


class _FixedUsage(LLMUsageRepository):
    """In-memory usage repo that returns a single fixed bucket sum."""

    def __init__(self, *, cost_micros: int) -> None:
        self._cost = cost_micros

    async def record(self, record: LLMUsageRecord) -> None:  # pragma: no cover
        raise AssertionError("not used")

    async def list_for_task(
        self, tenant_id: str, task_id: str
    ) -> list[LLMUsageRecord]:  # pragma: no cover
        raise AssertionError

    async def aggregate_since(
        self, tenant_id: str, since: datetime
    ) -> BudgetUsage:  # pragma: no cover
        raise AssertionError

    async def list_filtered(
        self,
        tenant_id: str,
        filt: LLMUsageFilter,
    ) -> list[LLMUsageRecord]:  # pragma: no cover
        raise AssertionError

    async def aggregate_grouped(
        self,
        tenant_id: str,
        since: datetime,
        until: datetime,
        group_by: UsageGroupBy,
    ) -> list[UsageAggregate]:  # pragma: no cover
        raise AssertionError

    async def aggregate_for_task(
        self,
        tenant_id: str,
        task_id: str,
        group_by: UsageGroupBy,
    ) -> list[UsageAggregate]:
        return [
            UsageAggregate(key="plan", tokens=100, cost_usd_micros=self._cost, calls=1),
        ]


def _state(**data: object) -> TaskRunState:
    return TaskRunState(
        task_id="task-1",
        tenant_id="tenant-1",
        trace_id="trace-1",
        graph_id="builtin.test",
        data=data,
    )


async def test_returns_none_when_no_usage_repo_wired() -> None:
    result = await check_budget_policy(_state(), llm_usage=None, this_node="plan")
    assert result is None


async def test_returns_none_when_policy_is_none() -> None:
    usage = _FixedUsage(cost_micros=10_000_000)
    state = _state(_budget_policy="none", _budget_threshold_micros=1)
    assert await check_budget_policy(state, llm_usage=usage, this_node="plan") is None


async def test_returns_none_when_threshold_missing() -> None:
    usage = _FixedUsage(cost_micros=10_000_000)
    state = _state(_budget_policy="gate_on_threshold")
    assert await check_budget_policy(state, llm_usage=usage, this_node="plan") is None


async def test_returns_none_when_under_budget() -> None:
    usage = _FixedUsage(cost_micros=1_000)
    state = _state(_budget_policy="gate_on_threshold", _budget_threshold_micros=10_000)
    assert await check_budget_policy(state, llm_usage=usage, this_node="plan") is None


async def test_pauses_when_gate_on_threshold_exceeded() -> None:
    usage = _FixedUsage(cost_micros=20_000)
    state = _state(_budget_policy="gate_on_threshold", _budget_threshold_micros=10_000)
    result = await check_budget_policy(state, llm_usage=usage, this_node="plan")
    assert result is not None
    assert result.awaiting_approval is True
    assert result.data_update[HUMAN_GATE_AT_KEY] == BUDGET_GATE_ID
    assert result.data_update["_budget_spent_micros"] == 20_000


async def test_aborts_when_abort_on_threshold_exceeded() -> None:
    usage = _FixedUsage(cost_micros=50_000)
    state = _state(_budget_policy="abort_on_threshold", _budget_threshold_micros=10_000)
    result = await check_budget_policy(state, llm_usage=usage, this_node="plan")
    assert result is not None
    assert result.next_node == END
    assert result.error is not None
    assert "budget exceeded" in result.error
    assert result.data_update["_budget_exceeded"] is True


async def test_resume_after_approve_marks_passed_and_self_loops() -> None:
    usage = _FixedUsage(cost_micros=20_000)
    state = _state(
        _budget_policy="gate_on_threshold",
        _budget_threshold_micros=10_000,
        _human_decision="approve",
        _human_gate_at=BUDGET_GATE_ID,
    )
    result = await check_budget_policy(state, llm_usage=usage, this_node="plan")
    assert result is not None
    assert result.next_node == "plan"
    assert result.data_update["_budget_gate_passed"] is True
    # The decision is consumed so a downstream human_gate does not
    # see stale data.
    assert result.data_update[HUMAN_DECISION_KEY] is None
    assert result.data_update[HUMAN_GATE_AT_KEY] is None


async def test_resume_after_reject_aborts_with_error() -> None:
    usage = _FixedUsage(cost_micros=20_000)
    state = _state(
        _budget_policy="gate_on_threshold",
        _budget_threshold_micros=10_000,
        _human_decision="reject",
        _human_gate_at=BUDGET_GATE_ID,
    )
    result = await check_budget_policy(state, llm_usage=usage, this_node="plan")
    assert result is not None
    assert result.next_node == END
    assert result.error is not None
    assert "rejected by operator" in result.error


async def test_already_passed_short_circuits() -> None:
    usage = _FixedUsage(cost_micros=20_000)
    state = _state(
        _budget_policy="gate_on_threshold",
        _budget_threshold_micros=10_000,
        _budget_gate_passed=True,
    )
    # Even though spend > threshold, the prior approval permits this run.
    result = await check_budget_policy(state, llm_usage=usage, this_node="plan")
    assert result is None


async def test_human_gate_decision_for_different_gate_is_ignored() -> None:
    """If the operator approved a *human_gate* (not the budget gate),
    the budget gate must NOT consume the decision."""

    usage = _FixedUsage(cost_micros=20_000)
    state = _state(
        _budget_policy="gate_on_threshold",
        _budget_threshold_micros=10_000,
        _human_decision="approve",
        _human_gate_at="before_push",  # different gate
    )
    result = await check_budget_policy(state, llm_usage=usage, this_node="plan")
    # Falls through to the normal spend check; spend > threshold so it pauses.
    assert result is not None
    assert result.awaiting_approval is True
