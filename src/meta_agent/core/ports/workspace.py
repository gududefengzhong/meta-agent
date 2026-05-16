"""Workspace lifecycle port.

The :class:`WorkspaceManager` port hides the concrete mechanism for
materializing a ``git worktree + feature branch`` per task. The local
single-worker adapter shells out to ``git``; future adapters can
hydrate from a content-addressed cache, a remote build server, or a
container-isolated sandbox without the business layer noticing.

Adapters MUST satisfy two invariants:

* ``provision`` returns a :class:`Workspace` whose ``worktree_path``
  is an existing, writable directory containing ``branch`` checked
  out. The base ref must NOT be checked out in this worktree.
* ``cleanup`` is idempotent and never raises for the common
  "already gone" case; partial failures (e.g. residual files on
  disk) are surfaced through :class:`WorkspaceError` so the caller
  can audit them, but they never block the task's terminal write.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from meta_agent.core.domain.workspace import Workspace


class WorkspaceError(Exception):
    """Raised when provisioning or cleaning a workspace fails."""


class WorkspaceManager(ABC):
    """Materialize and dispose of per-task git workspaces."""

    @abstractmethod
    async def provision(
        self,
        *,
        tenant_id: str,
        task_id: str,
        trace_id: str,
        branch: str,
        repo_url: str | None = None,
        base_ref: str | None = None,
    ) -> Workspace:
        """Create a fresh worktree for ``task_id`` and return its handle.

        ``branch`` is the feature branch the task is allowed to write
        on; it must not be the base ref. When ``repo_url`` is ``None``
        the adapter is free to bootstrap a locally-initialised repo
        (useful for smoke flows that do not require remote source).

        Raises :class:`WorkspaceError` on any failure that left the
        host in a partially-provisioned state; the caller is
        responsible for not retrying without first auditing.
        """

    @abstractmethod
    async def cleanup(self, workspace: Workspace) -> None:
        """Tear down ``workspace`` and release its filesystem footprint.

        Idempotent: calling :meth:`cleanup` on an already-removed
        workspace is a no-op. Raises :class:`WorkspaceError` only when
        a partial cleanup happened (e.g. files remained on disk); the
        worker treats this as best-effort and never blocks terminal
        state on it.
        """
