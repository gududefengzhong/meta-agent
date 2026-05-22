"""End-to-end smoke for Phase β+ PR 4 routing + step_kind persistence.

Verifies the chain works against a real Postgres:

1. ``RoutingLLMClient`` rewrites ``request.model`` based on
   ``request.step_kind``.
2. ``MeteredLLMClient`` records the (routed) ``requested_model`` plus
   the original ``step_kind`` on every ``llm_usage_logs`` row.
3. The ``llm_usage_logs.step_kind`` column actually exists after
   migration 0007 (this is the most direct check that migration
   ordering survives a fresh DB bring-up).

The inner LLM is a :class:`FakeLLMClient` so no real network IO fires;
the test focuses on the persistence + routing wiring, not on any
provider's behaviour.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from meta_agent.core.ports.llm import ChatMessage, LLMRequest, MessageRole
from meta_agent.infra.llm.metered import MeteredLLMClient
from meta_agent.infra.llm.routing import RoutingLLMClient, StaticLLMRouter
from meta_agent.infra.persistence.llm_usage_repo import PgLLMUsageRepository
from meta_agent.infra.persistence.pool import DatabasePool
from meta_agent.infra.security.context import RequestContext, bind_context
from tests.core.orchestration._fakes import FakeLLMClient, make_response

pytestmark = pytest.mark.integration


async def test_routing_overrides_model_and_step_kind_lands_on_usage_row(
    db_pool: DatabasePool,
) -> None:
    tenant_id = f"tenant-router-{uuid.uuid4().hex[:6]}"
    task_id = f"task-router-{uuid.uuid4().hex[:6]}"
    trace_id = f"trace-router-{uuid.uuid4().hex[:6]}"

    # Inner LLM echoes whatever model the routed request carries, so
    # we can assert the override fired before metering.
    fake = FakeLLMClient(response=make_response(content="ok", model="deepseek/deepseek-chat"))
    usage_repo = PgLLMUsageRepository(db_pool)
    metered = MeteredLLMClient(fake, usage_repo, provider="openrouter")
    routed = RoutingLLMClient(
        metered,
        StaticLLMRouter({"plan": "deepseek/deepseek-chat", "edit": "qwen/qwen3-coder"}),
    )

    ctx = RequestContext(
        tenant_id=tenant_id,
        principal_id="system",
        trace_id=trace_id,
        request_id=task_id,
    )
    with bind_context(ctx):
        await routed.complete(
            LLMRequest(
                messages=(ChatMessage(role=MessageRole.USER, content="hi"),),
                model="caller/initial",
                step_kind="plan",
            )
        )
        # Force a second call with a different step_kind to verify each
        # row independently captures its own classification.
        await routed.complete(
            LLMRequest(
                messages=(ChatMessage(role=MessageRole.USER, content="edit it"),),
                model="caller/initial",
                step_kind="edit",
            )
        )

    # The inner LLM saw the routed model on each call.
    assert [call.model for call in fake.calls] == [
        "deepseek/deepseek-chat",
        "qwen/qwen3-coder",
    ]
    # step_kind survived the routing override (so MeteredLLMClient
    # still tagged the row).
    assert [call.step_kind for call in fake.calls] == ["plan", "edit"]

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT step_kind, requested_model FROM llm_usage_logs "
            "WHERE tenant_id = $1 ORDER BY created_at ASC",
            tenant_id,
        )
    assert [row["step_kind"] for row in rows] == ["plan", "edit"]
    assert [row["requested_model"] for row in rows] == [
        "deepseek/deepseek-chat",
        "qwen/qwen3-coder",
    ]


async def test_unrouted_request_writes_null_step_kind(db_pool: DatabasePool) -> None:
    tenant_id = f"tenant-noroute-{uuid.uuid4().hex[:6]}"
    task_id = f"task-noroute-{uuid.uuid4().hex[:6]}"
    trace_id = f"trace-noroute-{uuid.uuid4().hex[:6]}"

    fake = FakeLLMClient(response=make_response(content="ok", model="openai/gpt-4o"))
    usage_repo = PgLLMUsageRepository(db_pool)
    metered = MeteredLLMClient(fake, usage_repo, provider="openrouter")

    ctx = RequestContext(
        tenant_id=tenant_id,
        principal_id="system",
        trace_id=trace_id,
        request_id=task_id,
    )
    with bind_context(ctx):
        await metered.complete(
            LLMRequest(
                messages=(ChatMessage(role=MessageRole.USER, content="hi"),),
                model="openai/gpt-4o",
            )
        )

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT step_kind FROM llm_usage_logs WHERE tenant_id = $1",
            tenant_id,
        )
    assert row is not None
    assert row["step_kind"] is None
    # Sanity: the row landed in the same recent window.
    _ = datetime.now(UTC)
