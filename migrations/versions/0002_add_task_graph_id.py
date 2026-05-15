"""Add tasks.graph_id column.

The orchestration runtime resolves the graph to execute from either
``task_type`` (default) or an explicit ``graph_id`` override. The
column is nullable to preserve the implicit-default contract for the
business families that ship with a single canonical graph each.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("graph_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "graph_id")
