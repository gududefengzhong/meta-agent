"""Unit tests for :class:`CachingPromptRegistry`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from meta_agent.core.domain.prompt_asset import PromptAsset
from meta_agent.core.ports.prompt_registry import PromptNotFoundError
from meta_agent.infra.prompt_registry.caching import CachingPromptRegistry
from meta_agent.infra.prompt_registry.in_memory import InMemoryPromptRegistry

pytestmark = pytest.mark.asyncio


class _Clock:
    """Monotonic clock the tests advance by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _asset(version: int = 1, content: str = "hello") -> PromptAsset:
    return PromptAsset(
        prompt_id="test.system",
        version=version,
        tenant_id=None,
        content=content,
        description=None,
        created_at=datetime(2026, 5, 22, tzinfo=UTC),
    )


async def test_first_fetch_passes_through_to_inner() -> None:
    clock = _Clock()
    inner = InMemoryPromptRegistry()
    await inner.register(_asset())
    caching = CachingPromptRegistry(inner, ttl_seconds=60, monotonic=clock)
    assert (await caching.fetch("test.system")).content == "hello"


async def test_repeated_fetch_within_ttl_does_not_hit_inner() -> None:
    clock = _Clock()
    inner = _CountingRegistry()
    await inner.register(_asset())
    caching = CachingPromptRegistry(inner, ttl_seconds=60, monotonic=clock)
    await caching.fetch("test.system")
    await caching.fetch("test.system")
    await caching.fetch("test.system")
    assert inner.fetch_calls == 1


async def test_fetch_after_ttl_expiry_refetches_inner() -> None:
    clock = _Clock()
    inner = _CountingRegistry()
    await inner.register(_asset())
    caching = CachingPromptRegistry(inner, ttl_seconds=60, monotonic=clock)
    await caching.fetch("test.system")
    clock.now += 61.0
    await caching.fetch("test.system")
    assert inner.fetch_calls == 2


async def test_negative_result_cached_briefly() -> None:
    clock = _Clock()
    inner = _CountingRegistry()
    caching = CachingPromptRegistry(
        inner, ttl_seconds=60, negative_ttl_seconds=5, monotonic=clock
    )
    assert await caching.fetch_or_none("nope") is None
    assert await caching.fetch_or_none("nope") is None
    assert inner.fetch_calls == 1
    # After the short negative TTL we hit inner again.
    clock.now += 6.0
    assert await caching.fetch_or_none("nope") is None
    assert inner.fetch_calls == 2


async def test_register_invalidates_cached_entries_for_that_prompt_id() -> None:
    clock = _Clock()
    inner = InMemoryPromptRegistry()
    await inner.register(_asset(version=1, content="v1"))
    caching = CachingPromptRegistry(inner, ttl_seconds=300, monotonic=clock)

    assert (await caching.fetch("test.system")).version == 1

    # Register version 2 through the cache (so it can invalidate).
    await caching.register(_asset(version=2, content="v2"))
    # Even within TTL, the next read must reflect v2.
    refreshed = await caching.fetch("test.system")
    assert refreshed.version == 2
    assert refreshed.content == "v2"


async def test_fetch_raises_when_inner_has_nothing() -> None:
    clock = _Clock()
    inner = InMemoryPromptRegistry()
    caching = CachingPromptRegistry(inner, ttl_seconds=60, monotonic=clock)
    with pytest.raises(PromptNotFoundError):
        await caching.fetch("missing")


async def test_caching_registry_rejects_non_positive_ttl() -> None:
    inner = InMemoryPromptRegistry()
    with pytest.raises(ValueError, match="ttl_seconds must be positive"):
        CachingPromptRegistry(inner, ttl_seconds=0)
    with pytest.raises(ValueError, match="negative_ttl_seconds must be positive"):
        CachingPromptRegistry(inner, ttl_seconds=60, negative_ttl_seconds=0)


class _CountingRegistry(InMemoryPromptRegistry):
    """Wraps InMemoryPromptRegistry and counts ``fetch_or_none`` calls."""

    def __init__(self) -> None:
        super().__init__()
        self.fetch_calls = 0

    async def fetch_or_none(
        self,
        prompt_id: str,
        *,
        version: int | None = None,
        tenant_id: str | None = None,
    ) -> PromptAsset | None:
        self.fetch_calls += 1
        return await super().fetch_or_none(
            prompt_id, version=version, tenant_id=tenant_id
        )
