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
    # Plain ADD COLUMN (nullable, no default). Already there after a downgrade on SQLite
    # before 3.35 (see downgrade), with hashes nothing kept current since: cleared, so a PDF
    # with OCR words is read again rather than trusted.
    if "ocr_sha256" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("pdf_texts")}:
        op.add_column("pdf_texts", sa.Column("ocr_sha256", sa.String(length=64), nullable=True))
    else:
        op.execute("UPDATE pdf_texts SET ocr_sha256 = NULL")


def downgrade() -> None:
    # ALTER TABLE … DROP COLUMN needs SQLite 3.35. Before that the column stays: it is
    # nullable and 0018's code never reads it. Never a batch rebuild of pdf_texts: nothing
    # references it today, but a rebuild DROPs the table, which with foreign_keys=ON would
    # cascade into any table that comes to reference it.
    if (op.get_bind().dialect.server_version_info or (0,)) >= (3, 35):
        op.drop_column("pdf_texts", "ocr_sha256")
