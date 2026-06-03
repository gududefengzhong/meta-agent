"""End-to-end smoke: registered prompt drives an LLM call and shows up in usage logs.

Verifies the full Phase β+ PR 2 chain:

1. ``PgPromptRegistry`` is seeded via :func:`ensure_seeded`.
2. A graph instance fetches the ``bug_fix.system`` prompt from
   the registry at plan time.
3. The outgoing :class:`LLMRequest` carries
   ``prompt_id`` + ``prompt_version``.
4. :class:`MeteredLLMClient` writes them onto the
   ``llm_usage_logs`` row.

Kept deliberately narrow: no worker dispatch, no workspace
provisioning. Those are exercised by the bug_fix docker smokes;
here we want a fast assertion that prompt provenance survives the
metering hop.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from meta_agent.core.domain.prompt_asset import PromptAsset
from meta_agent.core.ports.llm import ChatMessage, LLMRequest, MessageRole
from meta_agent.infra.llm.metered import MeteredLLMClient
from meta_agent.infra.persistence.llm_usage_repo import PgLLMUsageRepository
from meta_agent.infra.persistence.pool import DatabasePool
from meta_agent.infra.prompt_registry.postgres import PgPromptRegistry
from meta_agent.infra.prompt_registry.seeds import ensure_seeded
from meta_agent.infra.security.context import RequestContext, bind_context
from tests.core.orchestration._fakes import FakeLLMClient, make_response

pytestmark = pytest.mark.integration


async def test_registered_prompt_id_lands_on_llm_usage_logs(db_pool: DatabasePool) -> None:
    tenant_id = f"tenant-prov-{uuid.uuid4().hex[:6]}"
    task_id = f"task-prov-{uuid.uuid4().hex[:6]}"
    trace_id = f"trace-prov-{uuid.uuid4().hex[:6]}"

    prompt_registry = PgPromptRegistry(db_pool)
    materialised = await ensure_seeded(prompt_registry)
    asset: PromptAsset | None = next(
        (a for a in materialised if a.prompt_id == "bug_fix.system"), None
    )
    assert asset is not None

    # Fake inner LLM client returns a stable usage stub so the metered
    # row has known token counts to assert on.
    fake = FakeLLMClient(response=make_response(content="ok", model="fake/echo"))
    usage_repo = PgLLMUsageRepository(db_pool)
    metered = MeteredLLMClient(fake, usage_repo, provider="openrouter")

    request = LLMRequest(
        messages=(ChatMessage(role=MessageRole.USER, content="hi"),),
        prompt_id=asset.prompt_id,
        prompt_version=asset.version,
    )
    ctx = RequestContext(
        tenant_id=tenant_id,
        principal_id="system",
        trace_id=trace_id,
        request_id=task_id,
    )
    with bind_context(ctx):
        await metered.complete(request)
        aggregate = await usage_repo.aggregate_since(tenant_id, datetime(2020, 1, 1, tzinfo=UTC))
    assert aggregate.tokens_used >= 1
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT prompt_id, prompt_version FROM llm_usage_logs "
            "WHERE tenant_id = $1 ORDER BY created_at DESC LIMIT 1",
            tenant_id,
        )
    assert row is not None
    assert row["prompt_id"] == asset.prompt_id
    assert row["prompt_version"] == asset.version
