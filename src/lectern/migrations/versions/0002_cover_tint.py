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


def upgrade() -> None:
    with op.batch_alter_table("works") as batch:
        batch.add_column(sa.Column("cover_tint", sa.String(length=7), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("works") as batch:
        batch.drop_column("cover_tint")
