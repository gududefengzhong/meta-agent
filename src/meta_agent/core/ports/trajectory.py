"""Trajectory query port (Phase γ-B-1).

The repository owns the cross-table join over ``audit_events``,
``task_checkpoints`` and ``llm_usage_logs``. Splitting it out from the
existing per-aggregate repositories keeps the join logic in one place
and avoids forcing every other repo to grow a trajectory-shaped
method.

The port is read-only by design — trajectory rows are never written
through this surface, they appear because the underlying append-only
writers (audit sink, checkpoint append, metered llm client) already
wrote them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from meta_agent.core.domain.trajectory import TrajectoryPage


class TrajectoryRepository(ABC):
    """Read-only access to the merged audit + checkpoint + usage timeline."""

    @abstractmethod
    async def list_for_task(
        self,
        tenant_id: str,
        task_id: str,
        *,
        since: datetime | None = None,
        limit_per_source: int = 1000,
    ) -> TrajectoryPage:
        """Return a time-ordered page of trajectory items for one task.

        Each of the three underlying tables is queried with the same
        ``limit_per_source`` cap; if any cap is hit the returned page's
        ``truncated`` flag is set. Items are returned ordered by
        occurrence timestamp ASC. ``since`` filters items strictly
        greater than the supplied timestamp (None means "from the
        beginning of the task").
        """
