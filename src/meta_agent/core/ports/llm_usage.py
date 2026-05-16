"""LLM usage repository port.

Append-only persistence for :class:`LLMUsageRecord`. Kept as a
dedicated port (rather than folded into :class:`AuditRepository`)
because the access patterns are different: usage logs are queried by
``(tenant_id, task_id)`` for per-task summaries and by
``(tenant_id, created_at)`` for billing rollups, neither of which is
how audit events are read.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from meta_agent.core.domain.llm_usage import LLMUsageRecord


class LLMUsageRepository(ABC):
    """Append-only persistence for :class:`LLMUsageRecord`."""

    @abstractmethod
    async def record(self, record: LLMUsageRecord) -> None:
        """Persist ``record``. Must be idempotent on ``record_id``."""

    @abstractmethod
    async def list_for_task(
        self,
        tenant_id: str,
        task_id: str,
    ) -> list[LLMUsageRecord]:
        """Return every usage record attributed to ``task_id``.

        Ordering is by ``created_at`` ascending so callers can sum
        token counts in invocation order without re-sorting.
        """
