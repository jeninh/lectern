"""Quotes view and highlight of the day (issue #83)

``annotations.favourite`` / ``annotations.hidden``: the owner's marks on one highlight, to tune
the quotes feed (hidden ones leave the feed and the highlight of the day).
``users.quote_of_the_day``: whether Home shows a highlight of the day (on by default).

Revision ID: 0032_quotes
Revises: 0031_device_reports
Create Date: 2026-10-28

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032_quotes"
down_revision = "0031_device_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain ADD COLUMN, never a batch rebuild: rebuilding annotations would drop the
    # annotations_fts triggers (0026), and rebuilding users would cascade on SQLite.
    # Already there after a downgrade on SQLite before 3.35 (see downgrade): kept, as the
    # person's own marks; rows added since got the defaults.
    false, true = sa.false(), sa.true()
    bind = op.get_bind()
    have = {c["name"] for c in sa.inspect(bind).get_columns("annotations")}
    if "favourite" not in have:
        op.add_column(
            "annotations",
            sa.Column("favourite", sa.Boolean(), nullable=False, server_default=false),
        )
    if "hidden" not in have:
        op.add_column(
            "annotations", sa.Column("hidden", sa.Boolean(), nullable=False, server_default=false)
        )
    if "quote_of_the_day" not in {c["name"] for c in sa.inspect(bind).get_columns("users")}:
        op.add_column(
            "users",
            sa.Column("quote_of_the_day", sa.Boolean(), nullable=False, server_default=true),
        )


def downgrade() -> None:
    # DROP COLUMN in place, for the same reason. It needs SQLite 3.35; before that the columns
    # stay: they have server defaults, so 0031's code inserts rows without naming them, and
    # never reads them.
    if (op.get_bind().dialect.server_version_info or (0,)) >= (3, 35):
        op.drop_column("users", "quote_of_the_day")
        op.drop_column("annotations", "hidden")
        op.drop_column("annotations", "favourite")
