"""Prompt registry port — fetch and persist versioned prompt assets.

Phase β+ introduces a DB-backed prompt registry so graph nodes stop
inlining their system / user prompts as Python string literals and
instead resolve them through a versioned table. ``llm_usage_logs`` and
``audit_events`` then carry the exact ``prompt_id`` + ``version`` that
drove each LLM call, which is what later phases (multi-model A/B,
SWE-bench regression analysis) need to make sense of cost / quality
deltas.

Two operations matter to graph code:

* :meth:`PromptRegistry.fetch` — read the latest active version of a
  prompt, or a specific version when pinning is needed.
* :meth:`PromptRegistry.fetch_or_none` — same, but returns ``None``
  instead of raising for callers that need to fall back.

Management / seeding code additionally uses :meth:`register` to insert
a new version (typically version N+1 of an existing ``prompt_id`` when
the new content's hash differs from the current latest).

The port is async because the default adapter is Postgres-backed; the
in-memory adapter implements the same coroutine signatures so callers
do not branch on backend identity.

Tenant scoping: ``tenant_id`` is optional on every method. ``None``
means "look up the global / system row". Adapters that support
per-tenant overrides MUST treat a tenant-scoped row as a higher-
precedence shadow of the same ``prompt_id``; ``fetch(prompt_id,
tenant_id='t-1')`` should return the tenant row when present and fall
back to the global row otherwise.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from meta_agent.core.domain.errors import AgentError, ErrorCategory
from meta_agent.core.domain.prompt_asset import PromptAsset


class PromptNotFoundError(AgentError):
    """Raised when a requested prompt_id / version is not registered.

    Mapped to :class:`ErrorCategory.LOGIC` because a graph asking for a
    prompt that does not exist almost always indicates the seed step
    failed to run or the prompt id literal drifted from what was
    registered — neither is retriable without code / config changes.
    """

    def __init__(self, prompt_id: str, version: int | None) -> None:
        suffix = f"@{version}" if version is not None else " (latest)"
        super().__init__(
            f"prompt {prompt_id!r}{suffix} not found in registry",
            category=ErrorCategory.LOGIC,
        )
        self.prompt_id = prompt_id
        self.version = version


class PromptRegistry(ABC):
    """Read / write access to versioned prompt assets."""

    @abstractmethod
    async def fetch(
        self,
        prompt_id: str,
        *,
        version: int | None = None,
        tenant_id: str | None = None,
    ) -> PromptAsset:
        """Return the prompt; raise :class:`PromptNotFoundError` if absent.

        ``version=None`` resolves to the highest version available.
        ``tenant_id`` selects a per-tenant override if present, falling
        back to the global (``tenant_id IS NULL``) row.
        """

    @abstractmethod
    async def fetch_or_none(
        self,
        prompt_id: str,
        *,
        version: int | None = None,
        tenant_id: str | None = None,
    ) -> PromptAsset | None:
        """Same as :meth:`fetch` but returns ``None`` when absent."""

    @abstractmethod
    async def register(self, asset: PromptAsset) -> None:
        """Insert a new (prompt_id, version) row.

        Implementations MUST refuse to overwrite an existing
        ``(prompt_id, version, tenant_id)`` triple — versions are
        immutable. Callers that want to evolve a prompt insert
        ``version = latest + 1`` instead.
        """

    @abstractmethod
    async def latest_version(
        self,
        prompt_id: str,
        *,
        tenant_id: str | None = None,
    ) -> int | None:
        """Return the highest version registered, or ``None`` if absent.

        Used by seed logic to compute "what version should I insert
        next when the content hash differs from the current latest".
        """
