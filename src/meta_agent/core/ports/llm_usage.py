"""LLM usage repository port.

Append-only persistence for :class:`LLMUsageRecord`. Kept as a
dedicated port (rather than folded into :class:`AuditRepository`)
because the access patterns are different: usage logs are queried by
``(tenant_id, task_id)`` for per-task summaries, by
``(tenant_id, created_at)`` for billing rollups, and by
``(tenant_id, window)`` for budget enforcement; none of which is how
audit events are read.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from meta_agent.core.domain.llm_usage import LLMUsageRecord
from meta_agent.core.ports.budget import BudgetUsage


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

    @abstractmethod
    async def aggregate_since(
        self,
        tenant_id: str,
        since: datetime,
    ) -> BudgetUsage:
        """Sum ``total_tokens`` and ``cost_usd_micros`` for ``tenant_id``.

        Window is ``[since, +inf)``; the caller supplies ``since`` as a
        timezone-aware UTC datetime (typically the first instant of the
        current calendar month). NULL token or cost values count as 0.
        """
