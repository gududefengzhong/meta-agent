"""Integration tests for :class:`PgPromptRegistry` against real Postgres."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from meta_agent.core.domain.prompt_asset import PromptAsset
from meta_agent.core.ports.prompt_registry import PromptNotFoundError
from meta_agent.infra.persistence.pool import DatabasePool
from meta_agent.infra.prompt_registry.postgres import PgPromptRegistry
from meta_agent.infra.prompt_registry.seeds import BUILTIN_PROMPT_SEEDS, ensure_seeded

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 5, 22, tzinfo=UTC)


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
        created_at=_NOW,
    )


async def test_register_and_fetch_round_trip(db_pool: DatabasePool) -> None:
    registry = PgPromptRegistry(db_pool)
    await registry.register(_asset(prompt_id="rt.alpha", version=1, content="alpha"))
    fetched = await registry.fetch("rt.alpha")
    assert fetched.version == 1
    assert fetched.content == "alpha"


async def test_latest_version_picks_highest(db_pool: DatabasePool) -> None:
    registry = PgPromptRegistry(db_pool)
    await registry.register(_asset(prompt_id="rt.beta", version=1, content="v1"))
    await registry.register(_asset(prompt_id="rt.beta", version=3, content="v3"))
    await registry.register(_asset(prompt_id="rt.beta", version=2, content="v2"))
    fetched = await registry.fetch("rt.beta")
    assert fetched.version == 3
    assert fetched.content == "v3"


async def test_duplicate_triple_rejected(db_pool: DatabasePool) -> None:
    registry = PgPromptRegistry(db_pool)
    await registry.register(_asset(prompt_id="rt.gamma", version=1, content="v1"))
    with pytest.raises(ValueError, match="already registered"):
        await registry.register(_asset(prompt_id="rt.gamma", version=1, content="v1-dup"))


async def test_tenant_row_shadows_global(db_pool: DatabasePool) -> None:
    registry = PgPromptRegistry(db_pool)
    await registry.register(_asset(prompt_id="rt.delta", version=1, content="global"))
    await registry.register(
        _asset(prompt_id="rt.delta", version=1, tenant_id="t-1", content="tenant-1")
    )
    assert (await registry.fetch("rt.delta")).content == "global"
    assert (await registry.fetch("rt.delta", tenant_id="t-1")).content == "tenant-1"
    assert (await registry.fetch("rt.delta", tenant_id="t-2")).content == "global"


async def test_missing_prompt_raises(db_pool: DatabasePool) -> None:
    registry = PgPromptRegistry(db_pool)
    with pytest.raises(PromptNotFoundError):
        await registry.fetch("rt.never-registered")
    assert await registry.fetch_or_none("rt.never-registered") is None


async def test_ensure_seeded_against_postgres_registers_builtin_prompts(
    db_pool: DatabasePool,
) -> None:
    registry = PgPromptRegistry(db_pool)
    materialised = await ensure_seeded(registry, now=_NOW)
    assert len(materialised) == len(BUILTIN_PROMPT_SEEDS)
    for seed, asset in zip(BUILTIN_PROMPT_SEEDS, materialised, strict=True):
        assert asset.prompt_id == seed.prompt_id
        assert asset.content == seed.content
        # A second call must be a no-op: the latest version's hash
        # still matches the seed, so no new row is inserted.
        before = await registry.latest_version(seed.prompt_id)
        await ensure_seeded(registry, seeds=(seed,), now=_NOW)
        after = await registry.latest_version(seed.prompt_id)
        assert before == after, seed.prompt_id


async def test_latest_version_returns_none_for_unknown_prompt(db_pool: DatabasePool) -> None:
    registry = PgPromptRegistry(db_pool)
    assert await registry.latest_version("rt.unknown") is None
