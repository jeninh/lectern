"""per-device queues: works sent to one OPDS device (app password)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-20

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_queue_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("app_password_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("queued_by", sa.Integer(), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_file_id", sa.Integer(), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status <> 'DELIVERED' OR delivered_at IS NOT NULL", name="ck_device_queue_delivered"
        ),
        sa.CheckConstraint(
            "status <> 'REMOVED' OR removed_at IS NOT NULL", name="ck_device_queue_removed"
        ),
        sa.ForeignKeyConstraint(["app_password_id"], ["app_passwords.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["queued_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["removed_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["delivered_file_id"], ["book_files.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_password_id", "work_id", name="uq_device_queue_device_work"),
    )
    op.create_index(
        "ix_device_queue_items_feed",
        "device_queue_items",
        ["app_password_id", "status", "position"],
    )
    op.create_index("ix_device_queue_items_user_id", "device_queue_items", ["user_id"])
    op.create_index("ix_device_queue_items_work_id", "device_queue_items", ["work_id"])


def downgrade() -> None:
    op.drop_index("ix_device_queue_items_work_id", table_name="device_queue_items")
    op.drop_index("ix_device_queue_items_user_id", table_name="device_queue_items")
    op.drop_index("ix_device_queue_items_feed", table_name="device_queue_items")
    op.drop_table("device_queue_items")
