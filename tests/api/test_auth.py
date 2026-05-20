"""Bearer-token authentication tests for the public task API.

These tests exercise the *real* :func:`get_request_ctx` against a stub
:class:`TokenValidator`. They guard the security contract that:

* Missing or malformed ``Authorization`` headers return 401.
* Tenancy comes from the validated :class:`Principal` — not from any
  ``X-Tenant-Id`` / ``X-Principal-Id`` headers the client may send.
* Backend faults are surfaced as 503, not 401, so on-call alerts can
  distinguish auth-store outages from genuine credential errors.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from meta_agent.api.app import create_app
from meta_agent.api.deps import (
    get_db_pool,
    get_outbox_repo,
    get_task_repo,
    get_task_topic,
    get_token_validator,
)
from meta_agent.core.ports.auth import AuthBackendError, Principal, TokenValidator
from tests.worker._fakes import FakeOutboxRepo, FakeTaskRepo


class _StubTokenValidator(TokenValidator):
    """One token → tenant-1/user-1; everything else is unknown."""

    async def validate(self, token: str) -> Principal | None:
        if token == "tok-good":
            return Principal(tenant_id="tenant-1", principal_id="user-1")
        return None


class _FailingTokenValidator(TokenValidator):
    async def validate(self, token: str) -> Principal | None:
        raise AuthBackendError("simulated outage")


class _FakeDbPool:
    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[object]:
        yield object()


def _make_app(validator: TokenValidator) -> object:
    app = create_app(lifespan=None)
    app.dependency_overrides[get_task_repo] = lambda: FakeTaskRepo()
    app.dependency_overrides[get_outbox_repo] = lambda: FakeOutboxRepo()
    app.dependency_overrides[get_db_pool] = _FakeDbPool
    app.dependency_overrides[get_task_topic] = lambda: "task.commands"
    app.dependency_overrides[get_token_validator] = lambda: validator
    return app


async def _post(app: object, headers: dict[str, str]) -> int:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:  # type: ignore[arg-type]
        resp = await client.post(
            "/v1/tasks",
            json={"task_type": "system_echo", "input_payload": {"message": "x"}},
            headers=headers,
        )
    return resp.status_code


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer   "},
        {"Authorization": "bearer tok-good extra"},
    ],
)
async def test_missing_or_malformed_authorization_returns_401(headers: dict[str, str]) -> None:
    app = _make_app(_StubTokenValidator())
    assert await _post(app, headers) == 401


async def test_unknown_token_returns_401() -> None:
    app = _make_app(_StubTokenValidator())
    assert await _post(app, {"Authorization": "Bearer tok-bad"}) == 401


async def test_valid_token_authenticates_and_creates_task() -> None:
    app = _make_app(_StubTokenValidator())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:  # type: ignore[arg-type]
        resp = await client.post(
            "/v1/tasks",
            json={"task_type": "system_echo", "input_payload": {"message": "hi"}},
            headers={"Authorization": "Bearer tok-good"},
        )
    assert resp.status_code == 201
    body = resp.json()
    # Tenant/principal MUST come from the validated principal, not from headers.
    assert body["tenant_id"] == "tenant-1"


async def test_xtenant_header_cannot_spoof_tenant() -> None:
    app = _make_app(_StubTokenValidator())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:  # type: ignore[arg-type]
        resp = await client.post(
            "/v1/tasks",
            json={"task_type": "system_echo", "input_payload": {"message": "hi"}},
            headers={
                "Authorization": "Bearer tok-good",
                "X-Tenant-Id": "tenant-evil",
                "X-Principal-Id": "user-evil",
            },
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["tenant_id"] == "tenant-1"


async def test_backend_fault_returns_503() -> None:
    app = _make_app(_FailingTokenValidator())
    assert await _post(app, {"Authorization": "Bearer tok-good"}) == 503


async def test_case_insensitive_bearer_scheme_accepted() -> None:
    app = _make_app(_StubTokenValidator())
    assert await _post(app, {"Authorization": "bearer tok-good"}) == 201
    app = _make_app(_StubTokenValidator())
    assert await _post(app, {"Authorization": "BEARER tok-good"}) == 201
