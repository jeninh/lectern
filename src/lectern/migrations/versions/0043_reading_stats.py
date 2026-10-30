"""Private reading statistics (issue #69)

Revision ID: 0043_reading_stats
Revises: 0041_auto_shelves
Create Date: 2026-10-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043_reading_stats"
down_revision = "0041_auto_shelves"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("reading_timezone", sa.String(length=64), nullable=True))
    op.create_table(
        "reading_activity",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("from_fraction", sa.Float(), nullable=False),
        sa.Column("to_fraction", sa.Float(), nullable=False),
        sa.Column("read_seconds", sa.Integer(), server_default="0", nullable=False),
        sa.Column("listened_seconds", sa.Integer(), server_default="0", nullable=False),
        sa.Column("pages", sa.Float(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reading_activity_user_at", "reading_activity", ["user_id", "at"])
    op.create_index("ix_reading_activity_user_work_at", "reading_activity", ["user_id", "work_id", "at"])
    op.create_table(
        "reading_stats_days",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column("read_seconds", sa.Integer(), server_default="0", nullable=False),
        sa.Column("listened_seconds", sa.Integer(), server_default="0", nullable=False),
        sa.Column("pages", sa.Float(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "work_id", "day", name="uq_reading_stats_day"),
    )
    op.create_index("ix_reading_stats_days_user_id", "reading_stats_days", ["user_id"])
    op.create_index("ix_reading_stats_days_day", "reading_stats_days", ["day"])


def downgrade() -> None:
    op.drop_index("ix_reading_stats_days_day", table_name="reading_stats_days")
    op.drop_index("ix_reading_stats_days_user_id", table_name="reading_stats_days")
    op.drop_table("reading_stats_days")
    op.drop_index("ix_reading_activity_user_work_at", table_name="reading_activity")
    op.drop_index("ix_reading_activity_user_at", table_name="reading_activity")
    op.drop_table("reading_activity")
    if (op.get_bind().dialect.server_version_info or (0,)) >= (3, 35):
        op.drop_column("users", "reading_timezone")
