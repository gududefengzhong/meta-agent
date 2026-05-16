"""Per-task git workspace model.

Per ``docs/specs/AGENT_SPEC.md`` L0 isolation constraint, every task
that touches code must run inside a dedicated ``git worktree +
feature branch`` so the main branch is never mutated directly. This
module describes one such workspace as a domain value.

The aggregate is intentionally **ephemeral**: it lives in worker
memory for the duration of a single task run and is destroyed by the
adapter on cleanup. Lifecycle is observed through audit events
(``workspace.provisioned`` / ``workspace.cleaned``) rather than a
dedicated table; persistence is deferred until there is a real cross-
worker recovery / janitor use case.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Workspace(BaseModel):
    """An isolated git working tree provisioned for one task run.

    ``worktree_path`` is the absolute filesystem path inside which the
    task is allowed to read and write. Everything outside of it is off
    limits. ``branch`` is the feature branch checked out in the
    worktree; any commit produced by the task must land on it, never
    on the base ref.

    ``repo_url`` and ``base_ref`` are recorded for traceability so an
    audit reader can answer "where did this workspace come from?"
    without consulting the adapter. They are nullable because system
    flows (smoke tests, dry runs) may legitimately operate on a fresh
    locally-initialised repo with no upstream source.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    repo_url: str | None = Field(
        default=None,
        description="Upstream source URL the workspace was hydrated from, when applicable.",
    )
    base_ref: str | None = Field(
        default=None,
        description="Commit, tag or branch from which ``branch`` was forked.",
    )
    branch: str = Field(
        ...,
        min_length=1,
        description="Feature branch checked out in the worktree; never the base ref.",
    )
    worktree_path: str = Field(
        ...,
        min_length=1,
        description="Absolute path on the worker host; the task's writable sandbox.",
    )
    created_at: datetime
