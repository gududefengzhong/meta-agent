"""API tests for ``GET /v1/sessions/{id}`` + ``/messages`` (Phase δ-1)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from httpx import ASGITransport, AsyncClient

from meta_agent.api.app import create_app
from meta_agent.api.deps import (
    get_db_pool,
    get_request_ctx,
    get_session_repo,
    get_task_repo,
    get_token_validator,
)
from meta_agent.core.domain.session import Session
from meta_agent.core.domain.task import (
    BudgetPolicy,
    PermissionMode,
    Task,
    TaskState,
    TaskType,
)
from meta_agent.core.orchestration.result import TaskResult
from meta_agent.core.ports.auth import Principal, TokenValidator
from meta_agent.core.ports.repository import SessionRepository
from meta_agent.infra.security.context import RequestContext
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


class _FakeSessionRepo(SessionRepository):
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], Session] = {}

    async def upsert(self, session: Session) -> None:
        self.rows[(session.tenant_id, session.session_id)] = session

    async def get(self, tenant_id: str, session_id: str) -> Session | None:
        return self.rows.get((tenant_id, session_id))

    async def touch(self, tenant_id: str, session_id: str, last_active_at: datetime) -> None:
        existing = self.rows.get((tenant_id, session_id))
        if existing is not None:
            self.rows[(tenant_id, session_id)] = existing.model_copy(
                update={"last_active_at": last_active_at}
            )


def _make_app(*, task_repo: FakeTaskRepo, session_repo: _FakeSessionRepo) -> Any:
    app = create_app(lifespan=None)
    app.dependency_overrides[get_task_repo] = lambda: task_repo
    app.dependency_overrides[get_session_repo] = lambda: session_repo
    app.dependency_overrides[get_db_pool] = _FakeDbPool
    app.dependency_overrides[get_request_ctx] = _fixed_ctx
    app.dependency_overrides[get_token_validator] = _StubTokenValidator
    return app


def _session() -> Session:
    return Session(
        session_id="s-1",
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        created_at=datetime(2026, 6, 23, tzinfo=UTC),
        last_active_at=datetime(2026, 6, 23, 12, 0, tzinfo=UTC),
    )


def _task(
    *,
    task_id: str,
    user_prompt: str = "hi",
    state: TaskState = TaskState.SUCCEEDED,
    created_at: datetime | None = None,
) -> Task:
    return Task(
        task_id=task_id,
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id=f"tr-{task_id}",
        session_id="s-1",
        idempotency_key=f"idem-{task_id}",
        task_type=TaskType.SYSTEM_CHAT,
        graph_id=None,
        state=state,
        permission_mode=PermissionMode.AUTO,
        budget_policy=BudgetPolicy.NONE,
        input_payload={"user_prompt": user_prompt},
        created_at=created_at or datetime(2026, 6, 23, tzinfo=UTC),
        updated_at=created_at or datetime(2026, 6, 23, tzinfo=UTC),
    )


def _result(task: Task, assistant_message: str) -> TaskResult:
    return TaskResult(
        task_id=task.task_id,
        tenant_id=task.tenant_id,
        trace_id=task.trace_id,
        graph_id="builtin.simple_chat",
        status="succeeded",
        output={"assistant_message": assistant_message},
        error=None,
        node_sequence=1,
        started_at=task.created_at,
        finished_at=task.created_at,
    )


async def test_get_session_returns_row() -> None:
    session_repo = _FakeSessionRepo()
    await session_repo.upsert(_session())
    app = _make_app(task_repo=FakeTaskRepo(), session_repo=session_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/sessions/s-1", headers=_BEARER)
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "s-1"
    assert body["tenant_id"] == _TENANT
    assert body["is_closed"] is False


async def test_get_session_404_when_missing() -> None:
    app = _make_app(task_repo=FakeTaskRepo(), session_repo=_FakeSessionRepo())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/sessions/missing", headers=_BEARER)
    assert response.status_code == 404


async def test_get_session_messages_returns_reconstructed_thread() -> None:
    session_repo = _FakeSessionRepo()
    await session_repo.upsert(_session())
    task_repo = FakeTaskRepo()
    t1 = _task(
        task_id="task-1",
        user_prompt="first",
        created_at=datetime(2026, 6, 23, 12, 0, tzinfo=UTC),
    )
    t2 = _task(
        task_id="task-2",
        user_prompt="second",
        created_at=datetime(2026, 6, 23, 12, 5, tzinfo=UTC),
    )
    await task_repo.upsert(t1)
    await task_repo.upsert(t2)
    task_repo.results[(t1.tenant_id, t1.task_id)] = _result(t1, "first reply")
    task_repo.results[(t2.tenant_id, t2.task_id)] = _result(t2, "second reply")

    app = _make_app(task_repo=task_repo, session_repo=session_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/sessions/s-1/messages", headers=_BEARER)
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "s-1"
    contents = [m["content"] for m in body["messages"]]
    assert contents == ["first", "first reply", "second", "second reply"]
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    # task_id is exposed so the client can link each message to its task
    assert body["messages"][0]["task_id"] == "task-1"
    assert body["messages"][2]["task_id"] == "task-2"


async def test_get_session_messages_404_when_session_missing() -> None:
    app = _make_app(task_repo=FakeTaskRepo(), session_repo=_FakeSessionRepo())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/sessions/missing/messages", headers=_BEARER)
    assert response.status_code == 404


async def test_get_session_messages_skips_failed_and_incomplete_tasks() -> None:
    session_repo = _FakeSessionRepo()
    await session_repo.upsert(_session())
    task_repo = FakeTaskRepo()
    ok = _task(
        task_id="task-ok",
        user_prompt="worked",
        created_at=datetime(2026, 6, 23, 12, 0, tzinfo=UTC),
    )
    failed = _task(
        task_id="task-failed",
        user_prompt="oops",
        state=TaskState.FAILED,
        created_at=datetime(2026, 6, 23, 12, 5, tzinfo=UTC),
    )
    await task_repo.upsert(ok)
    await task_repo.upsert(failed)
    task_repo.results[(ok.tenant_id, ok.task_id)] = _result(ok, "all good")

    app = _make_app(task_repo=task_repo, session_repo=session_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/sessions/s-1/messages", headers=_BEARER)
    body = response.json()
    contents = [m["content"] for m in body["messages"]]
    assert contents == ["worked", "all good"]


async def test_get_session_unauthorized_without_bearer() -> None:
    session_repo = _FakeSessionRepo()
    await session_repo.upsert(_session())
    app = _make_app(task_repo=FakeTaskRepo(), session_repo=session_repo)
    del app.dependency_overrides[get_request_ctx]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/sessions/s-1")
    assert response.status_code == 401
