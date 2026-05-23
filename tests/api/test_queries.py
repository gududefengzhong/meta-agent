"""Unit tests for the query API (``/v1/audits``, ``/v1/usages``).

The router is exercised end-to-end through FastAPI / httpx, but every
collaborator (the two repositories, the request-context dependency) is
overridden so the tests are fully offline and deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import ASGITransport, AsyncClient

from meta_agent.api.app import create_app
from meta_agent.api.cursor import decode_cursor, encode_cursor
from meta_agent.api.deps import (
    get_audit_repo,
    get_llm_usage_repo,
    get_request_ctx,
)
from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.domain.llm_usage import LLMUsageRecord, LLMUsageStatus
from meta_agent.core.ports.llm_usage import (
    LLMUsageFilter,
    LLMUsageRepository,
    UsageAggregate,
    UsageGroupBy,
)
from meta_agent.core.ports.repository import AuditFilter, AuditRepository
from meta_agent.infra.security.context import RequestContext

_TENANT = "tenant-test"
_PRINCIPAL = "user-test"


def _fixed_ctx() -> RequestContext:
    return RequestContext(
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id="trace-fixed",
        request_id="req-fixed",
    )


class _FakeAuditRepo(AuditRepository):
    """In-memory fake; records the last filter the router constructed."""

    def __init__(self, events: list[AuditEvent]) -> None:
        self._events = events
        self.last_filter: AuditFilter | None = None

    async def append(self, event: AuditEvent) -> None:  # pragma: no cover - unused
        raise AssertionError

    async def list_recent(
        self, tenant_id: str, limit: int = 100
    ) -> list[AuditEvent]:  # pragma: no cover
        raise AssertionError

    async def list_filtered(self, tenant_id: str, filt: AuditFilter) -> list[AuditEvent]:
        assert tenant_id == _TENANT
        self.last_filter = filt
        # Honour ``limit`` so the cursor-emission branch can be exercised.
        return list(self._events[: filt.limit])


class _FakeUsageRepo(LLMUsageRepository):
    """In-memory fake covering only the read paths used by the router."""

    def __init__(
        self,
        records: list[LLMUsageRecord] | None = None,
        buckets: list[UsageAggregate] | None = None,
    ) -> None:
        self._records = records or []
        self._buckets = buckets or []
        self.last_filter: LLMUsageFilter | None = None
        self.last_group_by: UsageGroupBy | None = None
        self.last_window: tuple[datetime, datetime] | None = None

    async def record(self, record: LLMUsageRecord) -> None:  # pragma: no cover
        raise AssertionError

    async def list_for_task(
        self, tenant_id: str, task_id: str
    ) -> list[LLMUsageRecord]:  # pragma: no cover
        raise AssertionError

    async def aggregate_since(self, tenant_id: str, since: datetime) -> Any:  # pragma: no cover
        raise AssertionError

    async def list_filtered(self, tenant_id: str, filt: LLMUsageFilter) -> list[LLMUsageRecord]:
        assert tenant_id == _TENANT
        self.last_filter = filt
        return list(self._records[: filt.limit])

    async def aggregate_grouped(
        self,
        tenant_id: str,
        since: datetime,
        until: datetime,
        group_by: UsageGroupBy,
    ) -> list[UsageAggregate]:
        assert tenant_id == _TENANT
        self.last_group_by = group_by
        self.last_window = (since, until)
        return list(self._buckets)

    async def aggregate_for_task(
        self,
        tenant_id: str,
        task_id: str,
        group_by: UsageGroupBy,
    ) -> list[UsageAggregate]:  # pragma: no cover - not exercised by query API
        raise AssertionError


def _make_app(
    audit_repo: _FakeAuditRepo | None = None,
    usage_repo: _FakeUsageRepo | None = None,
) -> Any:
    app = create_app(lifespan=None)
    app.dependency_overrides[get_request_ctx] = _fixed_ctx
    if audit_repo is not None:
        app.dependency_overrides[get_audit_repo] = lambda: audit_repo
    if usage_repo is not None:
        app.dependency_overrides[get_llm_usage_repo] = lambda: usage_repo
    return app


def _make_audit_event(idx: int, occurred_at: datetime) -> AuditEvent:
    return AuditEvent(
        event_id=f"evt-{idx}",
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        session_id=None,
        task_id=f"task-{idx}",
        trace_id=f"trace-{idx}",
        action="test.action",
        payload={"i": idx},
        occurred_at=occurred_at,
    )


def _make_usage_record(idx: int, created_at: datetime) -> LLMUsageRecord:
    return LLMUsageRecord(
        record_id=f"rec-{idx}",
        tenant_id=_TENANT,
        trace_id=f"trace-{idx}",
        provider="openrouter",
        model="gpt-test",
        total_tokens=10 + idx,
        latency_ms=42,
        status=LLMUsageStatus.OK,
        created_at=created_at,
    )


# ── GET /v1/audits ────────────────────────────────────────────────────────────


async def test_list_audits_returns_page_with_cursor_when_full() -> None:
    """A full page (``len(items) == limit``) emits a ``next_cursor``."""
    t0 = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    events = [_make_audit_event(i, t0 - timedelta(minutes=i)) for i in range(3)]
    repo = _FakeAuditRepo(events)
    app = _make_app(audit_repo=repo)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/audits", params={"limit": 3})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 3
    assert body["items"][0]["event_id"] == "evt-0"
    # next_cursor present and decodes to the last row's (occurred_at, event_id).
    assert body["next_cursor"] is not None
    ts, ident = decode_cursor(body["next_cursor"])
    assert ident == "evt-2"
    assert ts == events[-1].occurred_at


async def test_list_audits_short_page_omits_cursor() -> None:
    """A short page (``len(items) < limit``) returns ``next_cursor=null``."""
    t0 = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    repo = _FakeAuditRepo([_make_audit_event(0, t0)])
    app = _make_app(audit_repo=repo)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/audits", params={"limit": 50})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["next_cursor"] is None


async def test_list_audits_threads_filters_into_repository() -> None:
    """Optional query params reach ``AuditFilter`` unchanged."""
    repo = _FakeAuditRepo([])
    app = _make_app(audit_repo=repo)
    since = datetime(2026, 5, 1, tzinfo=UTC).isoformat()
    until = datetime(2026, 5, 2, tzinfo=UTC).isoformat()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/v1/audits",
            params={
                "since": since,
                "until": until,
                "action": "task.submitted",
                "task_id": "t-9",
                "limit": 25,
            },
        )

    assert resp.status_code == 200
    assert repo.last_filter is not None
    assert repo.last_filter.action == "task.submitted"
    assert repo.last_filter.task_id == "t-9"
    assert repo.last_filter.limit == 25
    assert repo.last_filter.since.isoformat() == since
    assert repo.last_filter.until.isoformat() == until


async def test_list_audits_decodes_cursor() -> None:
    """A valid ``cursor`` query param is decoded into ``filter.before``."""
    repo = _FakeAuditRepo([])
    app = _make_app(audit_repo=repo)
    cursor_at = datetime(2026, 5, 15, 11, 30, tzinfo=UTC)
    cursor = encode_cursor(cursor_at, "evt-prev")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/audits", params={"cursor": cursor})

    assert resp.status_code == 200
    assert repo.last_filter is not None
    assert repo.last_filter.before == (cursor_at, "evt-prev")


async def test_list_audits_invalid_cursor_returns_400() -> None:
    repo = _FakeAuditRepo([])
    app = _make_app(audit_repo=repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/audits", params={"cursor": "not-base64!"})
    assert resp.status_code == 400


async def test_list_audits_rejects_inverted_window() -> None:
    repo = _FakeAuditRepo([])
    app = _make_app(audit_repo=repo)
    since = datetime(2026, 5, 2, tzinfo=UTC).isoformat()
    until = datetime(2026, 5, 1, tzinfo=UTC).isoformat()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/audits", params={"since": since, "until": until})
    assert resp.status_code == 400


async def test_list_audits_limit_out_of_range() -> None:
    repo = _FakeAuditRepo([])
    app = _make_app(audit_repo=repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/audits", params={"limit": 0})
    assert resp.status_code == 422


# ── GET /v1/usages ────────────────────────────────────────────────────────────


async def test_list_usages_returns_page_with_cursor() -> None:
    t0 = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    records = [_make_usage_record(i, t0 - timedelta(minutes=i)) for i in range(2)]
    repo = _FakeUsageRepo(records=records)
    app = _make_app(usage_repo=repo)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/usages", params={"limit": 2})

    assert resp.status_code == 200
    body = resp.json()
    assert [it["record_id"] for it in body["items"]] == ["rec-0", "rec-1"]
    ts, ident = decode_cursor(body["next_cursor"])
    assert ident == "rec-1"
    assert ts == records[-1].created_at


async def test_list_usages_threads_status_filter() -> None:
    repo = _FakeUsageRepo()
    app = _make_app(usage_repo=repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/v1/usages",
            params={"status": "error", "model": "gpt-x", "task_id": "t-1"},
        )
    assert resp.status_code == 200
    assert repo.last_filter is not None
    assert repo.last_filter.status is LLMUsageStatus.ERROR
    assert repo.last_filter.model == "gpt-x"
    assert repo.last_filter.task_id == "t-1"


async def test_list_usages_rejects_bad_status() -> None:
    repo = _FakeUsageRepo()
    app = _make_app(usage_repo=repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/usages", params={"status": "bogus"})
    assert resp.status_code == 422


# ── GET /v1/usages/aggregate ─────────────────────────────────────────────────


async def test_aggregate_usages_returns_buckets() -> None:
    buckets = [
        UsageAggregate(key="gpt-a", tokens=100, cost_usd_micros=200, calls=5),
        UsageAggregate(key="gpt-b", tokens=50, cost_usd_micros=80, calls=2),
    ]
    repo = _FakeUsageRepo(buckets=buckets)
    app = _make_app(usage_repo=repo)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/usages/aggregate", params={"group_by": "model"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["group_by"] == "model"
    assert [b["key"] for b in body["items"]] == ["gpt-a", "gpt-b"]
    assert body["items"][0]["tokens"] == 100
    assert repo.last_group_by is UsageGroupBy.MODEL
    assert repo.last_window is not None


async def test_aggregate_usages_requires_group_by() -> None:
    repo = _FakeUsageRepo()
    app = _make_app(usage_repo=repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/usages/aggregate")
    assert resp.status_code == 422


async def test_aggregate_usages_rejects_bad_group_by() -> None:
    repo = _FakeUsageRepo()
    app = _make_app(usage_repo=repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/usages/aggregate", params={"group_by": "bogus"})
    assert resp.status_code == 422


async def test_aggregate_usages_default_window_is_seven_days() -> None:
    repo = _FakeUsageRepo()
    app = _make_app(usage_repo=repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/usages/aggregate", params={"group_by": "day"})
    assert resp.status_code == 200
    assert repo.last_window is not None
    since, until = repo.last_window
    # Sanity check: the window width should be ~7 days.
    width = until - since
    assert timedelta(days=6, hours=23) < width <= timedelta(days=7, seconds=1)
