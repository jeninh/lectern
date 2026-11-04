"""generated covers: per-work style and palette shuffle

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-21

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


# Plain ADD COLUMN, never a batch rebuild of works: a rebuild DROPs the table, which with
# foreign_keys=ON would cascade into every row that references a work. Already there after a
# downgrade on SQLite before 3.35 (see downgrade): kept as it was.
def upgrade() -> None:
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("works")}
    if "cover_style" not in have:
        op.add_column("works", sa.Column("cover_style", sa.String(length=16), nullable=True))
    if "cover_seed" not in have:
        op.add_column(
            "works", sa.Column("cover_seed", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    # DROP COLUMN in place, for the same reason. It needs SQLite 3.35; before that the columns
    # stay: the previous release never reads them, and cover_seed has a constant default.
    if (op.get_bind().dialect.server_version_info or (0,)) >= (3, 35):
        op.drop_column("works", "cover_seed")
        op.drop_column("works", "cover_style")
