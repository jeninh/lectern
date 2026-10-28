"""Automatic metadata suggestions and library duplicate detection

``metadata_suggestions``: one row per work for the automatic metadata pass
(``lectern.autometa``): its queue state, and the suggestions for fields the work was missing,
waiting for a person to apply or dismiss them.

``match_suggestions.origin``: ``ingest`` (existing rows: a new file looked like a work) or
``library`` (the duplicate scan, ``lectern.duplicates``).

Revision ID: 0019_auto_metadata
Revises: 0018_library_filters
Create Date: 2026-10-27

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_auto_metadata"
down_revision = "0018_library_filters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metadata_suggestions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(length=64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("requested_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_metadata_suggestions_work_id", "metadata_suggestions", ["work_id"], unique=True
    )
    op.create_index("ix_metadata_suggestions_status", "metadata_suggestions", ["status"])
    # Plain ADD COLUMN with a constant default, never a batch rebuild (see 0017). Already
    # there after a downgrade on SQLite before 3.35 (see downgrade): rows added since got the
    # default, which is right, since 0018's code only makes ingest suggestions.
    if "origin" not in {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("match_suggestions")
    }:
        op.add_column(
            "match_suggestions",
            sa.Column("origin", sa.String(length=16), nullable=False, server_default="ingest"),
        )


def downgrade() -> None:
    # ALTER TABLE … DROP COLUMN needs SQLite 3.35. Before that the column stays: it has a
    # server default, so 0018's code inserts rows without naming it, and never reads it.
    if (op.get_bind().dialect.server_version_info or (0,)) >= (3, 35):
        op.drop_column("match_suggestions", "origin")
    op.drop_index("ix_metadata_suggestions_status", table_name="metadata_suggestions")
    op.drop_index("ix_metadata_suggestions_work_id", table_name="metadata_suggestions")
    op.drop_table("metadata_suggestions")
