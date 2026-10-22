"""Worker capabilities: job routing by capability, worker presence, enrollment codes

Revision ID: 0012_worker_capabilities
Revises: 0011
Create Date: 2026-10-21

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_worker_capabilities"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("capability", sa.String(length=48), nullable=True))
    # Every job before this revision is "align" or "transcode": the capability is the kind.
    op.execute("UPDATE jobs SET capability = kind")
    with op.batch_alter_table("jobs") as batch:
        batch.alter_column("capability", existing_type=sa.String(length=48), nullable=False)
        batch.create_index("ix_jobs_capability", ["capability"])
    with op.batch_alter_table("worker_credentials") as batch:
        batch.add_column(sa.Column("capabilities", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("advertised", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("state", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("info", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("status_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "worker_enrollments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_ip", sa.String(length=64), nullable=True),
        sa.Column("credential_id", sa.Integer(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["credential_id"], ["worker_credentials.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_worker_enrollments_code_hash", "worker_enrollments", ["code_hash"], unique=True
    )


def downgrade() -> None:
    # Before this revision only "align" went to external workers; the in-process runner
    # never picks up a transcode lease an external worker held. Give such leases back to the
    # queue (without spending the attempt), or they would stay "leased" for good.
    op.execute(
        "UPDATE jobs SET status = 'QUEUED', lease_token_hash = NULL, lease_expires_at = NULL, "
        "heartbeat_at = NULL, leased_by_id = NULL, result = NULL, "
        "attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END "
        "WHERE status = 'LEASED' AND kind != 'align' AND leased_by_id IS NOT NULL"
    )
    op.drop_index("ix_worker_enrollments_code_hash", table_name="worker_enrollments")
    op.drop_table("worker_enrollments")
    # Plain ALTER TABLE … DROP COLUMN (SQLite ≥ 3.35), not a batch copy: recreating
    # worker_credentials would fire jobs.leased_by_id's ON DELETE SET NULL and turn every
    # external align lease into an orphaned local one.
    for column in ("status_at", "info", "state", "advertised", "capabilities"):
        op.drop_column("worker_credentials", column)
    op.drop_index("ix_jobs_capability", table_name="jobs")
    op.drop_column("jobs", "capability")
