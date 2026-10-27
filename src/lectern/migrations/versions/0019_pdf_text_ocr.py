"""PDF text from OCR: which recognised words a PDF's canonical text was built from

``pdf_texts.ocr_sha256``: the SHA-256 of the OCR words file (``derived/ocr-<prefix>.json``)
a scan's text document was built from, NULL when it was read from the PDF's own text layer.
A new recognition (another language, a retry) changes it, so the PDF is read again.

Revision ID: 0019_pdf_text_ocr
Revises: 0018_library_filters
Create Date: 2026-10-27

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_pdf_text_ocr"
down_revision = "0018_library_filters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("pdf_texts") as batch:
        batch.add_column(sa.Column("ocr_sha256", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("pdf_texts") as batch:
        batch.drop_column("ocr_sha256")
