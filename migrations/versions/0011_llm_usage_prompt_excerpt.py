"""``llm_usage_logs.prompt_excerpt`` for redacted prompt previews.

Phase γ observability: persist a bounded, redacted preview of the
request messages sent to the LLM so trajectory queries and Langfuse
exports can show what prompt shape drove a call without storing the
full raw prompt body.

The column is nullable because:

* some callers may intentionally suppress prompt previews;
* legacy rows predate the feature;
* empty / tool-free requests can legitimately yield no preview.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_usage_logs",
        sa.Column("prompt_excerpt", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("llm_usage_logs", "prompt_excerpt")
