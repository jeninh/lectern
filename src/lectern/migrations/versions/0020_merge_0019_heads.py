"""Merge the two 0019 heads: automatic metadata and PDF text from OCR

Both branched from ``0018_library_filters``; this revision joins them and changes nothing.

Revision ID: 0020_merge_0019_heads
Revises: 0019_auto_metadata, 0019_pdf_text_ocr
Create Date: 2026-10-27

"""

from __future__ import annotations

revision = "0020_merge_0019_heads"
down_revision = ("0019_auto_metadata", "0019_pdf_text_ocr")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
