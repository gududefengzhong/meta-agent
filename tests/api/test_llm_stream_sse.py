"""API tests for ``GET /v1/tasks/{id}/llm-stream`` (Phase δ-1 SSE).

The endpoint subscribes to a :class:`ChunkBroadcaster` and relays
chunks to the client as ``event: llm.chunk`` SSE frames. These
tests cover the wire shape + lifecycle: missing task → 404,
chunks round-trip in order, terminal task state closes the stream,
broadcaster failure → 503, missing bearer → 401.

A background producer task publishes chunks into the in-memory
broadcaster while the SSE consumer reads them, mirroring the
worker → API split in production. SSE knobs are squashed so the
tests run in well under a second.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from meta_agent.api.app import create_app
from meta_agent.api.deps import (
    get_chunk_broadcaster,
    get_db_pool,
    get_request_ctx,
    get_task_repo,
    get_token_validator,
)
from meta_agent.api.routers import tasks as tasks_router
from meta_agent.core.domain.task import (
    BudgetPolicy,
    PermissionMode,
    Task,
    TaskState,
    TaskType,
)
from meta_agent.core.ports.auth import Principal, TokenValidator
from meta_agent.core.ports.chunk_broadcaster import (
    ChunkBroadcaster,
    ChunkBroadcasterError,
)
from meta_agent.core.ports.llm import LLMStreamChunk
from meta_agent.infra.security.context import RequestContext
from meta_agent.infra.streaming.in_memory import InMemoryChunkBroadcaster
from tests.worker._fakes import FakeTaskRepo

_TENANT = "tenant-test"
_PRINCIPAL = "user-test"
_BEARER = {"Authorization": "Bearer tok-test"}


class _StubTokenValidator(TokenValidator):
    async def validate(self, token: str) -> Principal | None:
        if token == "tok-test":
            return Principal(tenant_id=_TENANT, principal_id=_PRINCIPAL)
        return None


class _FakeDbPool:
    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[object]:
        yield object()


def _fixed_ctx() -> RequestContext:
    return RequestContext(
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id="trace-fixed",
        request_id="req-fixed",
    )


def _task(state: TaskState = TaskState.RUNNING) -> Task:
    return Task(
        task_id="task-1",
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id="trace-fixed",
        idempotency_key="idem-1",
        task_type=TaskType.SYSTEM_ECHO,
        graph_id=None,
        state=state,
        permission_mode=PermissionMode.AUTO,
        budget_policy=BudgetPolicy.NONE,
        input_payload={},
        created_at=datetime(2026, 6, 23, tzinfo=UTC),
        updated_at=datetime(2026, 6, 23, tzinfo=UTC),
    )


def _make_app(*, task_repo: FakeTaskRepo, broadcaster: ChunkBroadcaster) -> Any:
    app = create_app(lifespan=None)
    app.dependency_overrides[get_task_repo] = lambda: task_repo
    app.dependency_overrides[get_chunk_broadcaster] = lambda: broadcaster
    app.dependency_overrides[get_db_pool] = _FakeDbPool
    app.dependency_overrides[get_request_ctx] = _fixed_ctx
    app.dependency_overrides[get_token_validator] = _StubTokenValidator
    return app


@pytest.fixture(autouse=True)
def _fast_sse_intervals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Squash SSE timing knobs so the tests run quickly."""

    monkeypatch.setattr(tasks_router, "_LLM_STREAM_CHUNK_WAIT_S", 0.02)
    monkeypatch.setattr(tasks_router, "_LLM_STREAM_TERMINAL_GRACE_S", 0.05)
    monkeypatch.setattr(tasks_router, "_SSE_HEARTBEAT_INTERVAL_S", 100.0)
    monkeypatch.setattr(tasks_router, "_SSE_MAX_DURATION_S", 1.0)


async def test_missing_task_returns_404() -> None:
    broadcaster = InMemoryChunkBroadcaster()
    task_repo = FakeTaskRepo()
    app = _make_app(task_repo=task_repo, broadcaster=broadcaster)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/tasks/unknown/llm-stream", headers=_BEARER)
    assert response.status_code == 404


async def test_chunks_are_relayed_then_terminal_state_closes_stream() -> None:
    broadcaster = InMemoryChunkBroadcaster()
    task_repo = FakeTaskRepo()
    await task_repo.upsert(_task(state=TaskState.RUNNING))

    async def producer() -> None:
        # Wait briefly so the subscriber is registered before publish.
        await asyncio.sleep(0.05)
        for piece in ("he", "llo"):
            await broadcaster.publish(
                tenant_id=_TENANT,
                task_id="task-1",
                chunk=LLMStreamChunk(content_delta=piece),
            )
        await broadcaster.publish(
            tenant_id=_TENANT,
            task_id="task-1",
            chunk=LLMStreamChunk(finish_reason="stop"),
        )
        # Flip the task to terminal so the SSE loop closes.
        await task_repo.update_state(
            _TENANT,
            "task-1",
            TaskState.SUCCEEDED,
            datetime(2026, 6, 23, 12, 0, 5, tzinfo=UTC),
        )

    app = _make_app(task_repo=task_repo, broadcaster=broadcaster)
    producer_task = asyncio.create_task(producer())
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", timeout=5.0
        ) as client:
            response = await client.get("/v1/tasks/task-1/llm-stream", headers=_BEARER)
    finally:
        await producer_task

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    # Each chunk surfaces as one ``event: llm.chunk`` frame carrying the JSON shape.
    chunk_lines = [
        line for line in body.splitlines() if line.startswith("data: ") and "content_delta" in line
    ]
    decoded = [json.loads(line[len("data: ") :]) for line in chunk_lines]
    contents = [d["content_delta"] for d in decoded]
    assert "hello" in "".join(contents)
    # Terminal envelope closed the stream.
    assert "event: task.terminal" in body
    assert '"state": "succeeded"' in body


async def test_broadcaster_subscribe_failure_returns_503() -> None:
    class _BrokenBroadcaster(ChunkBroadcaster):
        async def publish(self, *, tenant_id: str, task_id: str, chunk: LLMStreamChunk) -> None:
            return None

        async def subscribe(self, *, tenant_id: str, task_id: str):  # type: ignore[no-untyped-def]
            raise ChunkBroadcasterError("redis down")

        async def close(self) -> None:
            return None

    task_repo = FakeTaskRepo()
    await task_repo.upsert(_task(state=TaskState.RUNNING))
    app = _make_app(task_repo=task_repo, broadcaster=_BrokenBroadcaster())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/tasks/task-1/llm-stream", headers=_BEARER)
    assert response.status_code == 503


async def test_unauthorized_without_bearer() -> None:
    broadcaster = InMemoryChunkBroadcaster()
    task_repo = FakeTaskRepo()
    app = _make_app(task_repo=task_repo, broadcaster=broadcaster)
    del app.dependency_overrides[get_request_ctx]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/tasks/task-1/llm-stream")
    assert response.status_code == 401


async def test_stream_closes_at_max_duration_when_no_chunks_and_no_terminal() -> None:
    """Task stays RUNNING and no chunks publish; the hard duration cap closes the stream."""

    broadcaster = InMemoryChunkBroadcaster()
    task_repo = FakeTaskRepo()
    await task_repo.upsert(_task(state=TaskState.RUNNING))
    app = _make_app(task_repo=task_repo, broadcaster=broadcaster)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", timeout=5.0
    ) as client:
        response = await client.get("/v1/tasks/task-1/llm-stream", headers=_BEARER)
    assert response.status_code == 200
    assert "event: task.terminal" not in response.text
