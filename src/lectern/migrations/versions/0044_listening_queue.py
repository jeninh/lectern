"""Per-user listening queue (issue #82).

Revision ID: 0044_listening_queue
Revises: 0043_reading_stats
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_listening_queue"
down_revision = "0043_reading_stats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "listening_queue_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=True),
        sa.Column("chapter_start_s", sa.Float(), nullable=True),
        sa.Column("chapter_end_s", sa.Float(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_listening_queue_position"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["track_id"], ["audio_tracks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "work_id", "track_id", "chapter_start_s", name="uq_listening_queue_item"
        ),
    )
    op.create_index("ix_listening_queue_items_user_id", "listening_queue_items", ["user_id"])
    op.create_index("ix_listening_queue_items_work_id", "listening_queue_items", ["work_id"])
    op.create_index("ix_listening_queue_items_track_id", "listening_queue_items", ["track_id"])
    op.create_index(
        "ix_listening_queue_items_user_position", "listening_queue_items", ["user_id", "position"]
    )
    op.create_index(
        "uq_listening_queue_whole_work",
        "listening_queue_items",
        ["user_id", "work_id"],
        unique=True,
        sqlite_where=sa.text("track_id IS NULL"),
    )
    op.create_table(
        "listening_queue_states",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("listening_queue_states")
    op.drop_index("uq_listening_queue_whole_work", table_name="listening_queue_items")
    op.drop_index("ix_listening_queue_items_user_position", table_name="listening_queue_items")
    op.drop_index("ix_listening_queue_items_track_id", table_name="listening_queue_items")
    op.drop_index("ix_listening_queue_items_work_id", table_name="listening_queue_items")
    op.drop_index("ix_listening_queue_items_user_id", table_name="listening_queue_items")
    op.drop_table("listening_queue_items")
