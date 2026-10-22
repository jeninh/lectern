"""PDF text layers: canonical PDF text, sentences with page and box maps (read-along on PDFs)

Revision ID: 0014_pdf_text
Revises: 0013
Create Date: 2026-10-22

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_pdf_text"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pdf_texts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("pdf_sha256", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("text_sha256", sa.String(length=64), nullable=True),
        sa.Column("pages", sa.Integer(), nullable=False),
        sa.Column("pages_with_text", sa.Integer(), nullable=False),
        sa.Column("chars", sa.Integer(), nullable=False),
        sa.Column("sentence_count", sa.Integer(), nullable=False),
        sa.Column("sections", sa.JSON(), nullable=True),
        sa.Column("stats", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(length=512), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("projected_key", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["book_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_id"),
    )
    op.create_index("ix_pdf_texts_work_id", "pdf_texts", ["work_id"])
    op.create_index("ix_pdf_texts_status", "pdf_texts", ["status"])
    op.create_table(
        "pdf_sentences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("section", sa.Integer(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("boxes", sa.JSON(), nullable=False),
        sa.Column("epub_n", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["file_id"], ["book_files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_id", "n", name="uq_pdf_sentence_n"),
    )
    op.create_index("ix_pdf_sentences_page", "pdf_sentences", ["file_id", "page_start"])
    op.create_index("ix_pdf_sentences_epub", "pdf_sentences", ["file_id", "epub_n"])


def downgrade() -> None:
    op.drop_index("ix_pdf_sentences_epub", table_name="pdf_sentences")
    op.drop_index("ix_pdf_sentences_page", table_name="pdf_sentences")
    op.drop_table("pdf_sentences")
    op.drop_index("ix_pdf_texts_status", table_name="pdf_texts")
    op.drop_index("ix_pdf_texts_work_id", table_name="pdf_texts")
    op.drop_table("pdf_texts")
