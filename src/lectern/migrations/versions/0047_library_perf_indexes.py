"""Large-library performance (issue #140): the index the stats year view needs, and
planner statistics.

Revision ID: 0047_library_perf_indexes
Revises: 0046_stat_event_retention
Create Date: 2026-11-01
"""

from __future__ import annotations

from alembic import op

revision = "0047_library_perf_indexes"
down_revision = "0046_stat_event_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The year view reads one person's days in a range: (user_id, day) finds them directly,
    # where ix_reading_stats_days_day walked every person's days in that range.
    op.create_index("ix_reading_stats_days_user_day", "reading_stats_days", ["user_id", "day"])
    # Statistics for SQLite's query planner (the app also runs PRAGMA optimize at startup).
    # Without them it could choose a plan that probed shelf_items once per work and shelf.
    op.execute("ANALYZE")


def downgrade() -> None:
    op.drop_index("ix_reading_stats_days_user_day", table_name="reading_stats_days")
