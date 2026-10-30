"""Audiobook bookmarks and clips with notes (``audio_bookmarks``, issue #81)

A moment (``end_s`` NULL) or a clip (``start_s``..``end_s``) on a work's audio timeline, with
an optional note, private to ``user_id``. Times only: a clip plays back from the work's own
tracks, no audio file is made. The sentence range it covers is worked out when it is read
(lectern.audio_bookmarks), so a re-alignment moves it with the text.

Revision ID: 0033_audio_bookmarks
Revises: 0032_quotes
Create Date: 2026-10-28

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_audio_bookmarks"
down_revision = "0032_quotes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audio_bookmarks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("start_s", sa.Float(), nullable=False),
        sa.Column("end_s", sa.Float(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("chapter", sa.String(length=512), nullable=True),
        sa.Column("device", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("start_s >= 0", name="ck_audio_bookmark_start"),
        sa.CheckConstraint("end_s IS NULL OR end_s > start_s", name="ck_audio_bookmark_span"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audio_bookmarks_user_work", "audio_bookmarks", ["user_id", "work_id"])
    op.create_index("ix_audio_bookmarks_work_id", "audio_bookmarks", ["work_id"])


def downgrade() -> None:
    op.drop_index("ix_audio_bookmarks_work_id", table_name="audio_bookmarks")
    op.drop_index("ix_audio_bookmarks_user_work", table_name="audio_bookmarks")
    op.drop_table("audio_bookmarks")
