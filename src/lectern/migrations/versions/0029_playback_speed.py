"""Playback speed per book, synced (issue #71)

``work_progress.speed``: the last listening speed this person used for the work, saved with
the audio position (and through the same offline queue), so every device opens the book at
it. ``users.playback_speed``: their default for books they have not played yet; the web app
fills it once from the speed this browser used to keep for every book.

Revision ID: 0029_playback_speed
Revises: 0027_reader_settings
Create Date: 2026-10-28

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_playback_speed"
down_revision = "0027_reader_settings"
branch_labels = None
depends_on = None


# Plain ADD/DROP COLUMN, never a batch rebuild: rebuilding users or work_progress on SQLite
# (copy, DROP, rename) with foreign_keys=ON would cascade into every row that references them.


def upgrade() -> None:
    # Already there after a downgrade on SQLite before 3.35 (see downgrade): kept, as the
    # speeds the person last used.
    bind = op.get_bind()
    if "speed" not in {c["name"] for c in sa.inspect(bind).get_columns("work_progress")}:
        op.add_column("work_progress", sa.Column("speed", sa.Float(), nullable=True))
    if "playback_speed" not in {c["name"] for c in sa.inspect(bind).get_columns("users")}:
        op.add_column("users", sa.Column("playback_speed", sa.Float(), nullable=True))


def downgrade() -> None:
    # DROP COLUMN in place, for the same reason. It needs SQLite 3.35; before that the columns
    # stay: they are nullable and 0027's code never reads them.
    if (op.get_bind().dialect.server_version_info or (0,)) >= (3, 35):
        op.drop_column("users", "playback_speed")
        op.drop_column("work_progress", "speed")
