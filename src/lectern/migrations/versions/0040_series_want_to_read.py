"""Per-user want-to-read queue (issue #70).

Revision ID: 0040_series_want_to_read
Revises: 0044_listening_queue
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040_series_want_to_read"
down_revision = "0044_listening_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "want_to_read_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_want_to_read_position"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "work_id", name="uq_want_to_read_item"),
    )
    op.create_index("ix_want_to_read_items_user_id", "want_to_read_items", ["user_id"])
    op.create_index("ix_want_to_read_items_work_id", "want_to_read_items", ["work_id"])
    op.create_index(
        "ix_want_to_read_items_user_position", "want_to_read_items", ["user_id", "position"]
    )


def downgrade() -> None:
    op.drop_index("ix_want_to_read_items_user_position", table_name="want_to_read_items")
    op.drop_index("ix_want_to_read_items_work_id", table_name="want_to_read_items")
    op.drop_index("ix_want_to_read_items_user_id", table_name="want_to_read_items")
    op.drop_table("want_to_read_items")
