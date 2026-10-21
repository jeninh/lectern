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


def upgrade() -> None:
    with op.batch_alter_table("works") as batch:
        batch.add_column(sa.Column("cover_style", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("cover_seed", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("works") as batch:
        batch.drop_column("cover_seed")
        batch.drop_column("cover_style")
