"""Data saver: a per-work switch for making data-saver copies, and a per-user default

``works.data_saver_enabled`` (default on): whether data-saver renditions may be made for the
work. Turning it off stops new ones; existing copies stay until deleted.
``users.data_saver`` (default off): whether the player and offline downloads use the
data-saver copy on devices that have not chosen for themselves.

Revision ID: 0015_data_saver
Revises: 0014_nested_shelves
Create Date: 2026-10-22

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_data_saver"
down_revision = "0014_nested_shelves"
branch_labels = None
depends_on = None


# Plain ADD COLUMN, never a batch rebuild: rebuilding users or works on SQLite (copy, DROP,
# rename) with foreign_keys=ON would cascade into every row that references them (shelf
# items, grants, progress). SQLite adds a NOT NULL column with a constant default in place.
def upgrade() -> None:
    op.add_column(
        "works",
        sa.Column("data_saver_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "users", sa.Column("data_saver", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    # DROP COLUMN in place (SQLite 3.35+), for the same reason.
    op.drop_column("users", "data_saver")
    op.drop_column("works", "data_saver_enabled")
