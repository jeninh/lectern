"""Library filters and smart shelves: reading states, page counts, filter indexes

``reading_states``: how far each person is with each work across every source (KOReader and
the page viewer in ``reading_progress``, the web reader and audio player in
``work_progress``), with when they last read it and first started and finished it
(``lectern.reading_state`` keeps it current). Backfilled here from the positions that exist:
the latest one gives the fraction; the earliest one stands in for the start, since no history
was kept before.

``book_files.pages``: page counts for the ``pages:`` filter, backfilled for PDFs whose text
layer was already read.

Indexes on the columns library filters and sorts look up: ``works.created_at``,
``language``, ``publisher``, ``published``, and ``imports(work_id, status)``.

Revision ID: 0018_library_filters
Revises: 0017_data_saver
Create Date: 2026-10-27

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_library_filters"
down_revision = "0017_data_saver"
branch_labels = None
depends_on = None

FINISHED = 0.985  # lectern.reading_state.FINISHED

_INDEXES = [
    ("ix_works_created_at", "works", ["created_at"]),
    ("ix_works_language", "works", ["language"]),
    ("ix_works_publisher", "works", ["publisher"]),
    ("ix_works_published", "works", ["published"]),
    ("ix_imports_work_status", "imports", ["work_id", "status"]),
]


def _fraction(n, source, raw, sentences, duration) -> float | None:
    """lectern.reading_state.work_progress_fraction, on raw rows."""
    if sentences:
        return min(1.0, n / sentences)
    if source == "AUDIO" and duration:
        try:
            return max(0.0, min(1.0, float(raw) / duration))
        except (TypeError, ValueError):
            return None
    return None


def _backfill(conn) -> None:
    # (user, work) -> list of (at, fraction); timestamps are SQLite's naive-UTC strings,
    # which order correctly as text.
    seen: dict[tuple[int, int], list[tuple[str, float]]] = {}
    rows = conn.execute(
        sa.text(
            "SELECT rp.user_id, bf.work_id, rp.percentage, rp.updated_at FROM reading_progress rp "
            "JOIN book_files bf ON bf.partial_md5 = rp.document"
        )
    )
    for user_id, work_id, pct, at in rows:
        seen.setdefault((user_id, work_id), []).append((at, max(0.0, min(1.0, pct or 0.0))))
    rows = conn.execute(
        sa.text(
            "SELECT wp.user_id, wp.work_id, wp.sentence_n, wp.source, wp.raw, wp.received_at, "
            "(SELECT count(*) FROM sentences s WHERE s.work_id = wp.work_id), "
            "(SELECT sum(t.duration_s) FROM audio_tracks t WHERE t.work_id = wp.work_id) "
            # A KOReader-sourced position projects a reading_progress row counted above.
            "FROM work_progress wp WHERE wp.source != 'KOREADER'"
        )
    )
    for user_id, work_id, n, source, raw, at, sentences, duration in rows:
        f = _fraction(n, source, raw, sentences, duration)
        seen.setdefault((user_id, work_id), []).append((at, 0.0 if f is None else f))
    states = sa.table(
        "reading_states",
        sa.column("user_id", sa.Integer),
        sa.column("work_id", sa.Integer),
        sa.column("fraction", sa.Float),
        sa.column("last_read_at", sa.String),
        sa.column("started_at", sa.String),
        sa.column("finished_at", sa.String),
    )
    out = []
    for (user_id, work_id), points in seen.items():
        points.sort(key=lambda p: p[0] or "")
        last_at, fraction = points[-1]
        started = next((at for at, f in points if f > 0), None)
        finished = next((at for at, f in points if f >= FINISHED), None)
        out.append(
            {
                "user_id": user_id,
                "work_id": work_id,
                "fraction": fraction,
                "last_read_at": last_at,
                "started_at": started,
                "finished_at": finished,
            }
        )
    if out:
        op.bulk_insert(states, out)
    conn.execute(
        sa.text(
            "UPDATE book_files SET pages = (SELECT p.pages FROM pdf_texts p "
            "WHERE p.file_id = book_files.id AND p.pdf_sha256 = book_files.sha256 AND p.pages > 0) "
            "WHERE pages IS NULL"
        )
    )


def upgrade() -> None:
    op.create_table(
        "reading_states",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("fraction", sa.Float(), nullable=False),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "work_id"),
    )
    op.create_index("ix_reading_states_work_id", "reading_states", ["work_id"])
    op.create_index("ix_reading_states_last_read", "reading_states", ["user_id", "last_read_at"])
    # Plain ADD COLUMN (nullable, no default), never a batch rebuild of book_files.
    op.add_column("book_files", sa.Column("pages", sa.Integer(), nullable=True))
    for name, table, cols in _INDEXES:
        op.create_index(name, table, cols)
    _backfill(op.get_bind())


def downgrade() -> None:
    for name, table, _cols in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
    op.drop_column("book_files", "pages")
    op.drop_index("ix_reading_states_last_read", table_name="reading_states")
    op.drop_index("ix_reading_states_work_id", table_name="reading_states")
    op.drop_table("reading_states")
