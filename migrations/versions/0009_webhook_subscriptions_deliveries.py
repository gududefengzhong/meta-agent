"""``webhook_subscriptions`` + ``webhook_deliveries`` tables (Phase γ-B-2).

Outbound webhook plumbing. ``subscriptions`` is the per-tenant
"where to notify and what to notify on" config; ``deliveries`` is
the per-attempt queue the dispatcher drains.

Schema notes:

* ``webhook_subscriptions.events`` is a Postgres ``TEXT[]`` so the
  fanout step can do ``WHERE $1 = ANY(events)`` against an indexed
  scan rather than a JSONB membership test.
* ``webhook_deliveries (tenant_id, idempotency_key)`` is unique so
  a redelivered audit fanout writing the same key is a no-op rather
  than producing duplicate HTTP attempts.
* The dispatcher's claim query orders by ``next_attempt_at`` and
  filters on ``status='pending'``; the partial index
  ``ix_webhook_deliveries_due`` covers that path cheaply even when
  the table accumulates terminal rows.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_subscriptions",
        sa.Column("subscription_id", sa.Text(), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        # ``secret`` is the HMAC signing key. Never logged, never
        # surfaced over the API; only the dispatcher reads it.
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column(
            "events",
            sa.dialects.postgresql.ARRAY(sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "ix_webhook_subscriptions_tenant_active",
        "webhook_subscriptions",
        ["tenant_id"],
        postgresql_where=sa.text("active = TRUE"),
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("delivery_id", sa.Text(), primary_key=True),
        sa.Column("subscription_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("event_action", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_webhook_deliveries_tenant_idem",
        ),
    )
    op.create_index(
        "ix_webhook_deliveries_due",
        "webhook_deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_due", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index(
        "ix_webhook_subscriptions_tenant_active",
        table_name="webhook_subscriptions",
    )
    op.drop_table("webhook_subscriptions")
