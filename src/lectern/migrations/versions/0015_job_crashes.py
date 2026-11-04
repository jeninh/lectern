"""Jobs count the times they crashed their worker (``jobs.crashes``)

A job that takes its worker process down twice (an engine calling ``sys.exit()``, a segfault,
an out-of-memory kill) fails for good with the last error instead of looping.

Revision ID: 0015_job_crashes
Revises: 0014_nested_shelves
Create Date: 2026-10-22

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_job_crashes"
down_revision = "0014_nested_shelves"
branch_labels = None
depends_on = None


# Plain ADD COLUMN, never a batch rebuild of jobs: a rebuild DROPs the table, which with
# foreign_keys=ON clears every ON DELETE SET NULL reference to a job (alignments, generated
# audiobooks, integration usage). Already there after a downgrade on SQLite before 3.35 (see
# downgrade): kept as it was.
def upgrade() -> None:
    if "crashes" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("jobs")}:
        op.add_column(
            "jobs", sa.Column("crashes", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    # DROP COLUMN in place, for the same reason. It needs SQLite 3.35; before that the column
    # stays: it has a constant default, so the previous release is unaffected.
    if (op.get_bind().dialect.server_version_info or (0,)) >= (3, 35):
        op.drop_column("jobs", "crashes")
