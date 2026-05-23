"""``tasks.budget_threshold_micros`` column (Phase γ-C).

Nullable per-task budget ceiling in micro-USD. The
:class:`BudgetPolicy` enum (set in migration 0008) only fires when
the threshold is non-null; existing rows default to NULL and behave
exactly as they did before γ-C.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("budget_threshold_micros", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "budget_threshold_micros")
