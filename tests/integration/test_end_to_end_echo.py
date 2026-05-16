"""End-to-end echo graph integration test.

Flow (P1-H closes the outbox path so submission and relay are split):
1. Create a SYSTEM_ECHO task in PG (PENDING, graph_id=builtin.echo)
   AND enqueue the matching outbox event in the SAME transaction —
   mirroring exactly what the ``POST /v1/tasks`` router does.
2. Run :class:`OutboxDispatcher` once to relay the outbox row to the
   Redis task-commands stream.
3. Run WorkerLoop.run_once() with real Postgres repos and the echo GraphRegistry.
4. Assert:
   - the outbox row transitions to ``dispatched``.
   - tasks.result_json is populated with status=succeeded and
     output={"echo": <message>}.
   - task.state is SUCCEEDED in the DB.
   - Redis PEL is empty (message was XACKed).
   - task_checkpoints has exactly 3 rows: plan → execute → review.

The echo graph performs no I/O and never calls the LLM, so a stub
LLMClient that raises on invocation is sufficient to catch accidental
calls.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis

from meta_agent.core.domain.outbox import OutboxEvent, OutboxStatus
from meta_agent.core.domain.task import Task, TaskState, TaskType
from meta_agent.core.orchestration import GraphDeps, GraphRegistry
from meta_agent.core.orchestration.graphs.echo import ECHO_GRAPH_ID, build_echo_graph
from meta_agent.core.ports.llm import LLMClient, LLMRequest, LLMResponse
from meta_agent.infra.persistence import (
    DatabasePool,
    OutboxDispatcher,
    PgAuditRepository,
    PgCheckpointRepository,
    PgOutboxRepository,
    PgTaskRepository,
)
from meta_agent.infra.queue import RedisStreamConsumer, RedisStreamPublisher, stream_name_for_topic
from meta_agent.infra.security.context import RequestContext, bind_context
from meta_agent.worker.runner import WorkerConfig, WorkerLoop

pytestmark = pytest.mark.integration

_TOPIC = "task.commands"
_TENANT = "tenant-echo"
_PRINCIPAL = "system"


class _StubLLM(LLMClient):
    """Echo graph must never call the LLM; raise loudly if it does."""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise AssertionError("echo graph must not invoke the LLM")

    async def close(self) -> None:
        pass


def _make_registry() -> GraphRegistry:
    registry = GraphRegistry()
    registry.register(
        ECHO_GRAPH_ID,
        lambda deps: build_echo_graph(),
        default_for=TaskType.SYSTEM_ECHO,
    )
    registry.materialize(GraphDeps(llm=_StubLLM()))
    return registry


async def test_echo_graph_end_to_end(db_pool: DatabasePool, redis_client: Redis) -> None:
    """Full pipeline: task in PG → message on Redis → worker processes → result in PG."""

    task_id = f"echo-{uuid.uuid4().hex[:8]}"
    trace_id = f"trace-{uuid.uuid4().hex[:8]}"
    message = "hello from integration test"
    now = datetime.now(UTC)

    ctx = RequestContext(
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id=trace_id,
        request_id=task_id,
    )

    # ── 1. Persist task + outbox row atomically (mirrors POST /v1/tasks) ────
    task = Task(
        task_id=task_id,
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id=trace_id,
        idempotency_key=f"idem-{task_id}",
        task_type=TaskType.SYSTEM_ECHO,
        graph_id=ECHO_GRAPH_ID,
        state=TaskState.PENDING,
        input_payload={"message": message},
        created_at=now,
        updated_at=now,
    )
    event = OutboxEvent(
        event_id=f"evt-{uuid.uuid4().hex[:8]}",
        tenant_id=_TENANT,
        trace_id=trace_id,
        aggregate_type="task",
        aggregate_id=task_id,
        topic=_TOPIC,
        payload={"message": message},
        idempotency_key=f"idem-{task_id}",
        created_at=now,
    )
    task_repo = PgTaskRepository(db_pool)
    outbox_repo = PgOutboxRepository(db_pool)
    with bind_context(ctx):
        async with db_pool.transaction() as conn:
            await task_repo.upsert_in_conn(task, conn)
            await outbox_repo.enqueue_in_conn(event, conn)

    # ── 2. Dispatcher relays outbox row → Redis stream ──────────────────────
    publisher = RedisStreamPublisher(redis_client)
    dispatcher = OutboxDispatcher(outbox_repo, publisher)
    drained = await dispatcher.run_once()
    assert drained == 1, f"expected 1 outbox row dispatched, got {drained}"

    # ── 3. Bootstrap worker (group unique per run to avoid cross-test leakage) ──
    group = f"echo-workers-{uuid.uuid4().hex[:6]}"
    consumer = RedisStreamConsumer(
        redis_client,
        topic=_TOPIC,
        group=group,
        consumer_name="worker-1",
        batch_size=8,
        block_ms=200,
    )
    checkpoint_repo = PgCheckpointRepository(db_pool)
    audit_repo = PgAuditRepository(db_pool)
    worker = WorkerLoop(
        stream=consumer,
        tasks=task_repo,
        checkpoints=checkpoint_repo,
        audits=audit_repo,
        registry=_make_registry(),
        config=WorkerConfig(max_attempts=3, block_ms=200),
    )

    # ── 4. Process one batch ─────────────────────────────────────────────────
    handled = await worker.run_once()
    assert handled == 1, f"expected 1 message handled, got {handled}"

    # ── 5. TaskResult persisted to tasks.result_json ─────────────────────────
    with bind_context(ctx):
        result = await task_repo.get_result(_TENANT, task_id)
    assert result is not None, "result_json must be populated after worker run"
    assert result.status == "succeeded"
    assert result.output == {"echo": message}
    assert result.task_id == task_id
    assert result.graph_id == ECHO_GRAPH_ID

    # ── 6. Task row transitioned to SUCCEEDED ────────────────────────────────
    with bind_context(ctx):
        fetched = await task_repo.get(_TENANT, task_id)
    assert fetched is not None
    assert fetched.state == TaskState.SUCCEEDED

    # ── 7. Redis PEL empty — message was XACKed ──────────────────────────────
    stream_key = stream_name_for_topic(_TOPIC)
    pending = await redis_client.xpending(stream_key, group)
    assert pending["pending"] == 0, "PEL must be empty after successful ack"

    # ── 8. Checkpoint chain complete (3 checkpoints, one per node) ──────────
    # _persist_step records state.current_node AFTER graph.step(), so
    # current_node holds the NEXT destination:
    #   plan ran  → state.current_node="execute"  → checkpoint node_name="execute"
    #   execute ran → state.current_node="review"  → checkpoint node_name="review"
    #   review ran  → state.current_node="__end__" → checkpoint node_name="__end__"
    with bind_context(ctx):
        checkpoints = await checkpoint_repo.list_for_task(_TENANT, task_id)
    assert len(checkpoints) == 3, f"expected 3 checkpoints, got {len(checkpoints)}"
    node_names = [cp.node_name for cp in checkpoints]
    assert node_names == ["execute", "review", "__end__"]
    # Sequences must be strictly increasing
    seqs = [cp.sequence for cp in checkpoints]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3

    # ── 9. Outbox row transitioned to DISPATCHED ────────────────────────────
    relayed = await outbox_repo.get(event.event_id)
    assert relayed is not None
    assert relayed.status == OutboxStatus.DISPATCHED
