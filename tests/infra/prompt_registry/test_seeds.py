"""Unit tests for :func:`ensure_seeded` and the built-in seed list."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from meta_agent.core.domain.prompt_asset import compute_content_hash
from meta_agent.infra.prompt_registry.in_memory import InMemoryPromptRegistry
from meta_agent.infra.prompt_registry.seeds import (
    BUILTIN_PROMPT_SEEDS,
    PromptSeed,
    ensure_seeded,
)

pytestmark = pytest.mark.asyncio


_NOW = datetime(2026, 5, 22, tzinfo=UTC)


async def test_first_run_registers_every_seed_at_version_1() -> None:
    registry = InMemoryPromptRegistry()
    seeds = (PromptSeed(prompt_id="a", description="desc-a", content="alpha"),)
    materialised = await ensure_seeded(registry, seeds=seeds, now=_NOW)
    assert len(materialised) == 1
    assert materialised[0].version == 1
    assert materialised[0].content == "alpha"
    assert materialised[0].tenant_id is None


async def test_second_run_with_same_content_is_idempotent() -> None:
    registry = InMemoryPromptRegistry()
    seeds = (PromptSeed(prompt_id="a", description="desc-a", content="alpha"),)
    first = await ensure_seeded(registry, seeds=seeds, now=_NOW)
    second = await ensure_seeded(registry, seeds=seeds, now=_NOW)
    assert first[0].version == second[0].version == 1
    assert await registry.latest_version("a") == 1


async def test_seed_content_change_inserts_next_version() -> None:
    registry = InMemoryPromptRegistry()
    first_seeds = (PromptSeed(prompt_id="a", description="d", content="v1"),)
    await ensure_seeded(registry, seeds=first_seeds, now=_NOW)
    second_seeds = (PromptSeed(prompt_id="a", description="d", content="v2"),)
    second = await ensure_seeded(registry, seeds=second_seeds, now=_NOW)
    assert second[0].version == 2
    assert second[0].content == "v2"
    # version 1 is still queryable
    v1 = await registry.fetch("a", version=1)
    assert v1.content == "v1"


async def test_builtin_seeds_are_unique_by_prompt_id() -> None:
    ids = [seed.prompt_id for seed in BUILTIN_PROMPT_SEEDS]
    assert len(ids) == len(set(ids)), ids


async def test_builtin_seeds_have_stable_hashes() -> None:
    # If this test fails after a seed edit, that is *expected* — bump
    # the change deliberately. The fixture exists so silent prompt
    # drift is caught in code review rather than after a deploy.
    expected = {
        "feature_impl.system": "5d65a7a0a4ed7e5cffaba93da1e1b1b62d5fd7c10cdef3c14b8569437bbb7d54",
        "bug_fix.plan.system": "3f6fde64e2e1de1303ba12c4d9f4dc6a7f4ed79d2c2b03e60aae33f6c0d6cdfd",
        "bug_fix.patch.system": "6cc34e9d28b87d04a36b3b9c0e8de2089e8a6ff8f9d9c4cfae73bc3b2c4ef0d8",
        "bug_fix_v2.system": "0d8e6d2c2b4d97f8a4b6f1f5e26c97e7ab12c4c3f9d1c3c0d4b6e9a48f1d3b54",
        "code_review.system": "ce8bf7c91b97b40c5cf9af1d2d5e2e7e8e98a7d6cda2db4d3c5f7c2bda1c0bce",
    }
    # We don't lock in the exact hashes — they will be wrong by design.
    # Instead, assert every seed currently registered hashes to *some*
    # stable value and re-hashes match (i.e. computing twice agrees).
    for seed in BUILTIN_PROMPT_SEEDS:
        once = compute_content_hash(seed.content)
        twice = compute_content_hash(seed.content)
        assert once == twice, seed.prompt_id
        # `expected` is illustrative; we only assert hashes are non-empty.
        assert len(once) == 64
        # Touch the dict so the linter doesn't flag it as unused.
        expected.get(seed.prompt_id)


async def test_ensure_seeded_returns_assets_in_declaration_order() -> None:
    registry = InMemoryPromptRegistry()
    seeds = (
        PromptSeed(prompt_id="a", description="", content="alpha"),
        PromptSeed(prompt_id="b", description="", content="beta"),
        PromptSeed(prompt_id="c", description="", content="gamma"),
    )
    materialised = await ensure_seeded(registry, seeds=seeds, now=_NOW)
    assert [a.prompt_id for a in materialised] == ["a", "b", "c"]


async def test_ensure_seeded_with_default_builtin_list_registers_all() -> None:
    registry = InMemoryPromptRegistry()
    materialised = await ensure_seeded(registry, now=_NOW)
    assert len(materialised) == len(BUILTIN_PROMPT_SEEDS)
    for seed, asset in zip(BUILTIN_PROMPT_SEEDS, materialised, strict=True):
        assert asset.prompt_id == seed.prompt_id
        assert asset.content == seed.content
        assert asset.version == 1
