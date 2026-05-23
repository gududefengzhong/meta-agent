"""Per-task budget gate helper (Phase γ-C).

Graphs that want to enforce :class:`BudgetPolicy` at strategic points
call :func:`check_budget_policy` before doing expensive work (typically
at the top of a plan / edit node). The helper consults
``llm_usage_logs`` for the running task-level cost and returns one of
three outcomes:

* ``None`` — proceed: no policy, no threshold, or under budget.
* A :class:`NodeResult` with ``awaiting_approval=True`` — pause for
  human approval (``BudgetPolicy.GATE_ON_THRESHOLD``).
* A :class:`NodeResult` routed to :data:`END` with an ``error`` —
  terminate the task (``BudgetPolicy.ABORT_ON_THRESHOLD``).

The threshold is taken from ``state.data['_budget_threshold_micros']``
(seeded by the worker from ``Task.budget_threshold_micros``). When
the threshold is missing the gate is a no-op regardless of policy —
tenants without per-task ceilings still get the tenant-level monthly
limit applied by :class:`BudgetEnforcingLLMClient`.
"""

from __future__ import annotations

from meta_agent.core.orchestration.graph import NodeResult
from meta_agent.core.orchestration.human_gate import (
    HUMAN_DECISION_KEY,
    HUMAN_GATE_AT_KEY,
)
from meta_agent.core.orchestration.state import END, TaskRunState
from meta_agent.core.ports.llm_usage import LLMUsageRepository, UsageGroupBy

BUDGET_GATE_ID = "budget"
"""Gate id recorded in ``state.data`` when the budget gate pauses.

Mirrors the human-gate convention so trajectory / audit consumers
can disambiguate "paused for human approval" from "paused at budget
threshold" by inspecting ``HUMAN_GATE_AT_KEY``.
"""


async def check_budget_policy(
    state: TaskRunState,
    *,
    llm_usage: LLMUsageRepository | None,
    this_node: str,
) -> NodeResult | None:
    """Consult the per-task cost and decide whether to gate / abort.

    ``this_node`` is the graph node calling the helper; on resume
    after approval the helper returns a self-loop so the same node
    re-executes with the gate now considered passed.

    Returns ``None`` to indicate "no gate fired, proceed normally".
    Returns a :class:`NodeResult` carrying the gate / abort / resume
    signal otherwise; the caller must return it verbatim.
    """

    if llm_usage is None:
        return None
    policy = state.data.get("_budget_policy")
    if policy not in ("gate_on_threshold", "abort_on_threshold"):
        return None

    # Resume-after-approve: the gate paused this task on a prior
    # invocation. The operator decided; we now own the consumption of
    # ``HUMAN_DECISION_KEY`` for the budget-gate variant so a
    # downstream human_gate node does not see a stale decision.
    decision = state.data.get(HUMAN_DECISION_KEY)
    gate_at = state.data.get(HUMAN_GATE_AT_KEY)
    if gate_at == BUDGET_GATE_ID and decision == "approve":
        return NodeResult(
            data_update={
                "_budget_gate_passed": True,
                HUMAN_DECISION_KEY: None,
                HUMAN_GATE_AT_KEY: None,
            },
            next_node=this_node,
        )
    if gate_at == BUDGET_GATE_ID and decision == "reject":
        return NodeResult(
            data_update={
                HUMAN_DECISION_KEY: None,
                HUMAN_GATE_AT_KEY: None,
                "_budget_exceeded": True,
            },
            next_node=END,
            error="budget gate rejected by operator",
        )

    threshold_raw = state.data.get("_budget_threshold_micros")
    if not isinstance(threshold_raw, int) or threshold_raw <= 0:
        return None
    # Already approved this run? Don't re-fire even if the spend is
    # still over the original threshold.
    if bool(state.data.get("_budget_gate_passed")):
        return None
    buckets = await llm_usage.aggregate_for_task(
        state.tenant_id, state.task_id, UsageGroupBy.STEP_KIND
    )
    spent = sum(bucket.cost_usd_micros for bucket in buckets)
    if spent < threshold_raw:
        return None
    if policy == "abort_on_threshold":
        return NodeResult(
            data_update={
                "_budget_exceeded": True,
                "_budget_spent_micros": spent,
                "_budget_threshold_micros_at_abort": threshold_raw,
            },
            next_node=END,
            error=(f"budget exceeded: spent {spent} >= threshold {threshold_raw} micro-USD"),
        )
    # ``gate_on_threshold`` pauses for human approval. Re-uses the
    # AWAITING_APPROVAL signal so the worker + approval gateway code
    # paths work without any new state. The gate id distinguishes
    # "budget" from a human-only gate when the trajectory is rendered.
    return NodeResult(
        data_update={
            HUMAN_GATE_AT_KEY: BUDGET_GATE_ID,
            "_budget_spent_micros": spent,
        },
        awaiting_approval=True,
    )
