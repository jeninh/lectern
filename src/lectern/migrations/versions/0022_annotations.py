"""KOReader highlights and notes (``annotations``, lectern.annotations)

Revision ID: 0022_annotations
Revises: 0021_email_in
Create Date: 2026-10-27

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_annotations"
down_revision = "0021_email_in"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "annotations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("document", sa.String(length=128), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("chapter", sa.String(length=512), nullable=True),
        sa.Column("color", sa.String(length=32), nullable=True),
        sa.Column("drawer", sa.String(length=32), nullable=True),
        sa.Column("pos0", sa.Text(), nullable=False),
        sa.Column("pos1", sa.Text(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("pageno", sa.Integer(), nullable=True),
        sa.Column("created", sa.String(length=32), nullable=False),
        sa.Column("updated", sa.String(length=32), nullable=False),
        sa.Column("device", sa.String(length=128), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("loc_sha", sa.String(length=64), nullable=True),
        sa.Column("loc_spine", sa.Integer(), nullable=True),
        sa.Column("loc_char", sa.Integer(), nullable=True),
        sa.Column("loc_len", sa.Integer(), nullable=True),
        sa.Column("sentence_n", sa.Integer(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "key", name="uq_annotation_user_key"),
    )
    op.create_index("ix_annotations_user_document", "annotations", ["user_id", "document"])


def downgrade() -> None:
    op.drop_index("ix_annotations_user_document", table_name="annotations")
    op.drop_table("annotations")
