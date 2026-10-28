"""Full-text search over your own highlights and notes (``annotations_fts``, issue #72)

An SQLite FTS5 index over ``annotations.text`` and ``annotations.note`` (external content: the
text lives only in ``annotations``), kept in step by triggers, so every create, edit, and delete
from the KOReader sync (and a user's deletion, by cascade) updates it in the same transaction.
Filled from the existing rows here; ``lectern highlights-reindex`` rebuilds it.

A later migration that recreates ``annotations`` (a batch ALTER on SQLite) drops these
triggers with the old table and must create them again.

Revision ID: 0026_highlights_search
Revises: 0023_shelf_sync
Create Date: 2026-10-28

"""

from __future__ import annotations

from alembic import op

revision = "0026_highlights_search"
down_revision = "0023_shelf_sync"
branch_labels = None
depends_on = None

_DELETE = (
    "INSERT INTO annotations_fts (annotations_fts, rowid, text, note) "
    "VALUES ('delete', old.id, old.text, old.note);"
)
_INSERT = "INSERT INTO annotations_fts (rowid, text, note) VALUES (new.id, new.text, new.note);"


def upgrade() -> None:
    op.execute(
        "CREATE VIRTUAL TABLE annotations_fts USING fts5("
        "text, note, content='annotations', content_rowid='id', "
        "tokenize='unicode61 remove_diacritics 2')"
    )
    op.execute(f"CREATE TRIGGER annotations_fts_ai AFTER INSERT ON annotations BEGIN {_INSERT} END")
    op.execute(f"CREATE TRIGGER annotations_fts_ad AFTER DELETE ON annotations BEGIN {_DELETE} END")
    op.execute(
        "CREATE TRIGGER annotations_fts_au AFTER UPDATE OF text, note ON annotations "
        f"BEGIN {_DELETE} {_INSERT} END"
    )
    op.execute("INSERT INTO annotations_fts (annotations_fts) VALUES ('rebuild')")


def downgrade() -> None:
    for name in ("annotations_fts_au", "annotations_fts_ad", "annotations_fts_ai"):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.execute("DROP TABLE IF EXISTS annotations_fts")
