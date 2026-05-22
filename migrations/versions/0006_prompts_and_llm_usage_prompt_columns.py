"""``prompts`` table + ``llm_usage_logs`` prompt-provenance columns.

Phase β+ PR 2: persist versioned prompt assets in a dedicated table and
record which (prompt_id, version) drove each LLM call. The schema is
designed so:

* ``(prompt_id, version, tenant_id)`` is unique — versions are
  immutable; updates always insert ``version + 1``.
* ``tenant_id`` is nullable; ``NULL`` rows are the global / system
  prompts. A non-null tenant row shadows the global row for that
  tenant — adapter resolution treats the tenant row as higher
  precedence.
* ``content_hash`` is stored alongside ``content`` so seed runs can
  detect drift cheaply without re-hashing every row at startup.

The two new columns on ``llm_usage_logs`` are nullable because not
every call originates from a registered prompt (early-stage shell tests
inject raw strings, smoke harnesses pass ad-hoc text), and forcing
non-null would block those paths without buying anything.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "prompts",
        sa.Column("prompt_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        # ``NULL`` = global / system prompt; a non-null value scopes to
        # one tenant and shadows the global row at fetch time.
        sa.Column("tenant_id", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # Lowercase hex SHA-256 of ``content``; the domain model
        # recomputes it on construction, so DB and model never drift.
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    # ``tenant_id`` can be NULL, so we use a partial unique index for
    # the global rows and a regular unique constraint for the
    # tenant-scoped rows. Postgres treats NULL as distinct under a
    # plain UNIQUE so we need the partial index to keep at most one
    # global row per (prompt_id, version).
    op.create_index(
        "uq_prompts_global_pid_ver",
        "prompts",
        ["prompt_id", "version"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NULL"),
    )
    op.create_index(
        "uq_prompts_tenant_pid_ver",
        "prompts",
        ["prompt_id", "version", "tenant_id"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
    )
    op.create_index(
        "ix_prompts_pid",
        "prompts",
        ["prompt_id"],
    )

    op.add_column(
        "llm_usage_logs",
        sa.Column("prompt_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "llm_usage_logs",
        sa.Column("prompt_version", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("llm_usage_logs", "prompt_version")
    op.drop_column("llm_usage_logs", "prompt_id")
    op.drop_index("ix_prompts_pid", table_name="prompts")
    op.drop_index("uq_prompts_tenant_pid_ver", table_name="prompts")
    op.drop_index("uq_prompts_global_pid_ver", table_name="prompts")
    op.drop_table("prompts")
