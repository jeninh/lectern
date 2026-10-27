"""Shelf sync: a device follows a shelf, and the KOReader plugin may download by itself

``device_follows``: the shelf a device (app password) follows, whose books are queued for it
by themselves; with ``cleanup``, books that leave it are dequeued and the plugin deletes the
copies it downloaded. Deleting the shelf or the device ends the follow.
``app_passwords.auto_download``: the plugin downloads queued books on its quiet automatic sync.
``device_queue_items.followed``: the item was queued by the followed shelf, not by a person.

Revision ID: 0019_shelf_sync
Revises: 0018_library_filters
Create Date: 2026-10-27

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_shelf_sync"
down_revision = "0018_library_filters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_follows",
        sa.Column("app_password_id", sa.Integer(), nullable=False),
        sa.Column("shelf_id", sa.Integer(), nullable=False),
        sa.Column("cleanup", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["app_password_id"], ["app_passwords.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shelf_id"], ["shelves.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("app_password_id"),
    )
    op.create_index("ix_device_follows_shelf_id", "device_follows", ["shelf_id"])
    # Plain ADD COLUMN, never a batch rebuild: rebuilding app_passwords on SQLite (copy, DROP,
    # rename) with foreign_keys=ON would cascade into every queue item that references it.
    op.add_column(
        "app_passwords",
        sa.Column("auto_download", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "device_queue_items",
        sa.Column("followed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    # DROP COLUMN in place (SQLite 3.35+), for the same reason.
    op.drop_column("device_queue_items", "followed")
    op.drop_column("app_passwords", "auto_download")
    op.drop_index("ix_device_follows_shelf_id", table_name="device_follows")
    op.drop_table("device_follows")
