"""Web reader settings that follow a person across browsers (issue #79)

``users.reader_settings``: their default typography and theme, only the fields they changed
(NULL until the first save; the app's defaults apply).
``reader_overrides``: settings for one work over that default ("Just this book"); deleting
the person or the work deletes them.

Revision ID: 0027_reader_settings
Revises: 0026_highlights_search
Create Date: 2026-10-28

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_reader_settings"
down_revision = "0026_highlights_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain ADD COLUMN, never a batch rebuild: rebuilding users on SQLite with foreign_keys=ON
    # would cascade into everything that references it. Already there after a downgrade on
    # SQLite before 3.35 (see downgrade): kept, as the settings the person last saved.
    if "reader_settings" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}:
        op.add_column("users", sa.Column("reader_settings", sa.JSON(), nullable=True))
    op.create_table(
        "reader_overrides",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "work_id"),
    )
    op.create_index("ix_reader_overrides_work_id", "reader_overrides", ["work_id"])


def downgrade() -> None:
    op.drop_index("ix_reader_overrides_work_id", table_name="reader_overrides")
    op.drop_table("reader_overrides")
    # DROP COLUMN in place, for the same reason. It needs SQLite 3.35; before that the column
    # stays: it is nullable and 0026's code never reads it.
    if (op.get_bind().dialect.server_version_info or (0,)) >= (3, 35):
        op.drop_column("users", "reader_settings")
