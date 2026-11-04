"""per-user UI preference: show technical details

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-20

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


# Plain ADD COLUMN, never a batch rebuild of users: a rebuild DROPs the table, which with
# foreign_keys=ON would cascade into every row that references a user. Already there after a
# downgrade on SQLite before 3.35 (see downgrade): kept as it was.
def upgrade() -> None:
    if "show_technical_details" not in {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")
    }:
        op.add_column(
            "users",
            sa.Column(
                "show_technical_details", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
        )


def downgrade() -> None:
    # DROP COLUMN in place, for the same reason. It needs SQLite 3.35; before that the column
    # stays: it has a constant default, so the previous release is unaffected.
    if (op.get_bind().dialect.server_version_info or (0,)) >= (3, 35):
        op.drop_column("users", "show_technical_details")
