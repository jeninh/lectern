"""shared contracts for milestones 2-4: audio tracks, sentences, alignments, canonical
progress, invites, shelves and grants, job queue, worker credentials

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-20

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_worker_credentials_key_hash", "worker_credentials", ["key_hash"], unique=True
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED",
                "LEASED",
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                name="jobstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("leased_by_id", sa.Integer(), nullable=True),
        sa.Column("lease_token_hash", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="ck_job_attempts"),
        sa.CheckConstraint(
            "status <> 'LEASED' OR lease_token_hash IS NOT NULL", name="ck_job_lease"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["leased_by_id"], ["worker_credentials.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_kind", "jobs", ["kind"], unique=False)
    op.create_index("ix_jobs_work_id", "jobs", ["work_id"], unique=False)
    op.create_index("ix_jobs_claim", "jobs", ["status", "run_after", "priority"], unique=False)
    op.create_index(
        "uq_jobs_active_work_kind",
        "jobs",
        ["work_id", "kind"],
        unique=True,
        sqlite_where=sa.text("status IN ('QUEUED', 'LEASED')"),
    )

    op.create_table(
        "audio_tracks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("rel_path", sa.String(length=1024), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("codec", sa.String(length=32), nullable=False),
        sa.Column("duration_s", sa.Float(), nullable=False),
        sa.Column("offset_s", sa.Float(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("chapters", sa.JSON(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_audio_track_position"),
        sa.CheckConstraint("duration_s >= 0", name="ck_audio_track_duration"),
        sa.CheckConstraint("offset_s >= 0", name="ck_audio_track_offset"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_id", "position", name="uq_audio_track_position"),
        sa.UniqueConstraint("work_id", "rel_path", name="uq_audio_track_path"),
        sa.UniqueConstraint("work_id", "sha256", name="uq_audio_track_sha256"),
    )
    op.create_index("ix_audio_tracks_sha256", "audio_tracks", ["sha256"], unique=False)
    op.create_index("ix_audio_tracks_work_id", "audio_tracks", ["work_id"], unique=False)

    op.create_table(
        "sentences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("spine_index", sa.Integer(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("audio_start_s", sa.Float(), nullable=True),
        sa.Column("audio_end_s", sa.Float(), nullable=True),
        sa.Column("matched", sa.Boolean(), nullable=False),
        sa.CheckConstraint("n >= 0", name="ck_sentence_n"),
        sa.CheckConstraint("char_end > char_start AND char_start >= 0", name="ck_sentence_span"),
        sa.CheckConstraint(
            "(audio_start_s IS NULL) = (audio_end_s IS NULL)", name="ck_sentence_audio_pair"
        ),
        sa.CheckConstraint(
            "audio_end_s IS NULL OR audio_end_s >= audio_start_s", name="ck_sentence_audio_span"
        ),
        sa.CheckConstraint("NOT matched OR audio_start_s IS NOT NULL", name="ck_sentence_matched"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_id", "n", name="uq_sentence_n"),
    )
    op.create_index("ix_sentences_work_id", "sentences", ["work_id"], unique=False)
    op.create_index(
        "ix_sentences_text", "sentences", ["work_id", "spine_index", "char_start"], unique=False
    )
    op.create_index("ix_sentences_audio", "sentences", ["work_id", "audio_start_s"], unique=False)

    op.create_table(
        "alignments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                "STALE",
                name="alignmentstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("coverage", sa.Float(), nullable=True),
        sa.Column("sentence_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("epub_sha256", sa.String(length=64), nullable=False),
        sa.Column("audio_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "coverage IS NULL OR (coverage >= 0 AND coverage <= 1)", name="ck_alignment_coverage"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_id"),
    )
    op.create_index("ix_alignments_status", "alignments", ["status"], unique=False)

    op.create_table(
        "work_progress",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("sentence_n", sa.Integer(), nullable=False),
        sa.Column("sentence_offset", sa.Integer(), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "KOREADER", "WEB", "AUDIO", name="progresssource", native_enum=False, length=16
            ),
            nullable=False,
        ),
        sa.Column("raw", sa.Text(), nullable=False),
        sa.Column("device", sa.String(length=128), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sentence_n >= 0 AND sentence_offset >= 0", name="ck_work_progress_pos"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "work_id", name="uq_work_progress_user_work"),
    )
    op.create_index("ix_work_progress_user_id", "work_progress", ["user_id"], unique=False)
    op.create_index("ix_work_progress_work_id", "work_progress", ["work_id"], unique=False)

    op.create_table(
        "invites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "role",
            sa.Enum("OWNER", "MEMBER", "READER", name="role", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("note", sa.String(length=128), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by", sa.Integer(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('MEMBER', 'READER')", name="ck_invite_role"),
        sa.CheckConstraint("(used_at IS NULL) = (used_by IS NULL)", name="ck_invite_used"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["used_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )

    op.create_table(
        "shelves",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("rule", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "name", name="uq_shelf_owner_name"),
    )
    op.create_index("ix_shelves_owner_id", "shelves", ["owner_id"], unique=False)

    op.create_table(
        "shelf_items",
        sa.Column("shelf_id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["shelf_id"], ["shelves.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("shelf_id", "work_id"),
    )
    op.create_index("ix_shelf_items_work_id", "shelf_items", ["work_id"], unique=False)

    op.create_table(
        "shelf_grants",
        sa.Column("shelf_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "permission",
            sa.Enum("VIEW", "EDIT", name="shelfpermission", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("granted_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["shelf_id"], ["shelves.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("shelf_id", "user_id"),
    )
    op.create_index("ix_shelf_grants_user_id", "shelf_grants", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_shelf_grants_user_id", table_name="shelf_grants")
    op.drop_table("shelf_grants")
    op.drop_index("ix_shelf_items_work_id", table_name="shelf_items")
    op.drop_table("shelf_items")
    op.drop_index("ix_shelves_owner_id", table_name="shelves")
    op.drop_table("shelves")
    op.drop_table("invites")
    op.drop_index("ix_work_progress_work_id", table_name="work_progress")
    op.drop_index("ix_work_progress_user_id", table_name="work_progress")
    op.drop_table("work_progress")
    op.drop_index("ix_alignments_status", table_name="alignments")
    op.drop_table("alignments")
    op.drop_index("ix_sentences_audio", table_name="sentences")
    op.drop_index("ix_sentences_text", table_name="sentences")
    op.drop_index("ix_sentences_work_id", table_name="sentences")
    op.drop_table("sentences")
    op.drop_index("ix_audio_tracks_work_id", table_name="audio_tracks")
    op.drop_index("ix_audio_tracks_sha256", table_name="audio_tracks")
    op.drop_table("audio_tracks")
    op.drop_index("uq_jobs_active_work_kind", table_name="jobs")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_index("ix_jobs_work_id", table_name="jobs")
    op.drop_index("ix_jobs_kind", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_worker_credentials_key_hash", table_name="worker_credentials")
    op.drop_table("worker_credentials")
