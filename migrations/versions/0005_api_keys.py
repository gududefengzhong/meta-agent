"""``api_keys`` table for Bearer-token ingress authentication.

Stores SHA-256 hashes of issued tokens — never the cleartext — together
with the tenant / principal binding the token grants and an optional
revocation timestamp. ``last_used_at`` is updated best-effort by
:class:`PgTokenValidator` for observability; missing values just mean
the key has not yet authenticated a live request.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Text(), primary_key=True),
        # ``token_hash`` is the lowercase hex SHA-256 of the cleartext
        # token. We index it UNIQUE because the validator looks up by
        # exact match; a dup would mean two issued tokens with the same
        # hash, which is a provisioning bug.
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        # ``scopes`` is forward-compatible storage for RBAC; the α
        # validator does not enforce them, but PR #18 query endpoints
        # are expected to gate on ``read:audits`` / ``read:usages``.
        sa.Column(
            "scopes",
            sa.dialects.postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("ARRAY[]::TEXT[]"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_api_keys_token_hash",
        "api_keys",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_api_keys_tenant",
        "api_keys",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_api_keys_tenant", table_name="api_keys")
    op.drop_index("ix_api_keys_token_hash", table_name="api_keys")
    op.drop_table("api_keys")
