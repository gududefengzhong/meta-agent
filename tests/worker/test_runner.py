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
from meta_agent.core.domain.trajectory import TrajectoryPage
from meta_agent.core.orchestration import (
    Graph,
    GraphDeps,
    GraphRegistry,
    NodeResult,
    TaskChainRegistry,
    TaskRunState,
)
from meta_agent.core.orchestration.graphs.echo import ECHO_GRAPH_ID, build_echo_graph
from meta_agent.core.orchestration.result import TaskErrorCode
from meta_agent.core.orchestration.state import END, START
from meta_agent.core.ports.message import MessageEnvelope
from meta_agent.core.ports.repository import IllegalTaskTransitionError
from meta_agent.core.ports.task_submitter import FollowUpSpec, TaskSubmitter
from meta_agent.infra.queue.redis_consumer import DeliveredMessage
from meta_agent.worker.runner import WorkerConfig, WorkerLoop
from tests.core.orchestration._fakes import fake_deps

from ._fakes import (
    FakeAuditRepo,
    FakeCheckpointRepo,
    FakeStream,
    FakeTaskRepo,
    FakeTaskSubmitter,
    FakeWorkspaceManager,
)

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
    submitter: TaskSubmitter | None = None,
    chain_registry: TaskChainRegistry | None = None,
    trajectory: object | None = None,
    trajectory_exporter: object | None = None,
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
        submitter=submitter,
        chain_registry=chain_registry,
        trajectory=trajectory,  # type: ignore[arg-type]
        trajectory_exporter=trajectory_exporter,  # type: ignore[arg-type]
        clock=_fixed_clock(),
        id_factory=_id_factory(),
        config=config,
    )
    return loop, tasks, checkpoints, audits, stream


class _FakeTrajectoryRepo:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def list_for_task(
        self,
        tenant_id: str,
        task_id: str,
        *,
        since: datetime | None = None,
        limit_per_source: int = 1000,
    ) -> TrajectoryPage:
        self.calls.append((tenant_id, task_id))
        return TrajectoryPage(items=(), truncated=False)


class _FakeExportResult:
    trace_id = "0" * 32
    observation_count = 1


class _FakeTrajectoryExporter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    async def export_task(
        self,
        *,
        task_id: str,
        task: dict[str, object],
        trajectory: dict[str, object],
    ) -> _FakeExportResult:
        self.calls.append({"task_id": task_id, "task": task, "trajectory": trajectory})
        if self.fail:
            raise RuntimeError("langfuse down")
        return _FakeExportResult()


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


async def test_terminal_task_exports_trajectory_when_configured() -> None:
    trajectory = _FakeTrajectoryRepo()
    exporter = _FakeTrajectoryExporter()
    loop, tasks, _checkpoints, audits, stream = _build_loop(
        trajectory=trajectory,
        trajectory_exporter=exporter,
    )
    await tasks.upsert(_make_task())
    stream.push([_delivered()])

    await loop.run_once()

    assert trajectory.calls == [(TENANT, "task-1")]
    assert len(exporter.calls) == 1
    call = exporter.calls[0]
    assert call["task_id"] == "task-1"
    exported_task = call["task"]
    assert isinstance(exported_task, dict)
    assert exported_task["state"] == "succeeded"
    assert exported_task["result_status"] == "succeeded"
    assert exported_task["failure_category"] is None
    assert "observability.langfuse_exported" in audits.actions()
    exported_audit = next(e for e in audits.rows if e.action == "observability.langfuse_exported")
    assert exported_audit.payload["langfuse_trace_id"] == "0" * 32
    assert stream.acked == ["1-0"]


async def test_langfuse_export_failure_is_best_effort() -> None:
    trajectory = _FakeTrajectoryRepo()
    exporter = _FakeTrajectoryExporter(fail=True)
    loop, tasks, _checkpoints, audits, stream = _build_loop(
        trajectory=trajectory,
        trajectory_exporter=exporter,
    )
    await tasks.upsert(_make_task())
    stream.push([_delivered()])

    await loop.run_once()

    task = await tasks.get(TENANT, "task-1")
    assert task is not None and task.state == TaskState.SUCCEEDED
    assert stream.acked == ["1-0"]
    assert "observability.langfuse_export_failed" in audits.actions()


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
    assert result.error.details == {
        "failure_category": "infra_error",
        "current_node": START,
        "sequence": 2,
    }
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


# --- A.3.1: workspace dispatch ---------------------------------------------

_WS_GRAPH_ID = "test.requires_workspace"


def _build_ws_graph() -> Graph:
    """One-node graph that copies workspace data into ``output``."""

    async def only(state: TaskRunState) -> NodeResult:
        return NodeResult(
            data_update={
                "output": {
                    "workspace_path": state.data.get("_workspace_path"),
                    "workspace_branch": state.data.get("_workspace_branch"),
                }
            }
        )

    g = Graph(_WS_GRAPH_ID)
    g.add_node("only", only)
    g.set_entry("only")
    g.add_edge("only", END)
    g.compile()
    return g


def _registry_with_ws() -> GraphRegistry:
    registry = GraphRegistry()
    registry.register(
        _WS_GRAPH_ID,
        lambda _deps: _build_ws_graph(),
        requires_workspace=True,
    )
    registry.materialize(fake_deps())
    return registry


def _build_loop_ws(
    *,
    workspaces: FakeWorkspaceManager | None,
) -> tuple[WorkerLoop, FakeTaskRepo, FakeAuditRepo, FakeStream]:
    tasks = FakeTaskRepo()
    audits = FakeAuditRepo()
    stream = FakeStream()
    loop = WorkerLoop(
        stream=stream,
        tasks=tasks,
        checkpoints=FakeCheckpointRepo(),
        audits=audits,
        registry=_registry_with_ws(),
        workspaces=workspaces,
        clock=_fixed_clock(),
        id_factory=_id_factory(),
    )
    return loop, tasks, audits, stream


async def test_workspace_required_graph_provisions_runs_and_cleans() -> None:
    wm = FakeWorkspaceManager()
    loop, tasks, audits, stream = _build_loop_ws(workspaces=wm)
    task = _make_task(
        graph_id=_WS_GRAPH_ID,
        payload={"repo_url": "https://example.invalid/repo.git", "base_ref": "main"},
    )
    await tasks.upsert(task)
    stream.push([_delivered()])

    await loop.run_once()

    # Both lifecycle hooks fired exactly once.
    assert len(wm.provisioned) == 1
    assert len(wm.cleaned) == 1
    ws = wm.provisioned[0]
    assert wm.cleaned[0] is ws
    assert ws.branch == "agent/task-1"
    assert ws.repo_url == "https://example.invalid/repo.git"
    assert ws.base_ref == "main"

    # Workspace info reached graph state.
    result = await tasks.get_result(TENANT, "task-1")
    assert result is not None
    assert result.output == {
        "workspace_path": ws.worktree_path,
        "workspace_branch": "agent/task-1",
    }

    actions = audits.actions()
    assert actions.count("workspace.provisioned") == 1
    assert actions.count("workspace.cleaned") == 1
    # Provision audit precedes the terminal task event, cleanup follows it.
    assert actions.index("workspace.provisioned") < actions.index("task.succeeded")
    assert actions.index("task.succeeded") < actions.index("workspace.cleaned")
    assert stream.acked == ["1-0"]


async def test_workspace_required_graph_without_manager_aborts_gracefully() -> None:
    loop, tasks, audits, stream = _build_loop_ws(workspaces=None)
    await tasks.upsert(_make_task(graph_id=_WS_GRAPH_ID))
    stream.push([_delivered()])

    await loop.run_once()

    assert audits.actions() == ["worker.workspace_unavailable"]
    assert stream.acked == ["1-0"]
    # No state transition: task stays pending.
    task = await tasks.get(TENANT, "task-1")
    assert task is not None and task.state == TaskState.PENDING


async def test_workspace_provision_failure_keeps_message_in_pel() -> None:
    wm = FakeWorkspaceManager(fail_provision=True)
    loop, tasks, audits, stream = _build_loop_ws(workspaces=wm)
    await tasks.upsert(_make_task(graph_id=_WS_GRAPH_ID))
    stream.push([_delivered()])

    # _handle swallows the exception (PEL retry), so run_once returns normally.
    await loop.run_once()

    assert "workspace.provision_failed" in audits.actions()
    assert wm.cleaned == []  # nothing to clean
    assert stream.acked == []  # left in PEL for redelivery


async def test_workspace_cleanup_failure_does_not_block_terminal_write() -> None:
    wm = FakeWorkspaceManager(fail_cleanup=True)
    loop, tasks, audits, stream = _build_loop_ws(workspaces=wm)
    await tasks.upsert(_make_task(graph_id=_WS_GRAPH_ID))
    stream.push([_delivered()])

    await loop.run_once()

    actions = audits.actions()
    assert "task.succeeded" in actions
    assert "workspace.cleanup_failed" in actions
    assert "workspace.cleaned" not in actions
    # Terminal state recorded despite cleanup failure.
    task = await tasks.get(TENANT, "task-1")
    assert task is not None and task.state == TaskState.SUCCEEDED
    assert stream.acked == ["1-0"]


async def test_non_workspace_graph_skips_provision_and_cleanup() -> None:
    wm = FakeWorkspaceManager()
    loop, tasks, _ckpt, _audits, stream = _build_loop(registry=_registry_with_echo())
    # Inject the workspace manager on the already-built loop: a
    # non-workspace graph must not call into it at all.
    loop._workspaces = wm
    await tasks.upsert(_make_task())
    stream.push([_delivered()])

    await loop.run_once()

    assert wm.provisioned == []
    assert wm.cleaned == []
    task = await tasks.get(TENANT, "task-1")
    assert task is not None and task.state == TaskState.SUCCEEDED


# --- chain hook: BUG_FIX → AUTO_PR-style follow-up enqueue ----------------


def _spec_factory(parent_task_id: str) -> FollowUpSpec:
    return FollowUpSpec(
        task_type=TaskType.AUTO_PR,
        input_payload={"_parent_task_id": parent_task_id, "repo_url": "x"},
        idempotency_key=f"chain:{parent_task_id}:auto_pr",
        topic="task.commands",
    )


def _chain_registry_always_fires() -> TaskChainRegistry:
    """Bind a SYSTEM_ECHO parent to a deterministic follow-up spec.

    Using SYSTEM_ECHO keeps the test free of workspace plumbing while
    still exercising the runner's chain hook in its real shape.
    """
    registry = TaskChainRegistry()
    registry.register(
        TaskType.SYSTEM_ECHO,
        lambda parent, _result: _spec_factory(parent.task_id),
    )
    return registry


async def test_chain_hook_fires_after_success_and_audits_enqueued() -> None:
    submitter = FakeTaskSubmitter()
    loop, tasks, _ckpt, audits, stream = _build_loop(
        submitter=submitter,
        chain_registry=_chain_registry_always_fires(),
    )
    await tasks.upsert(_make_task())
    stream.push([_delivered()])

    await loop.run_once()

    assert len(submitter.calls) == 1
    parent, spec = submitter.calls[0]
    assert parent.task_id == "task-1"
    assert spec.idempotency_key == "chain:task-1:auto_pr"
    actions = audits.actions()
    assert "task.succeeded" in actions
    assert "task.chain_enqueued" in actions
    # ack still happens at the end of the run.
    assert stream.acked == ["1-0"]


async def test_chain_hook_skips_when_no_policy_registered() -> None:
    submitter = FakeTaskSubmitter()
    # Empty registry: ``derive`` returns None, no enqueue, no audit.
    loop, tasks, _ckpt, audits, _stream = _build_loop(
        submitter=submitter,
        chain_registry=TaskChainRegistry(),
    )
    await tasks.upsert(_make_task())
    _stream.push([_delivered()])

    await loop.run_once()

    assert submitter.calls == []
    assert "task.chain_enqueued" not in audits.actions()


async def test_chain_hook_audits_duplicate_when_submitter_returns_none() -> None:
    submitter = FakeTaskSubmitter(simulate_duplicate=True)
    loop, tasks, _ckpt, audits, _stream = _build_loop(
        submitter=submitter,
        chain_registry=_chain_registry_always_fires(),
    )
    await tasks.upsert(_make_task())
    _stream.push([_delivered()])

    await loop.run_once()

    assert len(submitter.calls) == 1
    actions = audits.actions()
    assert "task.chain_skipped" in actions
    assert "task.chain_enqueued" not in actions


async def test_chain_hook_audits_failure_without_breaking_parent() -> None:
    submitter = FakeTaskSubmitter(raise_on_submit=RuntimeError("db down"))
    loop, tasks, _ckpt, audits, stream = _build_loop(
        submitter=submitter,
        chain_registry=_chain_registry_always_fires(),
    )
    await tasks.upsert(_make_task())
    stream.push([_delivered()])

    await loop.run_once()

    # Parent task remains SUCCEEDED, message still acked: chain
    # failures must never roll back a finished parent run.
    task = await tasks.get(TENANT, "task-1")
    assert task is not None and task.state == TaskState.SUCCEEDED
    assert stream.acked == ["1-0"]
    assert "task.chain_failed" in audits.actions()


async def test_chain_hook_disabled_when_submitter_missing() -> None:
    # ``chain_registry`` set but ``submitter`` is None: the hook is a
    # no-op so default unit-test bootstraps don't accidentally chain.
    loop, tasks, _ckpt, audits, _stream = _build_loop(
        chain_registry=_chain_registry_always_fires(),
    )
    await tasks.upsert(_make_task())
    _stream.push([_delivered()])

    await loop.run_once()

    actions = audits.actions()
    assert "task.chain_enqueued" not in actions
    assert "task.chain_skipped" not in actions
