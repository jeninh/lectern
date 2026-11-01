"""Optional retention of raw reading-stats events (issue #69).

Revision ID: 0046_stat_event_retention
Revises: 0045_guest_accounts
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046_stat_event_retention"
down_revision = "0045_guest_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reading_session_rollups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seconds", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reading_session_rollups_user_start",
        "reading_session_rollups",
        ["user_id", "started_at"],
    )
    with op.batch_alter_table("reading_stats_days") as batch:
        batch.add_column(
            sa.Column("frozen_read_seconds", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("frozen_listened_seconds", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(sa.Column("frozen_pages", sa.Float(), server_default="0", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("reading_stats_days") as batch:
        batch.drop_column("frozen_pages")
        batch.drop_column("frozen_listened_seconds")
        batch.drop_column("frozen_read_seconds")
    op.drop_index("ix_reading_session_rollups_user_start", table_name="reading_session_rollups")
    op.drop_table("reading_session_rollups")
