"""AI keys: per-user, scoped bearer credentials for the MCP endpoint

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-20

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ip", sa.String(length=64), nullable=True),
        sa.Column("last_client", sa.String(length=128), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_keys_user_id", "ai_keys", ["user_id"])
    op.create_index("ix_ai_keys_key_hash", "ai_keys", ["key_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_ai_keys_key_hash", table_name="ai_keys")
    op.drop_index("ix_ai_keys_user_id", table_name="ai_keys")
    op.drop_table("ai_keys")
