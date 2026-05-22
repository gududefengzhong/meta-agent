"""asyncpg-backed :class:`PromptRegistry` against the ``prompts`` table.

Schema lives in migration ``0006_prompts_and_llm_usage_prompt_columns``.
Concurrency model:

* ``register`` is naturally idempotent for a fixed
  ``(prompt_id, version, tenant_id)`` triple: the partial unique
  indexes turn a duplicate insert into a constraint violation, which
  the adapter surfaces as :class:`ValueError`. Higher layers
  (``ensure_seeded``) compute ``version = latest + 1`` and tolerate
  losing the race — a competing seeder may have inserted the same
  ``next_version`` first, in which case the second attempt simply
  returns the version the winner registered.
* ``fetch_or_none`` does *not* enforce tenant isolation against the
  bound request context; the adapter is a registry, not a per-tenant
  data store. Global rows are visible to all tenants by design and
  tenant overrides are addressed by passing ``tenant_id`` explicitly.
"""

from __future__ import annotations

from typing import Any

from asyncpg.exceptions import UniqueViolationError

from meta_agent.core.domain.prompt_asset import PromptAsset
from meta_agent.core.ports.prompt_registry import PromptNotFoundError, PromptRegistry
from meta_agent.infra.persistence.pool import DatabasePool


def _row_to_asset(row: dict[str, Any]) -> PromptAsset:
    return PromptAsset(
        prompt_id=row["prompt_id"],
        version=row["version"],
        tenant_id=row["tenant_id"],
        content=row["content"],
        description=row["description"],
        created_at=row["created_at"],
    )


class PgPromptRegistry(PromptRegistry):
    """Postgres-backed registry. Shared source of truth across workers."""

    _INSERT = """
        INSERT INTO prompts (
            prompt_id, version, tenant_id, content, description,
            content_hash, created_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7)
    """

    _FETCH_SPECIFIC_GLOBAL = """
        SELECT prompt_id, version, tenant_id, content, description, created_at
        FROM prompts
        WHERE prompt_id = $1 AND version = $2 AND tenant_id IS NULL
        LIMIT 1
    """

    _FETCH_SPECIFIC_TENANT = """
        SELECT prompt_id, version, tenant_id, content, description, created_at
        FROM prompts
        WHERE prompt_id = $1 AND version = $2 AND tenant_id = $3
        LIMIT 1
    """

    _FETCH_LATEST_GLOBAL = """
        SELECT prompt_id, version, tenant_id, content, description, created_at
        FROM prompts
        WHERE prompt_id = $1 AND tenant_id IS NULL
        ORDER BY version DESC
        LIMIT 1
    """

    _FETCH_LATEST_TENANT = """
        SELECT prompt_id, version, tenant_id, content, description, created_at
        FROM prompts
        WHERE prompt_id = $1 AND tenant_id = $2
        ORDER BY version DESC
        LIMIT 1
    """

    _LATEST_VERSION_GLOBAL = """
        SELECT MAX(version) AS v FROM prompts
        WHERE prompt_id = $1 AND tenant_id IS NULL
    """

    _LATEST_VERSION_TENANT = """
        SELECT MAX(version) AS v FROM prompts
        WHERE prompt_id = $1 AND tenant_id = $2
    """

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

    async def fetch(
        self,
        prompt_id: str,
        *,
        version: int | None = None,
        tenant_id: str | None = None,
    ) -> PromptAsset:
        asset = await self.fetch_or_none(
            prompt_id, version=version, tenant_id=tenant_id
        )
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
        async with self._pool.acquire() as conn:
            # Tenant precedence: try the tenant row first, then global.
            if tenant_id is not None:
                if version is not None:
                    row = await conn.fetchrow(
                        self._FETCH_SPECIFIC_TENANT, prompt_id, version, tenant_id
                    )
                else:
                    row = await conn.fetchrow(
                        self._FETCH_LATEST_TENANT, prompt_id, tenant_id
                    )
                if row is not None:
                    return _row_to_asset(dict(row))
            if version is not None:
                row = await conn.fetchrow(self._FETCH_SPECIFIC_GLOBAL, prompt_id, version)
            else:
                row = await conn.fetchrow(self._FETCH_LATEST_GLOBAL, prompt_id)
            return _row_to_asset(dict(row)) if row is not None else None

    async def register(self, asset: PromptAsset) -> None:
        async with self._pool.acquire() as conn:
            try:
                await conn.execute(
                    self._INSERT,
                    asset.prompt_id,
                    asset.version,
                    asset.tenant_id,
                    asset.content,
                    asset.description,
                    asset.content_hash,
                    asset.created_at,
                )
            except UniqueViolationError as exc:
                raise ValueError(
                    f"prompt {asset.prompt_id!r}@{asset.version} for "
                    f"tenant={asset.tenant_id!r} is already registered; "
                    "versions are immutable, insert version + 1 to evolve"
                ) from exc

    async def latest_version(
        self,
        prompt_id: str,
        *,
        tenant_id: str | None = None,
    ) -> int | None:
        async with self._pool.acquire() as conn:
            if tenant_id is None:
                row = await conn.fetchrow(self._LATEST_VERSION_GLOBAL, prompt_id)
            else:
                row = await conn.fetchrow(self._LATEST_VERSION_TENANT, prompt_id, tenant_id)
        if row is None or row["v"] is None:
            return None
        return int(row["v"])
