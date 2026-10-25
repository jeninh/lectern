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


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("crashes", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("crashes")
