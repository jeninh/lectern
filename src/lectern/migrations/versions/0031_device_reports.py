"""Device dashboard: what the KOReader plugin reports about its device (issue #86)

``device_reports``: one row per device (app password) with the latest battery level and
charging state, free storage, plugin and KOReader versions, and the Lectern books on the
device (as sent: work ids and document digests with their position). Deleting the device's
app password deletes its report.

Revision ID: 0031_device_reports
Revises: 0030_book_reviews
Create Date: 2026-10-28

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_device_reports"
down_revision = "0030_book_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_reports",
        sa.Column("app_password_id", sa.Integer(), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("battery", sa.Integer(), nullable=True),
        sa.Column("charging", sa.Boolean(), nullable=True),
        sa.Column("free_bytes", sa.Integer(), nullable=True),
        sa.Column("plugin_version", sa.String(length=32), nullable=True),
        sa.Column("koreader_version", sa.String(length=64), nullable=True),
        sa.Column("books", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["app_password_id"], ["app_passwords.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("app_password_id"),
    )


def downgrade() -> None:
    op.drop_table("device_reports")
