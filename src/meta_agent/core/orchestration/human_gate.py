"""``human_gate`` graph node factory (Phase γ-A).

A human gate is a graph node whose only job is to ask "should I keep
going?" — usually injected just before a destructive / costly step
(push, merge, big tool call). When ``state.data`` does not yet
carry an approval payload, the gate returns
``NodeResult(awaiting_approval=True)`` and the graph runtime pauses
without advancing ``current_node``. The worker observes the pause,
transitions the task to ``TaskState.AWAITING_APPROVAL``, and acks the
queue message — no live resources are held while waiting.

On resume the API layer merges the operator's decision into the
checkpointed ``state.data`` (under :data:`HUMAN_DECISION_KEY` /
:data:`HUMAN_FEEDBACK_KEY`), re-enqueues the task, and the next
worker rehydrates the state with ``awaiting_approval=False``. The
gate node runs again, sees the decision, advances by returning a
``NodeResult`` whose ``next_node`` is the caller-supplied
``next_node_when_approved`` (or "abort" / END semantics for reject).

The gate intentionally **does not** consult ``Task.permission_mode``
itself — the *graph* decides whether to inject a gate based on the
task's mode. Once a gate is in the graph, it always gates. This
keeps the node single-purpose and the policy decision visible at
graph-build time.
"""

from __future__ import annotations

from meta_agent.core.orchestration.graph import GraphError, NodeFn, NodeResult
from meta_agent.core.orchestration.state import END, TaskRunState

# ---------------------------------------------------------------------------
# State.data keys the API layer writes into the checkpointed state when an
# operator approves / rejects a paused task. Centralised here so the gate
# node and the API repo / handler agree on the wire shape.
# ---------------------------------------------------------------------------

HUMAN_DECISION_KEY = "_human_decision"
"""``state.data`` key carrying the operator's verdict.

Values: ``"approve"`` (continue) / ``"reject"`` (route to END as a
caller-rejected outcome). Any other value is treated as "no decision
yet" and the gate re-pauses.
"""

HUMAN_FEEDBACK_KEY = "_human_feedback"
"""``state.data`` key carrying optional free-text feedback from the operator.

Forwarded into the next node via the same ``state.data`` slot so the
downstream node can incorporate it (e.g. as a replan hint). Cleared
on resume only if the gate explicitly chooses to.
"""

HUMAN_GATE_AT_KEY = "_human_gate_at"
"""``state.data`` key recording WHICH gate the task is paused at.

Lets graphs with multiple gates disambiguate when resuming; the
operator approves a specific gate, not a global "continue".
"""


def build_human_gate(
    *,
    gate_id: str,
    next_node_when_approved: str,
    next_node_when_rejected: str | None = None,
) -> NodeFn:
    """Return a graph-node function that pauses until an operator approves.

    ``gate_id`` is a short stable identifier (e.g. ``"before_push"``)
    written into ``state.data`` so downstream queries (trajectory,
    audit) can tell which gate the task is at. Multiple gates in the
    same graph MUST use distinct ids.

    ``next_node_when_approved`` is the destination after an
    ``"approve"`` decision.

    ``next_node_when_rejected`` is the destination after a ``"reject"``
    decision. The default :data:`None` keeps the legacy γ-A behaviour
    — reject routes to :data:`END` with a ``_rejected_by_human=True``
    marker on the state. Setting it to a node name turns the gate
    into a γ-C "approve with edits" surface: the operator's feedback
    travels through :data:`HUMAN_FEEDBACK_KEY` and the graph re-enters
    a planning node to incorporate it. Graphs wiring this MUST also
    bump ``_replan_attempts`` (or an equivalent guard) so an
    indefinite reject → replan → reject loop is bounded.
    """

    if not gate_id:
        raise GraphError("human_gate: gate_id must be a non-empty string")
    if not next_node_when_approved:
        raise GraphError("human_gate: next_node_when_approved must be a non-empty string")
    if next_node_when_rejected is not None and not next_node_when_rejected:
        raise GraphError(
            "human_gate: next_node_when_rejected must be a non-empty string when supplied"
        )

    async def gate(state: TaskRunState) -> NodeResult:
        decision = state.data.get(HUMAN_DECISION_KEY)
        if decision == "approve":
            # Clear the decision so a future gate (if any) starts
            # fresh; preserve the feedback so the next node can read
            # it as a replan hint.
            return NodeResult(
                data_update={
                    HUMAN_DECISION_KEY: None,
                    HUMAN_GATE_AT_KEY: None,
                },
                next_node=next_node_when_approved,
            )
        if decision == "reject":
            if next_node_when_rejected is None:
                return NodeResult(
                    data_update={
                        HUMAN_DECISION_KEY: None,
                        HUMAN_GATE_AT_KEY: None,
                        "_rejected_by_human": True,
                    },
                    next_node=END,
                )
            # γ-C "approve with edits" mode: route back to the
            # caller-supplied replan node and preserve the feedback
            # so the next plan iteration can render it.
            return NodeResult(
                data_update={
                    HUMAN_DECISION_KEY: None,
                    HUMAN_GATE_AT_KEY: None,
                    "_rejected_with_feedback": True,
                },
                next_node=next_node_when_rejected,
            )
        # No decision yet — pause. ``HUMAN_GATE_AT_KEY`` records which
        # gate we are at so the API can validate "approve only the gate
        # the task actually paused at".
        return NodeResult(
            data_update={HUMAN_GATE_AT_KEY: gate_id},
            awaiting_approval=True,
        )

    return gate
