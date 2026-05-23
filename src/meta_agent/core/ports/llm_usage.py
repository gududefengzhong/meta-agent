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
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from meta_agent.core.domain.llm_usage import LLMUsageRecord, LLMUsageStatus
from meta_agent.core.ports.budget import BudgetUsage


class UsageGroupBy(StrEnum):
    """Buckets supported by :meth:`LLMUsageRepository.aggregate_grouped`.

    * ``MODEL`` — group by ``model`` string (NULL → ``"unknown"``).
    * ``DAY``   — group by ``date_trunc('day', created_at)`` UTC.
    * ``TASK``  — group by ``task_id`` (NULL → ``"unattributed"``).
    * ``PRINCIPAL`` — group by ``principal_id`` (NULL → ``"unattributed"``).
    * ``STEP_KIND`` — group by ``step_kind`` string (NULL →
      ``"unspecified"``). Per-task cost views surface a breakdown by
      this dimension so operators see how plan / edit / review tokens
      split.
    """

    MODEL = "model"
    DAY = "day"
    TASK = "task"
    PRINCIPAL = "principal"
    STEP_KIND = "step_kind"


@dataclass(frozen=True, slots=True)
class UsageAggregate:
    """One bucket of an :meth:`LLMUsageRepository.aggregate_grouped` result.

    Read-only projection of ``llm_usage_logs``; never written back.
    ``key`` carries the grouping value (model id, ISO date, task id,
    principal id). ``tokens`` / ``cost_usd_micros`` sum NULL-as-0 so
    rows missing pricing still surface. ``calls`` is the row count in
    the bucket so callers can spot bursts of cheap calls separately
    from a few expensive ones.
    """

    key: str
    tokens: int
    cost_usd_micros: int
    calls: int


@dataclass(frozen=True, slots=True)
class LLMUsageFilter:
    """Filter / pagination parameters for :meth:`LLMUsageRepository.list_filtered`.

    ``before`` is the exclusive keyset cursor: ``(created_at, record_id)``
    of the last row of the previous page. The next page starts strictly
    before this tuple in DESC order, breaking ties on ``record_id``.
    """

    since: datetime
    until: datetime
    model: str | None = None
    task_id: str | None = None
    status: LLMUsageStatus | None = None
    before: tuple[datetime, str] | None = None
    limit: int = 100


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

    @abstractmethod
    async def list_filtered(
        self,
        tenant_id: str,
        filt: LLMUsageFilter,
    ) -> list[LLMUsageRecord]:
        """Return up to ``filt.limit`` records matching ``filt``.

        Ordering is by ``created_at`` DESC, breaking ties on
        ``record_id`` DESC so callers can keyset-paginate using the
        last row's ``(created_at, record_id)`` tuple via
        :attr:`LLMUsageFilter.before`. The window is the half-open
        interval ``[filt.since, filt.until)``.
        """

    @abstractmethod
    async def aggregate_for_task(
        self,
        tenant_id: str,
        task_id: str,
        group_by: UsageGroupBy,
    ) -> list[UsageAggregate]:
        """Per-task aggregate over the task's full lifetime.

        Unlike :meth:`aggregate_grouped`, the window is bounded by the
        task id rather than a time range — callers (worker result
        builder, budget gate) want "how much has this task spent" with
        no time horizon. NULL group keys collapse to the same sentinel
        :class:`UsageGroupBy` uses for the tenant-wide call.
        """

    @abstractmethod
    async def aggregate_grouped(
        self,
        tenant_id: str,
        since: datetime,
        until: datetime,
        group_by: UsageGroupBy,
    ) -> list[UsageAggregate]:
        """Return per-bucket sums over ``[since, until)`` for ``tenant_id``.

        Buckets are sorted by ``tokens`` DESC so the heaviest consumers
        sort first. NULL token / cost values count as 0; NULL group
        keys collapse to a sentinel (see :class:`UsageGroupBy`).
        """
