"""TTL-bounded read-through cache for :class:`PromptRegistry`.

Wraps an inner registry (typically :class:`PgPromptRegistry`) so each
worker process hits Postgres at most once per ``ttl_seconds`` per
``(prompt_id, version, tenant_id)`` triple. New prompt versions
written out-of-band to the DB become visible across all replicas
within ``ttl_seconds`` — that is the spec's "hot-reload" knob.

What is cached:

* Successful reads (a :class:`PromptAsset`) are cached for the full
  TTL.
* Negative results (``fetch_or_none`` returns ``None``) are cached for
  a shorter ``negative_ttl_seconds`` so a missing-then-seeded prompt
  becomes visible quickly without hammering Postgres in the meantime.

What is not cached:

* :meth:`PromptRegistry.register` and :meth:`PromptRegistry.latest_version`
  always pass through to the inner registry. ``register`` invalidates
  every cache entry for the affected ``prompt_id`` so the next read
  picks up the new version even within the TTL.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from meta_agent.core.domain.prompt_asset import PromptAsset
from meta_agent.core.ports.prompt_registry import PromptNotFoundError, PromptRegistry

_DEFAULT_TTL_SECONDS = 60.0
_DEFAULT_NEGATIVE_TTL_SECONDS = 5.0


@dataclass(slots=True)
class _CacheEntry:
    """Either a hit (``asset`` set) or a negative cache (both ``None``)."""

    asset: PromptAsset | None
    expires_at: float


class CachingPromptRegistry(PromptRegistry):
    """Read-through TTL cache wrapping another :class:`PromptRegistry`."""

    def __init__(
        self,
        inner: PromptRegistry,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        negative_ttl_seconds: float = _DEFAULT_NEGATIVE_TTL_SECONDS,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if negative_ttl_seconds <= 0:
            raise ValueError("negative_ttl_seconds must be positive")
        self._inner = inner
        self._ttl = ttl_seconds
        self._negative_ttl = negative_ttl_seconds
        self._cache: dict[tuple[str, int | None, str | None], _CacheEntry] = {}
        self._lock = asyncio.Lock()
        # Indirection lets tests inject a fake clock.
        self._monotonic = monotonic if monotonic is not None else asyncio.get_event_loop().time

    async def fetch(
        self,
        prompt_id: str,
        *,
        version: int | None = None,
        tenant_id: str | None = None,
    ) -> PromptAsset:
        asset = await self.fetch_or_none(prompt_id, version=version, tenant_id=tenant_id)
        if asset is None:
            raise PromptNotFoundError(prompt_id, version)
        return asset

    async def fetch_or_none(
        self,
        prompt_id: str,
        *,
        version: int | None = None,
        tenant_id: str | None = None,
    ) -> PromptAsset | None:
        key = (prompt_id, version, tenant_id)
        now = self._monotonic()
        async with self._lock:
            entry = self._cache.get(key)
            if entry is not None and entry.expires_at > now:
                return entry.asset
        # Cache miss / expired — go to inner.
        asset = await self._inner.fetch_or_none(prompt_id, version=version, tenant_id=tenant_id)
        ttl = self._ttl if asset is not None else self._negative_ttl
        async with self._lock:
            self._cache[key] = _CacheEntry(asset=asset, expires_at=self._monotonic() + ttl)
        return asset

    async def register(self, asset: PromptAsset) -> None:
        await self._inner.register(asset)
        # Drop every cache entry tagged with this prompt_id; the new
        # version invalidates "latest" lookups for all version pins
        # and tenant scopes.
        async with self._lock:
            stale = [k for k in self._cache if k[0] == asset.prompt_id]
            for k in stale:
                del self._cache[k]

    async def latest_version(
        self,
        prompt_id: str,
        *,
        tenant_id: str | None = None,
    ) -> int | None:
        # Not cached: seed logic is the only consumer and it runs once
        # per worker boot. Adding a cache here would add complexity
        # for no observable gain.
        return await self._inner.latest_version(prompt_id, tenant_id=tenant_id)
