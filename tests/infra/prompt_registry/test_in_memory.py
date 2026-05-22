"""Unit tests for :class:`InMemoryPromptRegistry`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from meta_agent.core.domain.prompt_asset import PromptAsset
from meta_agent.core.ports.prompt_registry import PromptNotFoundError
from meta_agent.infra.prompt_registry.in_memory import InMemoryPromptRegistry

pytestmark = pytest.mark.asyncio


def _asset(
    prompt_id: str = "test.system",
    version: int = 1,
    *,
    tenant_id: str | None = None,
    content: str = "hello",
) -> PromptAsset:
    return PromptAsset(
        prompt_id=prompt_id,
        version=version,
        tenant_id=tenant_id,
        content=content,
        description=None,
        created_at=datetime(2026, 5, 22, tzinfo=UTC),
    )


async def test_register_and_fetch_global_prompt() -> None:
    registry = InMemoryPromptRegistry()
    asset = _asset()
    await registry.register(asset)
    fetched = await registry.fetch("test.system")
    assert fetched == asset


async def test_fetch_latest_picks_highest_version() -> None:
    registry = InMemoryPromptRegistry()
    await registry.register(_asset(version=1, content="v1"))
    await registry.register(_asset(version=3, content="v3"))
    await registry.register(_asset(version=2, content="v2"))
    fetched = await registry.fetch("test.system")
    assert fetched.version == 3
    assert fetched.content == "v3"


async def test_fetch_specific_version() -> None:
    registry = InMemoryPromptRegistry()
    await registry.register(_asset(version=1, content="v1"))
    await registry.register(_asset(version=2, content="v2"))
    fetched = await registry.fetch("test.system", version=1)
    assert fetched.content == "v1"


async def test_fetch_missing_raises_prompt_not_found() -> None:
    registry = InMemoryPromptRegistry()
    with pytest.raises(PromptNotFoundError) as excinfo:
        await registry.fetch("missing.prompt")
    assert excinfo.value.prompt_id == "missing.prompt"
    assert excinfo.value.version is None


async def test_fetch_or_none_returns_none_when_absent() -> None:
    registry = InMemoryPromptRegistry()
    assert await registry.fetch_or_none("missing.prompt") is None
    assert await registry.fetch_or_none("missing.prompt", version=5) is None


async def test_tenant_row_shadows_global_row() -> None:
    registry = InMemoryPromptRegistry()
    await registry.register(_asset(content="global"))
    await registry.register(_asset(tenant_id="t-1", content="tenant-override"))
    assert (await registry.fetch("test.system")).content == "global"
    assert (await registry.fetch("test.system", tenant_id="t-1")).content == "tenant-override"
    assert (await registry.fetch("test.system", tenant_id="t-2")).content == "global"


async def test_tenant_query_falls_back_to_global_when_tenant_row_absent() -> None:
    registry = InMemoryPromptRegistry()
    await registry.register(_asset(content="global only"))
    fetched = await registry.fetch("test.system", tenant_id="t-1")
    assert fetched.content == "global only"
    assert fetched.tenant_id is None


async def test_tenant_version_pin_does_not_fall_back_silently_to_other_scope() -> None:
    # tenant-scoped version=3 exists but global only has version=1. Asking for
    # (prompt, version=3, tenant=t-1) should find the tenant row; asking for
    # (prompt, version=3, tenant=t-2) should fall back to global which has
    # no version=3 — so result is None.
    registry = InMemoryPromptRegistry()
    await registry.register(_asset(version=1, content="global"))
    await registry.register(_asset(version=3, tenant_id="t-1", content="tenant"))
    tenant_hit = await registry.fetch_or_none("test.system", version=3, tenant_id="t-1")
    assert tenant_hit is not None
    assert tenant_hit.content == "tenant"
    assert await registry.fetch_or_none("test.system", version=3, tenant_id="t-2") is None


async def test_register_rejects_duplicate_triple() -> None:
    registry = InMemoryPromptRegistry()
    await registry.register(_asset(version=1))
    with pytest.raises(ValueError, match="already registered"):
        await registry.register(_asset(version=1))


async def test_register_allows_same_version_for_different_tenants() -> None:
    registry = InMemoryPromptRegistry()
    await registry.register(_asset(version=1, content="global"))
    await registry.register(_asset(version=1, tenant_id="t-1", content="tenant"))
    # No exception means the partial-scope uniqueness held.


async def test_latest_version_returns_none_for_unknown_prompt() -> None:
    registry = InMemoryPromptRegistry()
    assert await registry.latest_version("nope") is None


async def test_latest_version_per_scope() -> None:
    registry = InMemoryPromptRegistry()
    await registry.register(_asset(version=1))
    await registry.register(_asset(version=2))
    await registry.register(_asset(version=4, tenant_id="t-1"))
    assert await registry.latest_version("test.system") == 2
    assert await registry.latest_version("test.system", tenant_id="t-1") == 4
    assert await registry.latest_version("test.system", tenant_id="t-2") is None
