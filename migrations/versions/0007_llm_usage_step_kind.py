"""``llm_usage_logs.step_kind`` column for multi-model routing aggregation.

Phase β+ PR 4: every LLM call is now tagged with a short ``step_kind``
("plan" / "edit" / "review" / "chat" / "observe") so the metered LLM
client persists which kind of step drove each row. Later phases
(SWE-bench regression, multi-model A/B) join on ``step_kind`` to
compare model performance at the same task type.

The column is nullable because not every call passes through a graph
that classifies its step — smoke harnesses, ad-hoc one-off calls, and
legacy callers continue to write NULL.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_usage_logs",
        sa.Column("step_kind", sa.Text(), nullable=True),
    )
    # Partial index on the tagged rows only — most analytic queries
    # filter to ``step_kind IS NOT NULL`` to compare like-vs-like, and
    # an index over the whole nullable column would not pay for itself
    # while early adoption is partial.
    op.create_index(
        "ix_llm_usage_step_kind",
        "llm_usage_logs",
        ["step_kind", "tenant_id", "created_at"],
        postgresql_where=sa.text("step_kind IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_llm_usage_step_kind", table_name="llm_usage_logs")
    op.drop_column("llm_usage_logs", "step_kind")
