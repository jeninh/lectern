"""Guest accounts (issue #77): a profile for readers with limits, and guest invites.

Revision ID: 0045_guest_accounts
Revises: 0040_series_want_to_read
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045_guest_accounts"
down_revision = "0040_series_want_to_read"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guest_profiles",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("allow_opds", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    # SQLite before 3.35 cannot drop it again, so downgrading there leaves it behind.
    if "guest" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("invites")}:
        op.add_column("invites", sa.Column("guest", sa.JSON(), nullable=True))


def downgrade() -> None:
    # ALTER TABLE ... DROP COLUMN needs SQLite 3.35; before that the nullable column stays.
    if (op.get_bind().dialect.server_version_info or (0,)) >= (3, 35):
        op.drop_column("invites", "guest")
    op.drop_table("guest_profiles")
