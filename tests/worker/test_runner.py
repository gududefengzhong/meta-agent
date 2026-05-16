"""Unit tests for :class:`WorkerLoop`.

The worker is driven against in-memory fakes (see ``_fakes.py``) so the
tests verify the orchestration / checkpoint / audit / ack control flow
without booting Postgres or Redis. Integration coverage of the real
adapters lives under ``tests/integration``.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from meta_agent.core.domain.checkpoint import TaskCheckpoint
from meta_agent.core.domain.task import Task, TaskState, TaskType
from meta_agent.core.orchestration import (
    Graph,
    GraphDeps,
    GraphRegistry,
    NodeResult,
    TaskRunState,
)
from meta_agent.core.orchestration.graphs.echo import ECHO_GRAPH_ID, build_echo_graph
from meta_agent.core.orchestration.result import TaskErrorCode
from meta_agent.core.orchestration.state import END
from meta_agent.core.ports.message import MessageEnvelope
from meta_agent.core.ports.repository import IllegalTaskTransitionError
from meta_agent.infra.queue.redis_consumer import DeliveredMessage
from meta_agent.worker.runner import WorkerConfig, WorkerLoop
from tests.core.orchestration._fakes import fake_deps

from ._fakes import FakeAuditRepo, FakeCheckpointRepo, FakeStream, FakeTaskRepo

pytestmark = pytest.mark.asyncio

TENANT = "tenant-1"
TRACE = "trace-1"


def _fixed_clock() -> Callable[[], datetime]:
    return lambda: datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)


def _id_factory() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"id-{next(counter)}"


def _make_task(
    task_id: str = "task-1",
    *,
    task_type: TaskType = TaskType.SYSTEM_ECHO,
    graph_id: str | None = None,
    state: TaskState = TaskState.PENDING,
    payload: dict[str, object] | None = None,
) -> Task:
    now = datetime(2026, 5, 15, tzinfo=UTC)
    return Task(
        task_id=task_id,
        tenant_id=TENANT,
        principal_id="user-1",
        trace_id=TRACE,
        idempotency_key="idem-1",
        task_type=task_type,
        graph_id=graph_id,
        state=state,
        input_payload=payload or {"message": "hello"},
        created_at=now,
        updated_at=now,
    )


def _make_envelope(task_id: str | None = "task-1") -> MessageEnvelope:
    now = datetime(2026, 5, 15, tzinfo=UTC)
    return MessageEnvelope(
        message_id="m-1",
        topic="task.events",
        tenant_id=TENANT,
        trace_id=TRACE,
        idempotency_key="idem-1",
        principal_id="user-1",
        task_id=task_id,
        event_type="task.submitted",
        payload={},
        occurred_at=now,
        enqueued_at=now,
    )


def _delivered(
    *, task_id: str | None = "task-1", entry_id: str = "1-0", count: int = 1
) -> DeliveredMessage:
    return DeliveredMessage(
        envelope=_make_envelope(task_id),
        entry_id=entry_id,
        delivery_count=count,
    )


def _registry_with_echo() -> GraphRegistry:
    registry = GraphRegistry()
    registry.register(
        ECHO_GRAPH_ID,
        lambda _deps: build_echo_graph(),
        default_for=TaskType.SYSTEM_ECHO,
    )
    registry.materialize(fake_deps())
    return registry


def _build_loop(
    *,
    tasks: FakeTaskRepo | None = None,
    checkpoints: FakeCheckpointRepo | None = None,
    audits: FakeAuditRepo | None = None,
    stream: FakeStream | None = None,
    registry: GraphRegistry | None = None,
    config: WorkerConfig | None = None,
) -> tuple[WorkerLoop, FakeTaskRepo, FakeCheckpointRepo, FakeAuditRepo, FakeStream]:
    tasks = tasks or FakeTaskRepo()
    checkpoints = checkpoints or FakeCheckpointRepo()
    audits = audits or FakeAuditRepo()
    stream = stream or FakeStream()
    loop = WorkerLoop(
        stream=stream,
        tasks=tasks,
        checkpoints=checkpoints,
        audits=audits,
        registry=registry or _registry_with_echo(),
        clock=_fixed_clock(),
        id_factory=_id_factory(),
        config=config,
    )
    return loop, tasks, checkpoints, audits, stream


async def test_run_once_executes_echo_graph_end_to_end() -> None:
    loop, tasks, checkpoints, audits, stream = _build_loop()
    await tasks.upsert(_make_task())
    stream.push([_delivered()])

    handled = await loop.run_once()

    assert handled == 1
    task = await tasks.get(TENANT, "task-1")
    assert task is not None
    assert task.state == TaskState.SUCCEEDED
    assert stream.acked == ["1-0"]
    sequences = [c.sequence for c in checkpoints.rows]
    assert sequences == [1, 2, 3]
    last = checkpoints.rows[-1]
    transcript = last.state_snapshot["data"]["transcript"]  # type: ignore[index]
    assert transcript == [
        "plan: received 'hello'",
        "execute: echo 'hello'",
        "review: ok",
    ]
    assert "task.succeeded" in audits.actions()
    assert audits.actions().count("task.node_completed") == 3

    result = await tasks.get_result(TENANT, "task-1")
    assert result is not None
    assert result.status == "succeeded"
    assert result.error is None
    assert result.output == {"echo": "hello"}
    assert result.graph_id == ECHO_GRAPH_ID
    assert result.node_sequence == 3
    assert result.finished_at >= result.started_at


async def test_run_once_resumes_from_checkpoint() -> None:
    loop, tasks, checkpoints, _audits, stream = _build_loop()
    await tasks.upsert(_make_task(state=TaskState.RUNNING))
    resume_state = TaskRunState(
        task_id="task-1",
        tenant_id=TENANT,
        trace_id=TRACE,
        graph_id=ECHO_GRAPH_ID,
        current_node="execute",
        sequence=1,
        data={"message": "hello", "transcript": ["plan: received 'hello'"]},
    )
    await checkpoints.append(
        TaskCheckpoint(
            checkpoint_id="cp-0",
            task_id="task-1",
            tenant_id=TENANT,
            trace_id=TRACE,
            node_name="execute",
            sequence=1,
            state_snapshot=resume_state.model_dump(mode="json"),
            created_at=datetime(2026, 5, 15, tzinfo=UTC),
        )
    )
    stream.push([_delivered()])

    await loop.run_once()

    new_checkpoints = [c for c in checkpoints.rows if c.checkpoint_id != "cp-0"]
    assert [c.sequence for c in new_checkpoints] == [2, 3]
    task = await tasks.get(TENANT, "task-1")
    assert task is not None and task.state == TaskState.SUCCEEDED
    final = new_checkpoints[-1].state_snapshot["data"]["transcript"]  # type: ignore[index]
    assert final == [
        "plan: received 'hello'",
        "execute: echo 'hello'",
        "review: ok",
    ]


async def test_run_once_abandons_after_max_attempts_exceeded() -> None:
    config = WorkerConfig(max_attempts=3)
    loop, tasks, checkpoints, audits, stream = _build_loop(config=config)
    await tasks.upsert(_make_task(state=TaskState.RUNNING))
    stream.push([_delivered(count=4)])

    await loop.run_once()

    assert checkpoints.rows == []
    task = await tasks.get(TENANT, "task-1")
    assert task is not None and task.state == TaskState.FAILED
    assert "task.abandoned" in audits.actions()
    assert stream.acked == ["1-0"]

    result = await tasks.get_result(TENANT, "task-1")
    assert result is not None
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == TaskErrorCode.ABANDONED
    assert result.error.details == {"delivery_count": 4}
    assert result.output is None
    assert result.node_sequence == 0


async def test_run_once_leaves_message_in_pel_on_node_failure() -> None:
    async def explode(state: TaskRunState) -> NodeResult:
        raise RuntimeError("boom")

    def boom_factory(_deps: GraphDeps) -> Graph:
        boom = Graph("builtin.boom")
        boom.add_node("explode", explode)
        boom.set_entry("explode")
        boom.add_edge("explode", END)
        return boom

    registry = GraphRegistry()
    registry.register("builtin.boom", boom_factory, default_for=TaskType.SYSTEM_ECHO)
    registry.materialize(fake_deps())

    loop, tasks, _checkpoints, audits, stream = _build_loop(registry=registry)
    await tasks.upsert(_make_task())
    stream.push([_delivered()])

    handled = await loop.run_once()

    assert handled == 1
    assert stream.acked == []  # left in PEL for redelivery
    task = await tasks.get(TENANT, "task-1")
    assert task is not None
    assert task.state == TaskState.RUNNING  # transitioned but not finalised
    assert "task.succeeded" not in audits.actions()
    assert "task.failed" not in audits.actions()
    assert "task.abandoned" not in audits.actions()


async def test_run_once_acks_when_task_is_missing() -> None:
    loop, _tasks, checkpoints, audits, stream = _build_loop()
    stream.push([_delivered()])

    await loop.run_once()

    assert stream.acked == ["1-0"]
    assert checkpoints.rows == []
    assert "worker.task_missing" in audits.actions()


async def test_run_once_acks_when_envelope_lacks_task_id() -> None:
    loop, _tasks, _checkpoints, audits, stream = _build_loop()
    stream.push([_delivered(task_id=None)])

    await loop.run_once()

    assert stream.acked == ["1-0"]
    assert "worker.envelope_invalid" in audits.actions()


async def test_run_once_uses_explicit_graph_id_override() -> None:
    registry = GraphRegistry()
    # Registered, but not default for any task_type — only graph_id override resolves.
    registry.register(ECHO_GRAPH_ID, lambda _deps: build_echo_graph())
    registry.materialize(fake_deps())

    loop, tasks, _checkpoints, _audits, stream = _build_loop(registry=registry)
    # task_type has no default graph; only graph_id override resolves
    await tasks.upsert(_make_task(graph_id=ECHO_GRAPH_ID))
    stream.push([_delivered()])

    await loop.run_once()

    task = await tasks.get(TENANT, "task-1")
    assert task is not None and task.state == TaskState.SUCCEEDED
    assert stream.acked == ["1-0"]


async def test_run_once_returns_zero_on_empty_batch() -> None:
    loop, *_ = _build_loop()
    assert await loop.run_once() == 0


async def test_build_result_projects_graph_error_to_failed_status() -> None:
    """Static projection: ``state.error`` -> ``failed`` ``TaskResult``.

    The current graph runtime doesn't expose a typed error channel
    through :class:`NodeResult`, so end-to-end failure is exercised via
    the abandon path. This test pins the projection contract so a later
    typed-error signal lands as ``code=graph_error`` automatically.
    """

    task = _make_task()
    graph = build_echo_graph()
    state = TaskRunState(
        task_id=task.task_id,
        tenant_id=task.tenant_id,
        trace_id=task.trace_id,
        graph_id=graph.graph_id,
        sequence=2,
        data={"transcript": ["x"]},
        finished=True,
        error="boom",
    )
    started = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
    finished = datetime(2026, 5, 15, 12, 0, 1, tzinfo=UTC)

    result, terminal = WorkerLoop._build_result(task, graph, state, started, finished)

    assert terminal == TaskState.FAILED
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == TaskErrorCode.GRAPH_ERROR
    assert result.error.message == "boom"
    assert result.output is None
    assert result.node_sequence == 2


async def test_redelivered_message_after_terminal_state_skips_complete() -> None:
    """A redelivered message for an already-terminal task must drain
    without re-running the graph or touching the result row."""

    loop, tasks, checkpoints, audits, stream = _build_loop()
    await tasks.upsert(_make_task(state=TaskState.SUCCEEDED))
    stream.push([_delivered()])

    await loop.run_once()

    assert stream.acked == ["1-0"]
    task = await tasks.get(TENANT, "task-1")
    assert task is not None and task.state == TaskState.SUCCEEDED
    assert await tasks.get_result(TENANT, "task-1") is None
    assert "worker.task_already_terminal" in audits.actions()
    # Graph never executed: no checkpoints and no terminal audit.
    assert checkpoints.rows == []
    assert "task.succeeded" not in audits.actions()
    assert "task.node_completed" not in audits.actions()


async def test_abandon_is_noop_when_task_already_terminal() -> None:
    """Redelivery of an already-terminal task on the abandon path must
    leave the existing result untouched."""

    config = WorkerConfig(max_attempts=3)
    loop, tasks, _checkpoints, _audits, stream = _build_loop(config=config)
    await tasks.upsert(_make_task(state=TaskState.SUCCEEDED))
    stream.push([_delivered(count=4)])

    await loop.run_once()

    task = await tasks.get(TENANT, "task-1")
    assert task is not None and task.state == TaskState.SUCCEEDED
    assert await tasks.get_result(TENANT, "task-1") is None
    assert stream.acked == ["1-0"]


async def test_fake_repo_complete_rejects_non_terminal_state() -> None:
    """Defensive: the fake mirrors the PG guard against non-terminal
    ``terminal_state`` values so worker bugs surface in unit tests."""

    from meta_agent.core.orchestration.result import TaskResult

    repo = FakeTaskRepo()
    await repo.upsert(_make_task())
    bogus = TaskResult(
        task_id="task-1",
        tenant_id=TENANT,
        trace_id=TRACE,
        graph_id=ECHO_GRAPH_ID,
        status="succeeded",
        output={"echo": "hi"},
        error=None,
        node_sequence=3,
        started_at=datetime(2026, 5, 15, tzinfo=UTC),
        finished_at=datetime(2026, 5, 15, tzinfo=UTC),
    )
    with pytest.raises(IllegalTaskTransitionError):
        await repo.complete(
            TENANT,
            "task-1",
            result=bogus,
            terminal_state=TaskState.RUNNING,
            updated_at=datetime(2026, 5, 15, tzinfo=UTC),
        )
