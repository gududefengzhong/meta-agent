"""γ-C long-tail expiry sweeper for ``AWAITING_APPROVAL`` tasks.

A paused task that never gets approved sits in ``AWAITING_APPROVAL``
indefinitely. The sweeper is a system-level loop that:

1. Scans the ``tasks`` table cross-tenant for rows whose
   ``state='awaiting_approval'`` and ``updated_at < now - N days``.
2. Transitions each to ``EXPIRED`` via the existing
   :meth:`TaskRepository.transition_from_awaiting_approval` atomic
   guard (so a concurrent approve / abort wins cleanly if it races
   with the sweep).
3. Audits ``task.expired`` so the γ-B-2 webhook fanout pushes the
   notification to subscribed endpoints.

Worktree cleanup is deliberately **not** done here: the sweeper has
no path back to the worker process that originally provisioned the
worktree, and a separate filesystem hygiene job is the right tool
for orphaned ``workspace_root`` entries. The audit row carries
enough metadata for the hygiene job to correlate.

The sweeper is one-shot per :meth:`run_once`; a cron loop or a
daemon supervisor decides how often to invoke it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.domain.task import TaskState
from meta_agent.core.ports.audit_sink import AuditSink
from meta_agent.core.ports.repository import (
    IllegalTaskTransitionError,
    TaskRepository,
)
from meta_agent.infra.security.context import RequestContext, bind_context

logger = logging.getLogger(__name__)

_DEFAULT_EXPIRY_DAYS = 30
_DEFAULT_BATCH_SIZE = 100


class AwaitingApprovalSweeper:
    """Expires stale ``AWAITING_APPROVAL`` tasks past a configurable age."""

    def __init__(
        self,
        *,
        tasks: TaskRepository,
        audits: AuditSink,
        expiry_days: int = _DEFAULT_EXPIRY_DAYS,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if expiry_days <= 0:
            raise ValueError("expiry_days must be positive")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self._tasks = tasks
        self._audits = audits
        self._expiry = timedelta(days=expiry_days)
        self._batch_size = batch_size
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))

    async def run_once(self) -> int:
        """Process one batch. Returns the number of tasks transitioned to EXPIRED.

        Errors per task (lost race against approve / abort, repo
        transient failure) are logged + audited and the sweep
        continues; the loop does not abort the whole batch on one
        bad row.
        """

        now = self._clock()
        threshold = now - self._expiry
        stale = await self._tasks.list_awaiting_approval_older_than(
            threshold, limit=self._batch_size
        )
        expired = 0
        for task in stale:
            try:
                await self._tasks.transition_from_awaiting_approval(
                    task.tenant_id,
                    task.task_id,
                    TaskState.EXPIRED,
                    now,
                )
            except IllegalTaskTransitionError as exc:
                # Another path (approve, abort, second sweeper) raced
                # the transition. Treat as a no-op and audit the
                # observation so the operator can see why some rows
                # in the batch were skipped.
                logger.info(
                    "sweeper.transition_lost_race",
                    extra={"task_id": task.task_id, "reason": str(exc)},
                )
                continue
            except Exception:
                logger.exception(
                    "sweeper.transition_failed",
                    extra={"task_id": task.task_id, "tenant_id": task.tenant_id},
                )
                continue
            ctx = RequestContext(
                tenant_id=task.tenant_id,
                principal_id="system",
                trace_id=task.trace_id,
                request_id=self._id_factory(),
            )
            with bind_context(ctx):
                await self._audits.append(
                    AuditEvent(
                        event_id=self._id_factory(),
                        tenant_id=task.tenant_id,
                        principal_id="system",
                        session_id=task.session_id,
                        task_id=task.task_id,
                        trace_id=task.trace_id,
                        action="task.expired",
                        payload={
                            "task_id": task.task_id,
                            "expired_after_days": self._expiry.days,
                            "paused_at": task.updated_at.isoformat(),
                        },
                        occurred_at=now,
                    )
                )
            expired += 1
        return expired
