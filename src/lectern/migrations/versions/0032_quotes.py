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
    false, true = sa.false(), sa.true()
    op.add_column(
        "annotations", sa.Column("favourite", sa.Boolean(), nullable=False, server_default=false)
    )
    op.add_column(
        "annotations", sa.Column("hidden", sa.Boolean(), nullable=False, server_default=false)
    )
    op.add_column(
        "users", sa.Column("quote_of_the_day", sa.Boolean(), nullable=False, server_default=true)
    )


def downgrade() -> None:
    # DROP COLUMN in place (SQLite 3.35+), for the same reason.
    op.drop_column("users", "quote_of_the_day")
    op.drop_column("annotations", "hidden")
    op.drop_column("annotations", "favourite")
