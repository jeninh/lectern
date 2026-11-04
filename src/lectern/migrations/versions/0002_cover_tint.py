"""work cover tint

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-19

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


# Plain ADD COLUMN, never a batch rebuild of works: a rebuild DROPs the table, which with
# foreign_keys=ON would cascade into every row that references a work. Already there after a
# downgrade on SQLite before 3.35 (see downgrade): kept as it was.
def upgrade() -> None:
    if "cover_tint" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("works")}:
        op.add_column("works", sa.Column("cover_tint", sa.String(length=7), nullable=True))


def downgrade() -> None:
    # DROP COLUMN in place, for the same reason. It needs SQLite 3.35; before that the nullable
    # column stays and the previous release never reads it.
    if (op.get_bind().dialect.server_version_info or (0,)) >= (3, 35):
        op.drop_column("works", "cover_tint")
