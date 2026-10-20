"""pair OPDS app passwords with the KOReader sync key issued alongside them

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-19

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("app_passwords") as batch:
        batch.add_column(sa.Column("sync_key_id", sa.Integer(), nullable=True))
        batch.create_index("ix_app_passwords_sync_key_id", ["sync_key_id"])
        batch.create_foreign_key(
            "fk_app_passwords_sync_key_id",
            "sync_keys",
            ["sync_key_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("app_passwords") as batch:
        batch.drop_constraint("fk_app_passwords_sync_key_id", type_="foreignkey")
        batch.drop_index("ix_app_passwords_sync_key_id")
        batch.drop_column("sync_key_id")
