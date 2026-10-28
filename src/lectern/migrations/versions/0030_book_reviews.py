"""Per-book private ratings, notes, and read-throughs (``book_reviews``, ``read_throughs``,
lectern.reviews, issue #84)

``book_reviews``: one person's rating (half stars, 1–10) and Markdown notes for one work, and
whether they dismissed the "Rate it?" prompt. ``read_throughs``: the days they started and
finished it, one row per read (re-reads are more rows). Nothing is backfilled: until a person
saves read-throughs for a work, its dates are the ones ``reading_states`` derives.

Revision ID: 0030_book_reviews
Revises: 0026_highlights_search
Create Date: 2026-10-28

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030_book_reviews"
down_revision = "0026_highlights_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "book_reviews",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("prompt_dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "rating IS NULL OR rating BETWEEN 1 AND 10", name="ck_book_review_rating"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "work_id"),
    )
    op.create_index("ix_book_reviews_work_id", "book_reviews", ["work_id"])
    op.create_table(
        "read_throughs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "started_at IS NOT NULL OR finished_at IS NOT NULL", name="ck_read_through_dates"
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR finished_at IS NULL OR finished_at >= started_at",
            name="ck_read_through_order",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_read_throughs_work_id", "read_throughs", ["work_id"])
    op.create_index("ix_read_throughs_user_work", "read_throughs", ["user_id", "work_id"])
    op.create_index("ix_read_throughs_user_finished", "read_throughs", ["user_id", "finished_at"])


def downgrade() -> None:
    op.drop_index("ix_read_throughs_user_finished", table_name="read_throughs")
    op.drop_index("ix_read_throughs_user_work", table_name="read_throughs")
    op.drop_index("ix_read_throughs_work_id", table_name="read_throughs")
    op.drop_table("read_throughs")
    op.drop_index("ix_book_reviews_work_id", table_name="book_reviews")
    op.drop_table("book_reviews")
