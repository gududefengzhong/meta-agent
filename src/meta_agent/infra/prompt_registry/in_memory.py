"""Single-process in-memory :class:`PromptRegistry`.

Stores rows in a list and resolves on each call. Tenant precedence
matches the spec (tenant row shadows global). Mutations are guarded
by an ``asyncio.Lock`` so concurrent ``register`` calls do not race
each other for the "next version" computation.

Use cases:

* Unit tests that need a deterministic registry without spinning
  Postgres.
* Worker bootstraps that pre-seed every prompt at startup and never
  hot-reload — the in-memory registry behaves identically to Postgres
  for read-only consumers.
"""

from __future__ import annotations

import asyncio

from meta_agent.core.domain.prompt_asset import PromptAsset
from meta_agent.core.ports.prompt_registry import PromptNotFoundError, PromptRegistry


class InMemoryPromptRegistry(PromptRegistry):
    """Single-process registry. Not safe across worker processes."""

    def __init__(self) -> None:
        self._rows: list[PromptAsset] = []
        self._lock = asyncio.Lock()

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
        async with self._lock:
            return self._resolve(prompt_id, version=version, tenant_id=tenant_id)

    async def register(self, asset: PromptAsset) -> None:
        async with self._lock:
            for existing in self._rows:
                if (
                    existing.prompt_id == asset.prompt_id
                    and existing.version == asset.version
                    and existing.tenant_id == asset.tenant_id
                ):
                    raise ValueError(
                        f"prompt {asset.prompt_id!r}@{asset.version} for "
                        f"tenant={asset.tenant_id!r} is already registered; "
                        "versions are immutable, insert version + 1 to evolve"
                    )
            self._rows.append(asset)

    async def latest_version(
        self,
        prompt_id: str,
        *,
        tenant_id: str | None = None,
    ) -> int | None:
        async with self._lock:
            candidates = [
                r.version
                for r in self._rows
                if r.prompt_id == prompt_id and r.tenant_id == tenant_id
            ]
            return max(candidates) if candidates else None

    def _resolve(
        self,
        prompt_id: str,
        *,
        version: int | None,
        tenant_id: str | None,
    ) -> PromptAsset | None:
        # Tenant precedence: prefer tenant_id match, fall back to global (None).
        for scope in (tenant_id, None) if tenant_id is not None else (None,):
            rows = [r for r in self._rows if r.prompt_id == prompt_id and r.tenant_id == scope]
            if not rows:
                continue
            if version is not None:
                exact = [r for r in rows if r.version == version]
                if exact:
                    return exact[0]
                # version pin didn't match this scope; try the next one
                continue
            return max(rows, key=lambda r: r.version)
        return None
