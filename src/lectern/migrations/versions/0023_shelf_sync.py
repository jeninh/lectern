"""Shelf sync: a device follows a shelf, and the KOReader plugin may download by itself

``device_follows``: the shelf a device (app password) follows, whose books are queued for it
by themselves; with ``cleanup``, books that leave it are dequeued and the plugin deletes the
copies it downloaded. Deleting the shelf or the device ends the follow.
``app_passwords.auto_download``: the plugin downloads queued books on its quiet automatic sync.
``device_queue_items.followed``: the item was queued by the followed shelf, not by a person.

Revision ID: 0023_shelf_sync
Revises: 0022_annotations
Create Date: 2026-10-27

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_shelf_sync"
down_revision = "0022_annotations"
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
    # Already there after a downgrade on SQLite before 3.35 (see downgrade): kept. Rows added
    # since got the default; a ``followed`` item whose follow went with device_follows is what
    # an unfollow leaves too, and nothing acts on it until the device follows a shelf again.
    bind = op.get_bind()
    if "auto_download" not in {c["name"] for c in sa.inspect(bind).get_columns("app_passwords")}:
        op.add_column(
            "app_passwords",
            sa.Column("auto_download", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "followed" not in {c["name"] for c in sa.inspect(bind).get_columns("device_queue_items")}:
        op.add_column(
            "device_queue_items",
            sa.Column("followed", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    # DROP COLUMN in place, for the same reason. It needs SQLite 3.35; before that the columns
    # stay: they have server defaults, so 0022's code inserts rows without naming them, and
    # never reads them.
    if (op.get_bind().dialect.server_version_info or (0,)) >= (3, 35):
        op.drop_column("device_queue_items", "followed")
        op.drop_column("app_passwords", "auto_download")
    op.drop_index("ix_device_follows_shelf_id", table_name="device_follows")
    op.drop_table("device_follows")
