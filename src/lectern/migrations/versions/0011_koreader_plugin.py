"""KOReader plugin: one-time bootstrap secrets and 6-digit pairing codes per device

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-21

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_bootstraps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("sync_key_id", sa.Integer(), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("server_url", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_ip", sa.String(length=64), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sync_key_id"], ["sync_keys.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("secret_hash"),
    )
    op.create_index("ix_device_bootstraps_user_id", "device_bootstraps", ["user_id"])
    op.create_index("ix_device_bootstraps_sync_key_id", "device_bootstraps", ["sync_key_id"])
    op.create_table(
        "device_pairing_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("sync_key_id", sa.Integer(), nullable=False),
        sa.Column("salt", sa.String(length=32), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sync_key_id"], ["sync_keys.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_device_pairing_codes_user_id", "device_pairing_codes", ["user_id"])
    op.create_index("ix_device_pairing_codes_sync_key_id", "device_pairing_codes", ["sync_key_id"])


def downgrade() -> None:
    op.drop_index("ix_device_pairing_codes_sync_key_id", table_name="device_pairing_codes")
    op.drop_index("ix_device_pairing_codes_user_id", table_name="device_pairing_codes")
    op.drop_table("device_pairing_codes")
    op.drop_index("ix_device_bootstraps_sync_key_id", table_name="device_bootstraps")
    op.drop_index("ix_device_bootstraps_user_id", table_name="device_bootstraps")
    op.drop_table("device_bootstraps")
