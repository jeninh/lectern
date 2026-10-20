"""add & edit books: review-before-filing imports, editable work metadata (authors, series,
tags, narrators, primary EPUB, playback), optimistic versions, revisions for undo, trash,
idempotency records, instance settings

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-20

"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_FILE_KIND = sa.Enum("EBOOK", "AUDIO", name="filekind", native_enum=False, length=16)


def upgrade() -> None:
    with op.batch_alter_table("works") as batch:
        batch.add_column(sa.Column("authors", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("series", sa.String(length=256), nullable=True))
        batch.add_column(sa.Column("series_index", sa.Float(), nullable=True))
        batch.add_column(sa.Column("tags", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("narrators", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("primary_file_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("playback_default", sa.String(length=16), nullable=True))
        batch.add_column(
            sa.Column("readalong_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch.add_column(sa.Column("cover_source", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("trashed_by", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("trash_from", sa.String(length=1024), nullable=True))
        batch.create_foreign_key(
            "fk_works_trashed_by", "users", ["trashed_by"], ["id"], ondelete="SET NULL"
        )
        batch.create_index("ix_works_series", ["series"])
        batch.create_index("ix_works_trashed_at", ["trashed_at"])

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, author, sort_author FROM works")).all()
    for wid, author, sort_author in rows:
        conn.execute(
            sa.text("UPDATE works SET authors = :a WHERE id = :id"),
            {"a": json.dumps([{"name": author, "sort": sort_author}]), "id": wid},
        )

    with op.batch_alter_table("audio_tracks") as batch:
        batch.add_column(sa.Column("title", sa.String(length=256), nullable=True))
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("auto_file_imports", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    op.create_table(
        "imports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=256), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "UPLOADING",
                "READY",
                "FILED",
                "DISCARDED",
                "FAILED",
                name="importstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("detected", sa.JSON(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("proposal", sa.JSON(), nullable=True),
        sa.Column("target", sa.JSON(), nullable=True),
        sa.Column("use_cover", sa.Boolean(), nullable=True),
        sa.Column("error", sa.String(length=64), nullable=True),
        sa.Column("work_id", sa.Integer(), nullable=True),
        sa.Column("auto_filed", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("filed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status <> 'FILED' OR work_id IS NOT NULL", name="ck_import_filed"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_imports_public_id", "imports", ["public_id"], unique=True)
    op.create_index("ix_imports_user_status", "imports", ["user_id", "status"])
    op.create_index("ix_imports_idempotency", "imports", ["user_id", "idempotency_key"])

    op.create_table(
        "import_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("import_id", sa.Integer(), nullable=False),
        sa.Column("kind", _FILE_KIND, nullable=False),
        sa.Column("filename", sa.String(length=600), nullable=False),
        sa.Column("rel_path", sa.String(length=256), nullable=False),
        sa.Column("media_type", sa.String(length=64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("partial_md5", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("duplicate_work_id", sa.Integer(), nullable=True),
        sa.Column("probe", sa.JSON(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["import_id"], ["imports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("import_id", "sha256", name="uq_import_file_sha"),
    )
    op.create_index("ix_import_files_import_id", "import_files", ["import_id"])
    op.create_index("ix_import_files_sha256", "import_files", ["sha256"])

    with op.batch_alter_table("upload_sessions") as batch:
        batch.add_column(sa.Column("import_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_upload_sessions_import", "imports", ["import_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_index("ix_upload_sessions_import_id", ["import_id"])

    op.create_table(
        "work_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("batch_id", sa.String(length=32), nullable=True),
        sa.Column("version_before", sa.Integer(), nullable=False),
        sa.Column("version_after", sa.Integer(), nullable=False),
        sa.Column("before", sa.JSON(), nullable=False),
        sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_work_revisions_work_id", "work_revisions", ["work_id"])
    op.create_index("ix_work_revisions_batch", "work_revisions", ["batch_id"])

    op.create_table(
        "trashed_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=True),
        sa.Column("kind", _FILE_KIND, nullable=False),
        sa.Column("rel_path", sa.String(length=1024), nullable=False),
        sa.Column("trash_path", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("row", sa.JSON(), nullable=False),
        sa.Column("trashed_by", sa.Integer(), nullable=True),
        sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trashed_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trashed_files_work_id", "trashed_files", ["work_id"])
    op.create_index("ix_trashed_files_sha256", "trashed_files", ["sha256"])
    op.create_index("ix_trashed_files_trashed_at", "trashed_files", ["trashed_at"])

    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "scope", "key", name="uq_idempotency_user_scope_key"),
    )
    op.create_index("ix_idempotency_records_created_at", "idempotency_records", ["created_at"])

    op.create_table(
        "instance_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("instance_settings")
    op.drop_index("ix_idempotency_records_created_at", table_name="idempotency_records")
    op.drop_table("idempotency_records")
    op.drop_index("ix_trashed_files_trashed_at", table_name="trashed_files")
    op.drop_index("ix_trashed_files_sha256", table_name="trashed_files")
    op.drop_index("ix_trashed_files_work_id", table_name="trashed_files")
    op.drop_table("trashed_files")
    op.drop_index("ix_work_revisions_batch", table_name="work_revisions")
    op.drop_index("ix_work_revisions_work_id", table_name="work_revisions")
    op.drop_table("work_revisions")
    with op.batch_alter_table("upload_sessions") as batch:
        batch.drop_index("ix_upload_sessions_import_id")
        batch.drop_constraint("fk_upload_sessions_import", type_="foreignkey")
        batch.drop_column("import_id")
    op.drop_index("ix_import_files_sha256", table_name="import_files")
    op.drop_index("ix_import_files_import_id", table_name="import_files")
    op.drop_table("import_files")
    op.drop_index("ix_imports_idempotency", table_name="imports")
    op.drop_index("ix_imports_user_status", table_name="imports")
    op.drop_index("ix_imports_public_id", table_name="imports")
    op.drop_table("imports")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("auto_file_imports")
    with op.batch_alter_table("audio_tracks") as batch:
        batch.drop_column("title")
    with op.batch_alter_table("works") as batch:
        batch.drop_index("ix_works_trashed_at")
        batch.drop_index("ix_works_series")
        batch.drop_constraint("fk_works_trashed_by", type_="foreignkey")
        for col in (
            "trash_from",
            "trashed_by",
            "trashed_at",
            "version",
            "cover_source",
            "readalong_enabled",
            "playback_default",
            "primary_file_id",
            "narrators",
            "tags",
            "series_index",
            "series",
            "authors",
        ):
            batch.drop_column(col)
