"""``tasks.permission_mode`` + ``tasks.budget_policy`` columns (Phase γ-A).

The two fields are independent and orthogonally composable; together
they describe the Phase γ trust-surface configuration for one task.

``permission_mode`` declares the human-in-the-loop policy
(``auto`` / ``approve_before_push`` / ``approve_each_tool``).
``budget_policy`` declares the budget-threshold reaction
(``none`` / ``gate_on_threshold`` / ``abort_on_threshold``).

Both default to the legacy zero-friction values
(``auto`` / ``none``) so existing tasks behave exactly as they did
before γ.

This migration also renames ``tasks.state`` rows from
``awaiting_human`` to ``awaiting_approval`` to match the renamed
enum value. The previous value was a placeholder that no graph ever
wrote, so the data migration is a precaution rather than a
necessity.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "permission_mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'auto'"),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "budget_policy",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'none'"),
        ),
    )
    # Backfill any legacy ``awaiting_human`` rows to the renamed value.
    op.execute(
        sa.text("UPDATE tasks SET state = 'awaiting_approval' WHERE state = 'awaiting_human'")
    )
    # Partial index keyed on tasks that the γ sweeper / approval API
    # need to find quickly. Most tasks are in terminal states; the
    # AWAITING_APPROVAL set stays small and benefits from a focused
    # index rather than the broad ``(tenant_id, state)`` index above.
    op.create_index(
        "ix_tasks_awaiting_approval",
        "tasks",
        ["tenant_id", "updated_at"],
        postgresql_where=sa.text("state = 'awaiting_approval'"),
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_awaiting_approval", table_name="tasks")
    op.execute(
        sa.text("UPDATE tasks SET state = 'awaiting_human' WHERE state = 'awaiting_approval'")
    )
    op.drop_column("tasks", "budget_policy")
    op.drop_column("tasks", "permission_mode")
