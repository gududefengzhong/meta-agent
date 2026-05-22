"""Phase γ-A worker tests: pause path + recover_in_flight.

The unit tests in :mod:`tests.worker.test_runner` exercise the
normal "PENDING → RUNNING → SUCCEEDED" flow; these focus on the
trust-surface additions: when a graph yields ``awaiting_approval``,
the worker writes ``TaskState.AWAITING_APPROVAL`` + acks the
message + audits the gate event; and on startup the loop
re-enqueues any task left in ``RUNNING`` by a crashed predecessor.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import UTC, datetime

from meta_agent.core.domain.outbox import OutboxEvent, OutboxStatus
from meta_agent.core.domain.task import PermissionMode, Task, TaskState, TaskType
from meta_agent.core.orchestration import (
    END,
    HUMAN_DECISION_KEY,
    HUMAN_GATE_AT_KEY,
    Graph,
    GraphDeps,
    GraphRegistry,
    NodeResult,
    TaskRunState,
    build_human_gate,
)
from meta_agent.core.ports.message import MessageEnvelope
from meta_agent.infra.queue.redis_consumer import DeliveredMessage
from meta_agent.worker.runner import WorkerLoop
from tests.worker._fakes import (
    FakeAuditRepo,
    FakeCheckpointRepo,
    FakeOutboxRepo,
    FakeStream,
    FakeTaskRepo,
)

TENANT = "t-1"
TRACE = "trace-1"
GATE_GRAPH_ID = "builtin.test_gate"


def _fixed_clock(start: datetime | None = None) -> Callable[[], datetime]:
    base = start or datetime(2026, 5, 23, tzinfo=UTC)
    counter = itertools.count()
    return lambda: base.replace(second=next(counter) % 60)


def _id_factory() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"id-{next(counter)}"


def _make_task(
    task_id: str = "task-1",
    *,
    state: TaskState = TaskState.PENDING,
    permission_mode: PermissionMode = PermissionMode.AUTO,
) -> Task:
    now = datetime(2026, 5, 23, tzinfo=UTC)
    return Task(
        task_id=task_id,
        tenant_id=TENANT,
        principal_id="user-1",
        trace_id=TRACE,
        idempotency_key=f"idem-{task_id}",
        task_type=TaskType.SYSTEM_ECHO,
        graph_id=GATE_GRAPH_ID,
        state=state,
        permission_mode=permission_mode,
        input_payload={"message": "hi"},
        created_at=now,
        updated_at=now,
    )


def _delivered(task_id: str = "task-1", entry_id: str = "1-0") -> DeliveredMessage:
    now = datetime(2026, 5, 23, tzinfo=UTC)
    return DeliveredMessage(
        envelope=MessageEnvelope(
            message_id="m-1",
            topic="task.commands",
            tenant_id=TENANT,
            trace_id=TRACE,
            idempotency_key=f"idem-{task_id}",
            principal_id="user-1",
            task_id=task_id,
            event_type="task.submitted",
            payload={},
            occurred_at=now,
            enqueued_at=now,
        ),
        entry_id=entry_id,
        delivery_count=1,
    )


def _registry_with_gate_graph() -> GraphRegistry:
    """A trivial graph that immediately hits a human_gate, for pause tests."""

    def factory(_deps: GraphDeps) -> Graph:
        g = Graph(GATE_GRAPH_ID)
        g.add_node("gate", build_human_gate(gate_id="only", next_node_when_approved="done"))

        async def done(_state: TaskRunState) -> NodeResult:
            return NodeResult(data_update={"output": {"resumed": True}}, next_node=END)

        g.add_node("done", done)
        g.set_entry("gate")
        g.add_edge("gate", "done")
        g.add_edge("done", END)
        g.compile()
        return g

    registry = GraphRegistry()
    registry.register(
        GATE_GRAPH_ID,
        factory,
        default_for=TaskType.SYSTEM_ECHO,
    )
    from tests.core.orchestration._fakes import fake_deps

    registry.materialize(fake_deps())
    return registry


def _build_loop(
    *,
    outbox: FakeOutboxRepo | None = None,
    registry: GraphRegistry | None = None,
) -> tuple[WorkerLoop, FakeTaskRepo, FakeCheckpointRepo, FakeAuditRepo, FakeStream, FakeOutboxRepo]:
    tasks = FakeTaskRepo()
    checkpoints = FakeCheckpointRepo()
    audits = FakeAuditRepo()
    stream = FakeStream()
    outbox = outbox if outbox is not None else FakeOutboxRepo()
    loop = WorkerLoop(
        stream=stream,
        tasks=tasks,
        checkpoints=checkpoints,
        audits=audits,
        registry=registry or _registry_with_gate_graph(),
        outbox=outbox,
        task_topic="task.commands",
        clock=_fixed_clock(),
        id_factory=_id_factory(),
    )
    return loop, tasks, checkpoints, audits, stream, outbox


# ---------------------------------------------------------------------------
# Pause path: graph yields awaiting_approval → worker transitions task,
# acks message, audits gate event, leaves no live resources.
# ---------------------------------------------------------------------------


async def test_worker_pauses_task_when_graph_yields_awaiting_approval() -> None:
    loop, tasks, checkpoints, audits, stream, _outbox = _build_loop()
    await tasks.upsert(_make_task())
    stream.push([_delivered()])

    handled = await loop.run_once()

    assert handled == 1
    paused = await tasks.get(TENANT, "task-1")
    assert paused is not None
    assert paused.state == TaskState.AWAITING_APPROVAL
    # Message acked: no live resources held while waiting.
    assert stream.acked == ["1-0"]
    # The pause event audits the gate id so trajectory can render it.
    actions = audits.actions()
    assert "task.awaiting_approval" in actions
    pause_audit = next(e for e in audits.rows if e.action == "task.awaiting_approval")
    assert pause_audit.payload.get("gate_id") == "only"
    # The latest checkpoint pinpoints the gate, so resume re-executes it.
    latest = await checkpoints.latest(TENANT, "task-1")
    assert latest is not None
    assert latest.state_snapshot["current_node"] == "gate"
    assert latest.state_snapshot["awaiting_approval"] is True


async def test_worker_resumes_past_gate_when_decision_injected_into_checkpoint() -> None:
    """A redelivered message after approve continues past the gate.

    Mirrors the production resume path: the API gateway writes a new
    checkpoint with the decision merged into state.data and
    ``awaiting_approval=False``; the worker rehydrates from that and
    the gate node advances to the next node.
    """

    loop, tasks, checkpoints, _audits, stream, _outbox = _build_loop()
    # Pre-populate as if a previous run had paused the task.
    paused_task = _make_task(state=TaskState.AWAITING_APPROVAL)
    await tasks.upsert(paused_task)
    paused_state = TaskRunState(
        task_id="task-1",
        tenant_id=TENANT,
        trace_id=TRACE,
        graph_id=GATE_GRAPH_ID,
        current_node="gate",
        sequence=1,
        data={"message": "hi", HUMAN_GATE_AT_KEY: "only"},
        awaiting_approval=True,
    )
    from meta_agent.core.domain.checkpoint import TaskCheckpoint

    await checkpoints.append(
        TaskCheckpoint(
            checkpoint_id="cp-1",
            task_id="task-1",
            tenant_id=TENANT,
            trace_id=TRACE,
            node_name="gate",
            sequence=1,
            state_snapshot=paused_state.model_dump(mode="json"),
            created_at=datetime(2026, 5, 23, tzinfo=UTC),
        )
    )
    # Simulate the gateway: write a NEW checkpoint with the decision
    # merged in and awaiting_approval=False, then flip the task state.
    resumed_state = paused_state.model_copy(
        update={
            "data": {**paused_state.data, HUMAN_DECISION_KEY: "approve"},
            "awaiting_approval": False,
            "sequence": 2,
        }
    )
    await checkpoints.append(
        TaskCheckpoint(
            checkpoint_id="cp-2",
            task_id="task-1",
            tenant_id=TENANT,
            trace_id=TRACE,
            node_name="gate",
            sequence=2,
            state_snapshot=resumed_state.model_dump(mode="json"),
            created_at=datetime(2026, 5, 23, tzinfo=UTC),
        )
    )
    await tasks.update_state(TENANT, "task-1", TaskState.RUNNING, datetime(2026, 5, 23, tzinfo=UTC))
    stream.push([_delivered()])

    handled = await loop.run_once()

    assert handled == 1
    refreshed = await tasks.get(TENANT, "task-1")
    assert refreshed is not None
    assert refreshed.state == TaskState.SUCCEEDED
    result = await tasks.get_result(TENANT, "task-1")
    assert result is not None
    assert result.output == {"resumed": True}


# ---------------------------------------------------------------------------
# recover_in_flight: re-enqueue RUNNING tasks at worker startup.
# ---------------------------------------------------------------------------


async def test_recover_in_flight_re_enqueues_running_tasks() -> None:
    loop, tasks, _checkpoints, audits, _stream, outbox = _build_loop()
    # One RUNNING (orphaned by a previous crash) + one terminal (must
    # NOT be touched) + one PENDING (must NOT be touched, it has its
    # own pending message in the queue already).
    await tasks.upsert(_make_task("running-1", state=TaskState.RUNNING))
    await tasks.upsert(_make_task("done-1", state=TaskState.SUCCEEDED))
    await tasks.upsert(_make_task("pending-1", state=TaskState.PENDING))

    count = await loop.recover_in_flight()

    assert count == 1
    enqueued_aggregate_ids = {e.aggregate_id for e in outbox.rows.values()}
    assert enqueued_aggregate_ids == {"running-1"}
    only_event = next(iter(outbox.rows.values()))
    assert only_event.status is OutboxStatus.PENDING
    assert only_event.idempotency_key.startswith("recover:running-1:")
    assert "worker.task_recovered" in audits.actions()


async def test_recover_in_flight_is_noop_without_outbox_wired() -> None:
    loop = WorkerLoop(
        stream=FakeStream(),
        tasks=FakeTaskRepo(),
        checkpoints=FakeCheckpointRepo(),
        audits=FakeAuditRepo(),
        registry=_registry_with_gate_graph(),
        # outbox intentionally None — unit-test mode
        clock=_fixed_clock(),
        id_factory=_id_factory(),
    )
    count = await loop.recover_in_flight()
    assert count == 0


async def test_recover_in_flight_continues_after_individual_failure() -> None:
    """One tenant's outbox write blowing up must not block others."""

    class _FlakyOutbox(FakeOutboxRepo):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def enqueue(self, event: OutboxEvent) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("simulated transient")
            await super().enqueue(event)

    flaky = _FlakyOutbox()
    loop, tasks, _ck, audits, _stream, _ob = _build_loop(outbox=flaky)
    await tasks.upsert(_make_task("running-1", state=TaskState.RUNNING))
    await tasks.upsert(_make_task("running-2", state=TaskState.RUNNING))

    count = await loop.recover_in_flight()

    # First task failed → audited; second task succeeded → enqueued.
    assert count == 1
    assert "worker.recover_failed" in audits.actions()
    assert "worker.task_recovered" in audits.actions()
    assert len(flaky.rows) == 1
