"""Append-only ``llm_usage_logs`` table for L0 cost visibility.

Each row captures one LLM invocation: tenant / task / trace
attribution, model identity, token counts, finish reason, latency,
status, and optional resolved cost (USD * 1_000_000). The table is
indexed for two read patterns:

* ``(tenant_id, created_at DESC)`` — billing rollups in a time window.
* ``(tenant_id, task_id)`` — per-task usage summary.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_usage_logs",
        sa.Column("record_id", sa.Text(), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("principal_id", sa.Text(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("requested_model", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("finish_reason", sa.Text(), nullable=True),
        sa.Column("provider_response_id", sa.Text(), nullable=True),
        # BIGINT: micro-USD can exceed 2.1B for high-token calls or
        # expensive models; INTEGER would overflow around $2,147.
        sa.Column("cost_usd_micros", sa.BigInteger(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_category", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_llm_usage_tenant_created",
        "llm_usage_logs",
        ["tenant_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_llm_usage_tenant_task",
        "llm_usage_logs",
        ["tenant_id", "task_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_usage_tenant_task", table_name="llm_usage_logs")
    op.drop_index("ix_llm_usage_tenant_created", table_name="llm_usage_logs")
    op.drop_table("llm_usage_logs")
