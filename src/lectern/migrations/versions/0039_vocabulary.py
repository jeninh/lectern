"""Private vocabulary list (issue #80)

``vocabulary_words``: words a person saved from the dictionary, one row per (user, language,
folded lemma), with where they met it. Deleting the book keeps the word (``work_id`` NULL).

Revision ID: 0039_vocabulary
Revises: 0029_playback_speed
Create Date: 2026-10-28

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039_vocabulary"
down_revision = "0029_playback_speed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vocabulary_words",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("lang", sa.String(length=16), nullable=False),
        sa.Column("lemma", sa.String(length=200), nullable=False),
        sa.Column("lemma_key", sa.String(length=200), nullable=False),
        sa.Column("word", sa.String(length=200), nullable=False),
        sa.Column("to", sa.String(length=16), nullable=True),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("pos", sa.String(length=32), nullable=True),
        sa.Column("dictionary", sa.String(length=120), nullable=True),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("work_id", sa.Integer(), nullable=True),
        sa.Column("file_id", sa.Integer(), nullable=True),
        sa.Column("spine_index", sa.Integer(), nullable=True),
        sa.Column("char", sa.Integer(), nullable=True),
        sa.Column("length", sa.Integer(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("sentence_n", sa.Integer(), nullable=True),
        sa.Column("seen_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("seen_count >= 1", name="ck_vocabulary_seen_count"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["file_id"], ["book_files.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "lang", "lemma_key", name="uq_vocabulary_user_word"),
    )
    op.create_index("ix_vocabulary_words_work_id", "vocabulary_words", ["work_id"])
    op.create_index("ix_vocabulary_user_created", "vocabulary_words", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_vocabulary_user_created", table_name="vocabulary_words")
    op.drop_index("ix_vocabulary_words_work_id", table_name="vocabulary_words")
    op.drop_table("vocabulary_words")
