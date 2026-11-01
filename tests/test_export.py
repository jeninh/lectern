"""Full personal-data export (lectern.export, issue #139): one streamed zip, own data only,
no secrets, bounded memory. Uses the guests household: the owner, and guest "cousin" who sees
only "Public Book" (so a guest's export must leave out every other book and every other person)."""

from __future__ import annotations

import csv
import io
import json
import os
import tracemalloc
import zipfile
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select
from tests.conftest import make_sync_key
from tests.test_guests import world  # noqa: F401  (fixture)

from lectern.db import session_factory
from lectern.models import (
    Annotation,
    BookFile,
    BookReview,
    ListeningQueueItem,
    ReadingState,
    ReadingStatsDay,
    ReadThrough,
    Shelf,
    ShelfItem,
    User,
    VocabularyWord,
    WantToReadItem,
)

NOW = datetime(2026, 5, 1, 12, tzinfo=UTC)


def seed(db, user_id: int, work_id: int, tag: str, n: int = 1) -> None:
    """One of everything for ``user_id`` on a work, its text carrying ``tag``."""
    doc = db.scalar(select(BookFile.partial_md5).where(BookFile.work_id == work_id))
    db.execute(
        insert(Annotation),
        [
            {
                "user_id": user_id, "document": doc, "key": f"{tag}-{work_id}-{i}", "text": f"quote {tag} {i}",
                "note": f"note {tag}", "chapter": "Ch 1", "pos0": "x", "created": "2026-05-01 10:00:00",
                "updated": "2026-05-01 10:00:00", "device": "", "sources": [],
                "synced_at": NOW, "favourite": False, "hidden": False,
            }
            for i in range(n)
        ],
    )  # fmt: skip
    db.add_all(
        [
            BookReview(user_id=user_id, work_id=work_id, rating=7, notes=f"# Review {tag}"),
            ReadThrough(user_id=user_id, work_id=work_id, started_at=NOW, finished_at=NOW),
            ReadingState(user_id=user_id, work_id=work_id, fraction=0.5, last_read_at=NOW),
            ReadingStatsDay(user_id=user_id, work_id=work_id, day="2026-05-01",
                            read_seconds=90, listened_seconds=30, pages=2.5),
            VocabularyWord(user_id=user_id, work_id=work_id, lemma=f"word{tag}{work_id}", lemma_key=f"word{tag}{work_id}",
                           word=f"word{tag}{work_id}", definition=f"def {tag}", context=f"ctx {tag}"),
            WantToReadItem(user_id=user_id, work_id=work_id, position=0),
            ListeningQueueItem(user_id=user_id, work_id=work_id, position=0),
        ]
    )  # fmt: skip
    shelf = Shelf(owner_id=user_id, name=f"shelf {tag} {work_id}")
    shelf.items.append(ShelfItem(work_id=work_id, position=0))
    db.add(shelf)


def open_zip(r) -> zipfile.ZipFile:
    assert r.status_code == 200, r.text
    return zipfile.ZipFile(io.BytesIO(r.content))


def everything(z: zipfile.ZipFile) -> str:
    return "\n".join(z.read(n).decode() for n in z.namelist())


def test_owner_archive_has_every_format_and_only_their_rows(world):  # noqa: F811
    w = world
    with session_factory()() as db:
        owner_id = db.scalar(select(User.id).where(User.username == "owner"))
        for title, wid in w.works.items():
            seed(db, owner_id, wid, "OWN" + title.split()[0])
        for wid in w.works.values():
            seed(db, w.uid, wid, "GUESTROW")
        db.commit()
    r = w.owner.get("/api/export")
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-disposition"].startswith('attachment; filename="lectern-export-')
    z = open_zip(r)
    assert z.testzip() is None
    assert set(z.namelist()) >= {
        "README.md", "data.json", "csv/annotations.csv", "csv/reviews.csv", "csv/vocabulary.csv",
        "csv/reading_stats_days.csv", "csv/shelves.csv", "csv/want_to_read.csv",
        "csv/listening_queue.csv", "markdown/highlights.md", "markdown/notes.md",
    }  # fmt: skip
    text = everything(z)
    assert "GUESTROW" not in text and "cousin" not in text
    data = json.loads(z.read("data.json"))
    assert data["schema_version"] == 1 and data["user"]["username"] == "owner"
    t = data["tables"]
    assert len(t["annotations"]) == 4 and len(t["vocabulary"]) == 4 and len(t["shelves"]) >= 4
    assert t["reading_stats_days"][0]["read_seconds"] == 90  # the rollups, not raw activity
    review = next(x for x in t["reviews"] if x["title"] == "Public Book")
    assert (review["rating"], review["stars"], review["notes"]) == (7, 3.5, "# Review OWNPublic")
    rows = list(csv.DictReader(io.StringIO(z.read("csv/annotations.csv").decode())))
    assert len(rows) == 4 and {r["title"] for r in rows} == set(w.works)
    md = z.read("markdown/highlights.md").decode()
    assert (
        "## Public Book" in md and "> quote OWNPublic 0" in md and "**Note:** note OWNPublic" in md
    )
    assert "Rating: 3.5 / 5" in z.read("markdown/notes.md").decode()


def test_guest_gets_only_their_own_rows_on_books_they_can_see(world):  # noqa: F811
    w = world
    with session_factory()() as db:
        owner_id = db.scalar(select(User.id).where(User.username == "owner"))
        for wid in w.works.values():
            seed(db, owner_id, wid, "OWNERROW")
            seed(db, w.uid, wid, "GUESTROW")
        db.commit()
    z = open_zip(w.gc.get("/api/export"))
    text = everything(z)
    assert "OWNERROW" not in text
    data = json.loads(z.read("data.json"))
    assert data["user"]["username"] == "cousin"
    visible = {"Public Book", "Kids Book"}  # Kids is on a sub-shelf of the granted one
    assert {a["title"] for a in data["tables"]["annotations"]} == visible
    assert "Secret Book" not in text and "Loose Book" not in text
    words = data["tables"]["vocabulary"]
    # Their words on hidden books stay, without the book or its context.
    assert len(words) == 4
    assert {v["title"] for v in words} == visible | {None}
    assert all(v["context"] is None and v["work_id"] is None for v in words if v["title"] is None)
    assert all(v["context"] == "ctx GUESTROW" for v in words if v["title"])


def test_no_secret_is_in_the_archive(world):  # noqa: F811
    w = world
    sync_secret = make_sync_key(w.owner, w.csrf, "Kobo")
    with session_factory()() as db:
        owner = db.scalar(select(User).where(User.username == "owner"))
        owner.totp_secret = "JBSWY3DPEHPK3PXP"
        db.commit()
        secrets = [owner.password_hash, owner.totp_secret, sync_secret]
        secrets += [db.scalar(select(User.password_hash).where(User.id == w.uid))]
    z = open_zip(w.owner.get("/api/export"))
    blob = b"\n".join(z.read(n) for n in z.namelist())
    for s in secrets:
        assert s and s.encode() not in blob
    for name in (b"password_hash", b"totp_secret", b"token_hash", b"sync_key", b"sources"):
        assert name not in blob


def test_export_needs_a_session(client):
    assert client.get("/api/export", follow_redirects=False).status_code in (303, 401)


@pytest.mark.parametrize("cell", ["=1+1", "+1", "-1+2", "@SUM(A1)"])
def test_csv_cells_cannot_start_a_formula(world, cell):  # noqa: F811
    w = world
    with session_factory()() as db:
        db.add(VocabularyWord(user_id=w.uid, lemma="a", lemma_key="a", word=cell, definition="d"))
        db.commit()
    z = open_zip(w.gc.get("/api/export"))
    rows = list(csv.DictReader(io.StringIO(z.read("csv/vocabulary.csv").decode())))
    assert rows[0]["word"] == "'" + cell
    assert json.loads(z.read("data.json"))["tables"]["vocabulary"][0]["word"] == cell


def test_large_export_streams_in_bounded_memory(world):  # noqa: F811
    """~15 MB of highlights (written three times: JSON, CSV, Markdown) goes
    through with a traced peak far below the archive's size."""
    from lectern.export import stream_archive

    w = world
    rows, size = 12_000, 1_200
    with session_factory()() as db:
        owner_id = db.scalar(select(User.id).where(User.username == "owner"))
        doc = db.scalar(
            select(BookFile.partial_md5).where(BookFile.work_id == w.works["Public Book"])
        )
        for start in range(0, rows, 2_000):
            db.execute(
                insert(Annotation),
                [
                    {
                        "user_id": owner_id, "document": doc, "key": f"big-{i}",
                        "text": os.urandom(size // 2).hex(), "note": None, "pos0": "x",
                        "created": f"2026-05-01 10:{i % 60:02d}:00", "updated": "2026-05-01 10:00:00",
                        "device": "", "sources": [], "synced_at": NOW, "favourite": False,
                        "hidden": False,
                    }
                    for i in range(start, start + 2_000)
                ],
            )  # fmt: skip
        db.commit()
    total = peak = 0
    tracemalloc.start()
    try:
        for chunk in stream_archive(session_factory(), owner_id):
            total += len(chunk)
            assert len(chunk) < 2 * 1024 * 1024
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert total > 15 * 1024 * 1024
    assert peak < 8 * 1024 * 1024, (peak, total)
