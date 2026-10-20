"""milestone 2: in-app notifications, known browser devices, download events and caps, ingest
match suggestions, upload idempotency and chunk-replay columns

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-20

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])
    op.create_index("ix_notifications_user_unread", "notifications", ["user_id", "read_at"])

    op.create_table(
        "known_devices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_ip", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_known_devices_user_id", "known_devices", ["user_id"])

    op.create_table(
        "download_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("credential", sa.String(length=48), nullable=False),
        sa.Column("target", sa.String(length=64), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("blocked", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_download_events_user_at", "download_events", ["user_id", "at"])
    op.create_index("ix_download_events_credential_at", "download_events", ["credential", "at"])

    op.create_table(
        "match_suggestions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("candidate_work_id", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.String(length=16), nullable=True),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_id", "candidate_work_id", name="uq_match_suggestion_pair"),
    )
    op.create_index("ix_match_suggestions_work_id", "match_suggestions", ["work_id"])
    op.create_index(
        "ix_match_suggestions_candidate_work_id", "match_suggestions", ["candidate_work_id"]
    )

    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("download_cap_per_hour", sa.Integer(), nullable=True))
    with op.batch_alter_table("app_passwords") as batch:
        batch.add_column(sa.Column("download_cap_per_hour", sa.Integer(), nullable=True))
    with op.batch_alter_table("upload_sessions") as batch:
        batch.add_column(sa.Column("idempotency_key", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("last_chunk_sha256", sa.String(length=64), nullable=True))
        batch.create_index("ix_upload_sessions_idempotency", ["user_id", "idempotency_key"])


def downgrade() -> None:
    with op.batch_alter_table("upload_sessions") as batch:
        batch.drop_index("ix_upload_sessions_idempotency")
        batch.drop_column("last_chunk_sha256")
        batch.drop_column("idempotency_key")
    with op.batch_alter_table("app_passwords") as batch:
        batch.drop_column("download_cap_per_hour")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("download_cap_per_hour")
    op.drop_index("ix_match_suggestions_candidate_work_id", table_name="match_suggestions")
    op.drop_index("ix_match_suggestions_work_id", table_name="match_suggestions")
    op.drop_table("match_suggestions")
    op.drop_index("ix_download_events_credential_at", table_name="download_events")
    op.drop_index("ix_download_events_user_at", table_name="download_events")
    op.drop_table("download_events")
    op.drop_index("ix_known_devices_user_id", table_name="known_devices")
    op.drop_table("known_devices")
    op.drop_index("ix_notifications_user_unread", table_name="notifications")
    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_table("notifications")
