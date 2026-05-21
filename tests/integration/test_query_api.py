"""Integration coverage for the Phase α query API.

Runs the real FastAPI router against a migrated Postgres database and a
real bearer-token validator. This closes the gap between the existing
offline API unit tests and the α exit condition that audit / usage
queries remain tenant-safe in an integrated environment.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient

from meta_agent.api.app import create_app
from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.domain.llm_usage import LLMUsageRecord, LLMUsageStatus
from meta_agent.infra.auth.env_validator import EnvTokenValidator
from meta_agent.infra.persistence.audit_repo import PgAuditRepository
from meta_agent.infra.persistence.llm_usage_repo import PgLLMUsageRepository
from meta_agent.infra.persistence.pool import DatabasePool
from meta_agent.infra.security.context import RequestContext, bind_context


def _ctx(tenant: str, principal: str, trace: str, request: str) -> RequestContext:
    return RequestContext(
        tenant_id=tenant,
        principal_id=principal,
        trace_id=trace,
        request_id=request,
    )


async def test_query_api_reads_real_audits_and_usages_with_bearer_auth(
    db_pool: DatabasePool,
) -> None:
    audit_repo = PgAuditRepository(db_pool)
    usage_repo = PgLLMUsageRepository(db_pool)
    now = datetime.now(UTC).replace(microsecond=0)

    with bind_context(_ctx("tenant-int", "user-int", "trace-int-1", "req-int-1")):
        await audit_repo.append(
            AuditEvent(
                event_id="ae-tenant-int-1",
                tenant_id="tenant-int",
                principal_id="user-int",
                task_id="task-1",
                trace_id="trace-int-1",
                action="task.succeeded",
                payload={"sequence": 3},
                occurred_at=now,
            )
        )
        await usage_repo.record(
            LLMUsageRecord(
                record_id="llmu-tenant-int-1",
                tenant_id="tenant-int",
                trace_id="trace-int-1",
                request_id="req-int-1",
                principal_id="user-int",
                task_id="task-1",
                provider="openrouter",
                model="openai/gpt-4o-mini",
                requested_model="openai/gpt-4o-mini",
                prompt_tokens=11,
                completion_tokens=13,
                total_tokens=24,
                finish_reason="stop",
                provider_response_id="resp-int-1",
                cost_usd_micros=240,
                latency_ms=120,
                status=LLMUsageStatus.OK,
                created_at=now,
            )
        )

    with bind_context(_ctx("tenant-other", "user-other", "trace-other-1", "req-other-1")):
        await audit_repo.append(
            AuditEvent(
                event_id="ae-tenant-other-1",
                tenant_id="tenant-other",
                principal_id="user-other",
                task_id="task-2",
                trace_id="trace-other-1",
                action="task.failed",
                payload={"sequence": 7},
                occurred_at=now - timedelta(minutes=1),
            )
        )
        await usage_repo.record(
            LLMUsageRecord(
                record_id="llmu-tenant-other-1",
                tenant_id="tenant-other",
                trace_id="trace-other-1",
                request_id="req-other-1",
                principal_id="user-other",
                task_id="task-2",
                provider="openrouter",
                model="openai/gpt-4o-mini",
                requested_model="openai/gpt-4o-mini",
                prompt_tokens=5,
                completion_tokens=7,
                total_tokens=12,
                finish_reason="stop",
                provider_response_id="resp-other-1",
                cost_usd_micros=120,
                latency_ms=80,
                status=LLMUsageStatus.OK,
                created_at=now - timedelta(minutes=1),
            )
        )

    app = create_app(lifespan=None)
    app.state.db_pool = db_pool
    app.state.token_validator = EnvTokenValidator("int-token:tenant-int:user-int")
    transport = ASGITransport(app=app)
    headers = {"Authorization": "Bearer int-token"}

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        audits = await client.get("/v1/audits", headers=headers, params={"limit": 10})
        usages = await client.get("/v1/usages", headers=headers, params={"limit": 10})

    assert audits.status_code == 200
    audits_payload = audits.json()
    assert [item["event_id"] for item in audits_payload["items"]] == ["ae-tenant-int-1"]
    assert audits_payload["items"][0]["tenant_id"] == "tenant-int"

    assert usages.status_code == 200
    usages_payload = usages.json()
    assert [item["record_id"] for item in usages_payload["items"]] == ["llmu-tenant-int-1"]
    assert usages_payload["items"][0]["tenant_id"] == "tenant-int"
    assert usages_payload["items"][0]["total_tokens"] == 24
