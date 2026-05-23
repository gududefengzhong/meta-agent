"""Task submission and query endpoints.

POST   /v1/tasks               – submit a new task, returns 201 + TaskResponse
GET    /v1/tasks/{task_id}     – get current task state, returns TaskResponse
GET    /v1/tasks/{task_id}/result – get completed result, returns TaskResultResponse

The handler binds the per-request :class:`RequestContext` around every
repository call so the multi-tenant isolation guard (``check_tenant``)
in the persistence layer is always satisfied.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from meta_agent.api.deps import (
    get_approval_gateway,
    get_audit_repo,
    get_chunk_broadcaster,
    get_db_pool,
    get_outbox_repo,
    get_permission_gate,
    get_request_ctx,
    get_session_repo,
    get_task_repo,
    get_task_topic,
    get_trajectory_repo,
)
from meta_agent.api.schemas import (
    AbortRequest,
    ApprovalRequest,
    PermissionDecisionRequest,
    PermissionDecisionResponse,
    SubmitTaskRequest,
    TaskResponse,
    TaskResultResponse,
    TrajectoryResponse,
)
from meta_agent.core.domain.outbox import OutboxEvent
from meta_agent.core.domain.permission import PermissionDecision
from meta_agent.core.domain.session import Session
from meta_agent.core.domain.task import Task, TaskState
from meta_agent.core.ports.chunk_broadcaster import (
    ChunkBroadcaster,
    ChunkBroadcasterError,
)
from meta_agent.core.ports.permission_gate import (
    PermissionGate,
    PermissionGateError,
)
from meta_agent.core.ports.repository import TERMINAL_TASK_STATES
from meta_agent.infra.persistence import (
    DatabasePool,
    PgAuditRepository,
    PgOutboxRepository,
    PgSessionRepository,
    PgTaskRepository,
    PgTrajectoryRepository,
)
from meta_agent.infra.persistence.approval_gateway import (
    TaskApprovalGateway,
    TaskNotAwaitingApprovalError,
)
from meta_agent.infra.security.context import RequestContext, bind_context

router = APIRouter(tags=["tasks"])


@router.post(
    "/tasks",
    status_code=status.HTTP_201_CREATED,
    response_model=TaskResponse,
    summary="Submit a new task",
)
async def submit_task(
    body: SubmitTaskRequest,
    ctx: RequestContext = Depends(get_request_ctx),
    pool: DatabasePool = Depends(get_db_pool),
    task_repo: PgTaskRepository = Depends(get_task_repo),
    outbox_repo: PgOutboxRepository = Depends(get_outbox_repo),
    session_repo: PgSessionRepository = Depends(get_session_repo),
    topic: str = Depends(get_task_topic),
) -> TaskResponse:
    task_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    task = Task(
        task_id=task_id,
        tenant_id=ctx.tenant_id,
        principal_id=ctx.principal_id,
        trace_id=ctx.trace_id,
        session_id=body.session_id,
        idempotency_key=body.idempotency_key,
        task_type=body.task_type,
        graph_id=body.graph_id,
        state=TaskState.PENDING,
        permission_mode=body.permission_mode,
        budget_policy=body.budget_policy,
        input_payload=body.input_payload,
        created_at=now,
        updated_at=now,
    )
    # The outbox row carries the command that the dispatcher will relay
    # to the queue. Writing it in the same PG transaction as the task
    # row is what makes submit atomic and closes the dual-write gap.
    event = OutboxEvent(
        event_id=str(uuid.uuid4()),
        tenant_id=ctx.tenant_id,
        trace_id=ctx.trace_id,
        aggregate_type="task",
        aggregate_id=task_id,
        topic=topic,
        payload=dict(body.input_payload),
        idempotency_key=body.idempotency_key or task_id,
        created_at=now,
    )
    # δ-1 multi-turn: when a session_id is set, ensure the Session row
    # exists (upsert is idempotent so resubmits and rapid follow-ups
    # don't race). Keeping the upsert inside the same transaction as
    # the task / outbox writes makes "first task in a session" atomic.
    session_row: Session | None = None
    if body.session_id:
        session_row = Session(
            session_id=body.session_id,
            tenant_id=ctx.tenant_id,
            principal_id=ctx.principal_id,
            created_at=now,
            last_active_at=now,
        )
    with bind_context(ctx):
        async with pool.transaction() as conn:
            if session_row is not None:
                await session_repo.upsert_in_conn(session_row, conn)
            await task_repo.upsert_in_conn(task, conn)
            await outbox_repo.enqueue_in_conn(event, conn)

    return TaskResponse(
        task_id=task.task_id,
        tenant_id=task.tenant_id,
        state=task.state,
        task_type=task.task_type,
        trace_id=task.trace_id,
        session_id=task.session_id,
        permission_mode=task.permission_mode,
        budget_policy=task.budget_policy,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@router.get(
    "/tasks/{task_id}",
    response_model=TaskResponse,
    summary="Get task state",
)
async def get_task(
    task_id: str,
    ctx: RequestContext = Depends(get_request_ctx),
    task_repo: PgTaskRepository = Depends(get_task_repo),
) -> TaskResponse:
    with bind_context(ctx):
        task = await task_repo.get(ctx.tenant_id, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task {task_id!r} not found",
        )
    return TaskResponse(
        task_id=task.task_id,
        tenant_id=task.tenant_id,
        state=task.state,
        task_type=task.task_type,
        trace_id=task.trace_id,
        session_id=task.session_id,
        permission_mode=task.permission_mode,
        budget_policy=task.budget_policy,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@router.get(
    "/tasks/{task_id}/result",
    response_model=TaskResultResponse,
    summary="Get task result",
)
async def get_task_result(
    task_id: str,
    ctx: RequestContext = Depends(get_request_ctx),
    task_repo: PgTaskRepository = Depends(get_task_repo),
) -> TaskResultResponse:
    with bind_context(ctx):
        result = await task_repo.get_result(ctx.tenant_id, task_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"result for task {task_id!r} not yet available",
        )
    return TaskResultResponse(
        task_id=result.task_id,
        status=result.status,
        graph_id=result.graph_id,
        output=result.output,
        error=result.error,
        node_sequence=result.node_sequence,
        started_at=result.started_at,
        finished_at=result.finished_at,
    )


@router.post(
    "/tasks/{task_id}/approve",
    response_model=TaskResponse,
    summary="Approve a task paused at a human_gate",
)
async def approve_task(
    task_id: str,
    body: ApprovalRequest,
    ctx: RequestContext = Depends(get_request_ctx),
    gateway: TaskApprovalGateway = Depends(get_approval_gateway),
) -> TaskResponse:
    """Resume a task currently in ``AWAITING_APPROVAL``.

    Atomically writes a new checkpoint that carries the operator's
    decision (and optional feedback), flips the task row back to
    ``RUNNING``, and enqueues a fresh outbox event so the next
    available worker picks the task up and resumes from the gate.
    """

    with bind_context(ctx):
        try:
            task = await gateway.approve(ctx.tenant_id, task_id, feedback=body.feedback)
        except TaskNotAwaitingApprovalError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
    return TaskResponse(
        task_id=task.task_id,
        tenant_id=task.tenant_id,
        state=task.state,
        task_type=task.task_type,
        trace_id=task.trace_id,
        session_id=task.session_id,
        permission_mode=task.permission_mode,
        budget_policy=task.budget_policy,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@router.post(
    "/tasks/{task_id}/abort",
    response_model=TaskResponse,
    summary="Abort a task paused at a human_gate",
)
async def abort_task(
    task_id: str,
    body: AbortRequest,
    ctx: RequestContext = Depends(get_request_ctx),
    gateway: TaskApprovalGateway = Depends(get_approval_gateway),
) -> TaskResponse:
    """Terminate a paused task as ``CANCELLED``.

    No checkpoint is appended (the task does not resume) and no
    :class:`TaskResult` is written — ``cancelled`` lives outside the
    result contract by design. ``reason`` is reserved for audit
    emission in γ-B; ignored here.
    """

    with bind_context(ctx):
        try:
            task = await gateway.abort(ctx.tenant_id, task_id, reason=body.reason)
        except TaskNotAwaitingApprovalError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
    return TaskResponse(
        task_id=task.task_id,
        tenant_id=task.tenant_id,
        state=task.state,
        task_type=task.task_type,
        trace_id=task.trace_id,
        session_id=task.session_id,
        permission_mode=task.permission_mode,
        budget_policy=task.budget_policy,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@router.post(
    "/tasks/{task_id}/permissions/{prompt_id}/decide",
    response_model=PermissionDecisionResponse,
    summary="Respond to an inline permission prompt",
)
async def decide_permission(
    task_id: str,
    prompt_id: str,
    body: PermissionDecisionRequest,
    ctx: RequestContext = Depends(get_request_ctx),
    task_repo: PgTaskRepository = Depends(get_task_repo),
    gate: PermissionGate = Depends(get_permission_gate),
) -> PermissionDecisionResponse:
    """Route a client's permission decision to the waiting worker.

    Validates task ownership (404 on missing / wrong tenant) and
    that the task is still in a state where a decision could
    matter — a terminal task has no live worker to receive the
    decision, so we 409 instead of silently swallowing the call.

    A decision for a ``prompt_id`` nobody is waiting on (the worker
    already timed out, or the prompt was never issued) is
    silently accepted at the gate per the
    :class:`PermissionGate.deliver` contract — we have no way to
    distinguish stale from spurious here.
    """

    with bind_context(ctx):
        task = await task_repo.get(ctx.tenant_id, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task {task_id!r} not found",
        )
    if task.state not in (TaskState.PENDING, TaskState.RUNNING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"task {task_id!r} is in terminal state {task.state.value!r}; "
                "no live worker to receive the decision"
            ),
        )
    decision = PermissionDecision(
        prompt_id=prompt_id,
        allow=body.allow,
        reason=body.reason,
        decided_at=datetime.now(UTC),
    )
    try:
        await gate.deliver(decision)
    except PermissionGateError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="permission gate unavailable",
        ) from exc
    return PermissionDecisionResponse(prompt_id=prompt_id, allow=body.allow)


@router.get(
    "/tasks/{task_id}/permissions/stream",
    summary="Stream inline permission prompts for one task (SSE)",
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def stream_task_permission_prompts(
    request: Request,
    task_id: str,
    ctx: RequestContext = Depends(get_request_ctx),
    task_repo: PgTaskRepository = Depends(get_task_repo),
    gate: PermissionGate = Depends(get_permission_gate),
) -> StreamingResponse:
    """SSE stream of :class:`PermissionPrompt` events for a task.

    The worker publishes a prompt every time it calls
    :meth:`PermissionGate.request`; this endpoint relays each
    prompt to the connected client as an SSE
    ``event: permission.prompt`` frame carrying the prompt's JSON.

    The stream closes when the task enters a terminal state (after
    a short grace window so any in-flight prompt has time to land),
    the client disconnects, or after :data:`_SSE_MAX_DURATION_S`.

    No replay: pub/sub semantics mean prompts published before the
    subscription is established are lost. Clients should connect
    *before* submitting the task or *immediately after* — the
    handful-of-millisecond gap is acceptable for interactive UX.
    """

    with bind_context(ctx):
        task = await task_repo.get(ctx.tenant_id, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task {task_id!r} not found",
        )

    try:
        iterator = await gate.subscribe_prompts(tenant_id=ctx.tenant_id, task_id=task_id)
    except PermissionGateError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="permission gate unavailable",
        ) from exc

    async def event_iterator() -> AsyncIterator[bytes]:
        start = datetime.now(UTC)
        last_heartbeat = start
        terminal_observed_at: datetime | None = None
        try:
            with bind_context(ctx):
                while True:
                    if await request.is_disconnected():
                        return
                    now = datetime.now(UTC)
                    if (now - start).total_seconds() >= _SSE_MAX_DURATION_S:
                        return
                    if (
                        terminal_observed_at is not None
                        and (now - terminal_observed_at).total_seconds()
                        >= _LLM_STREAM_TERMINAL_GRACE_S
                    ):
                        return
                    try:
                        prompt = await asyncio.wait_for(
                            iterator.__anext__(),
                            timeout=_LLM_STREAM_CHUNK_WAIT_S,
                        )
                    except TimeoutError:
                        prompt = None
                    except StopAsyncIteration:
                        return
                    if prompt is not None:
                        yield (
                            "event: permission.prompt\ndata: " + prompt.model_dump_json() + "\n\n"
                        ).encode()
                        continue
                    current = await task_repo.get(ctx.tenant_id, task_id)
                    if (
                        terminal_observed_at is None
                        and current is not None
                        and current.state in TERMINAL_TASK_STATES
                    ):
                        terminal_observed_at = now
                        yield (
                            f'event: task.terminal\ndata: {{"state": "{current.state.value}"}}\n\n'
                        ).encode()
                    tick = datetime.now(UTC)
                    if (tick - last_heartbeat).total_seconds() >= _SSE_HEARTBEAT_INTERVAL_S:
                        yield b": heartbeat\n\n"
                        last_heartbeat = tick
        finally:
            await iterator.aclose()

    return StreamingResponse(
        event_iterator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/tasks/{task_id}/trajectory",
    response_model=TrajectoryResponse,
    summary="Get the merged audit + checkpoint + LLM-usage timeline for one task",
)
async def get_task_trajectory(
    task_id: str,
    limit_per_source: int = Query(default=1000, ge=1, le=1000),
    ctx: RequestContext = Depends(get_request_ctx),
    task_repo: PgTaskRepository = Depends(get_task_repo),
    trajectory_repo: PgTrajectoryRepository = Depends(get_trajectory_repo),
) -> TrajectoryResponse:
    """Return a time-ordered timeline of everything that happened to a task.

    Merges rows from ``audit_events``, ``task_checkpoints`` and
    ``llm_usage_logs`` into a single list, ordered by occurrence
    timestamp. Each item carries a ``kind`` discriminator
    (``"audit"`` / ``"checkpoint"`` / ``"usage"``) so the API consumer
    or a future Web UI can render them appropriately.

    ``truncated`` is ``True`` when any of the three source queries hit
    its row cap; the operator should narrow the time window via the
    paginated drill-down endpoints that land in γ-B-2 / γ-C.
    """

    with bind_context(ctx):
        task = await task_repo.get(ctx.tenant_id, task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"task {task_id!r} not found",
            )
        page = await trajectory_repo.list_for_task(
            ctx.tenant_id,
            task_id,
            limit_per_source=limit_per_source,
        )
    return TrajectoryResponse(
        items=[item.model_dump(mode="json") for item in page.items],
        truncated=page.truncated,
    )


_SSE_POLL_INTERVAL_S = 1.5
"""Default delay between polls for new audit rows in the SSE loop.

Trade-off: lower means snappier client updates and higher DB load;
higher means the opposite. 1.5s is the sweet spot for the bug_fix
class of tasks (a few events per second peak) without turning the
audit table into a hot read partition. Operators that need real-time
should switch to the Redis pub/sub variant — out of γ-D scope.
"""

_SSE_HEARTBEAT_INTERVAL_S = 15.0
"""How often to send an SSE keepalive comment.

Anything past ~30s and intermediaries (proxies, load balancers) start
silently closing the connection. 15s is conservative and keeps the
heartbeat traffic negligible (one byte per interval per stream).
"""

_SSE_MAX_DURATION_S = 30 * 60.0
"""Hard cap on a single SSE connection lifetime.

The client is expected to reconnect with the last seen cursor; this
ceiling stops runaway connections from accumulating in the API tier
when a client forgets to close. The task state machine itself does
not need a long-lived stream — terminal states close the loop early.
"""


@router.get(
    "/tasks/{task_id}/events",
    summary="Stream task lifecycle events (SSE)",
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def stream_task_events(
    request: Request,
    task_id: str,
    last_event_id: str | None = Query(default=None, max_length=128),
    last_event_at: datetime | None = Query(default=None),
    ctx: RequestContext = Depends(get_request_ctx),
    task_repo: PgTaskRepository = Depends(get_task_repo),
    audit_repo: PgAuditRepository = Depends(get_audit_repo),
) -> StreamingResponse:
    """Server-Sent Events stream of audit rows for one task.

    Resumes from ``(last_event_at, last_event_id)`` if both are
    supplied — the client should pass back the ``id:`` of the most
    recent event it saw to avoid re-receiving rows on reconnect. The
    stream closes when the task enters a terminal state or after
    :data:`_SSE_MAX_DURATION_S` (whichever comes first); the client
    must reconnect to keep watching.

    Polling-based v0: a future PR can swap the inner loop for a
    Redis pub/sub listener without changing the wire shape.
    """

    with bind_context(ctx):
        task = await task_repo.get(ctx.tenant_id, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task {task_id!r} not found",
        )

    cursor: tuple[datetime, str] | None = None
    if last_event_at is not None and last_event_id is not None:
        cursor = (last_event_at, last_event_id)

    async def event_iterator() -> AsyncIterator[bytes]:
        nonlocal cursor
        start = datetime.now(UTC)
        last_heartbeat = start
        with bind_context(ctx):
            while True:
                if await request.is_disconnected():
                    return
                if (datetime.now(UTC) - start).total_seconds() >= _SSE_MAX_DURATION_S:
                    return
                events = await audit_repo.list_for_task_since(
                    ctx.tenant_id, task_id, after=cursor, limit=100
                )
                for ev in events:
                    cursor = (ev.occurred_at, ev.event_id)
                    body = json.dumps(
                        {
                            "event_id": ev.event_id,
                            "task_id": ev.task_id,
                            "trace_id": ev.trace_id,
                            "action": ev.action,
                            "payload": ev.payload,
                            "occurred_at": ev.occurred_at.isoformat(),
                        }
                    )
                    yield f"id: {ev.event_id}\nevent: {ev.action}\ndata: {body}\n\n".encode()
                # Refresh the task each poll so a terminal transition
                # observed by the database closes the stream cleanly.
                current = await task_repo.get(ctx.tenant_id, task_id)
                if current is not None and current.state in TERMINAL_TASK_STATES:
                    yield (
                        f'event: task.terminal\ndata: {{"state": "{current.state.value}"}}\n\n'
                    ).encode()
                    return
                now = datetime.now(UTC)
                if (now - last_heartbeat).total_seconds() >= _SSE_HEARTBEAT_INTERVAL_S:
                    yield b": heartbeat\n\n"
                    last_heartbeat = now
                await asyncio.sleep(_SSE_POLL_INTERVAL_S)

    return StreamingResponse(
        event_iterator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx response buffering
        },
    )


_LLM_STREAM_CHUNK_WAIT_S = 1.0
"""How long to wait for the next chunk before checking task state.

Keeps the loop responsive to terminal-state transitions and client
disconnects even when the LLM stream is silent (e.g. the worker is
between LLM calls, executing tools). Short enough that a finished
task closes the stream within ~1s; long enough that an active LLM
stream is dominated by chunk delivery rather than tick overhead.
"""

_LLM_STREAM_TERMINAL_GRACE_S = 2.0
"""How long to keep the subscription open after the task goes terminal.

A chunk may already be in flight (worker published, broadcaster
delivering) when the task transitions; close immediately and the
client misses the tail. Two seconds is enough for Redis pub/sub
delivery to settle without holding the connection meaningfully
longer than the task.
"""


@router.get(
    "/tasks/{task_id}/llm-stream",
    summary="Stream LLM token chunks for one task (SSE)",
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def stream_task_llm_chunks(
    request: Request,
    task_id: str,
    ctx: RequestContext = Depends(get_request_ctx),
    task_repo: PgTaskRepository = Depends(get_task_repo),
    broadcaster: ChunkBroadcaster = Depends(get_chunk_broadcaster),
) -> StreamingResponse:
    """SSE stream of :class:`LLMStreamChunk` events for a task.

    The worker's outermost LLM decorator
    (:class:`BroadcastingLLMClient`) publishes each chunk to a
    per-task channel; this endpoint subscribes and relays them as
    SSE ``event: llm.chunk`` frames carrying the chunk's JSON
    serialisation. The stream closes when the task enters a
    terminal state (after a short grace window so any in-flight
    chunk has time to land), the client disconnects, or after
    :data:`_SSE_MAX_DURATION_S`.

    No replay: pub/sub semantics mean chunks published before the
    subscription is established are lost. Clients that need
    durable history should consume ``/tasks/{id}/events`` (audit
    stream) and / or fetch the post-hoc trajectory.
    """

    with bind_context(ctx):
        task = await task_repo.get(ctx.tenant_id, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task {task_id!r} not found",
        )

    try:
        iterator = await broadcaster.subscribe(tenant_id=ctx.tenant_id, task_id=task_id)
    except ChunkBroadcasterError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="chunk broadcaster unavailable",
        ) from exc

    async def event_iterator() -> AsyncIterator[bytes]:
        start = datetime.now(UTC)
        last_heartbeat = start
        terminal_observed_at: datetime | None = None
        try:
            with bind_context(ctx):
                while True:
                    if await request.is_disconnected():
                        return
                    now = datetime.now(UTC)
                    if (now - start).total_seconds() >= _SSE_MAX_DURATION_S:
                        return
                    if (
                        terminal_observed_at is not None
                        and (now - terminal_observed_at).total_seconds()
                        >= _LLM_STREAM_TERMINAL_GRACE_S
                    ):
                        return
                    try:
                        chunk = await asyncio.wait_for(
                            iterator.__anext__(),
                            timeout=_LLM_STREAM_CHUNK_WAIT_S,
                        )
                    except TimeoutError:
                        chunk = None
                    except StopAsyncIteration:
                        return
                    if chunk is not None:
                        yield (
                            "event: llm.chunk\ndata: " + chunk.model_dump_json() + "\n\n"
                        ).encode()
                        continue
                    # No chunk in this tick — check terminal state.
                    current = await task_repo.get(ctx.tenant_id, task_id)
                    if (
                        terminal_observed_at is None
                        and current is not None
                        and current.state in TERMINAL_TASK_STATES
                    ):
                        terminal_observed_at = now
                        yield (
                            f'event: task.terminal\ndata: {{"state": "{current.state.value}"}}\n\n'
                        ).encode()
                    tick = datetime.now(UTC)
                    if (tick - last_heartbeat).total_seconds() >= _SSE_HEARTBEAT_INTERVAL_S:
                        yield b": heartbeat\n\n"
                        last_heartbeat = tick
        finally:
            await iterator.aclose()

    return StreamingResponse(
        event_iterator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
