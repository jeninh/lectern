"""KOReader highlights and notes (issue #65, lectern.annotations): the plugin's sync route,
dedupe and edits across syncs and devices, placement on the work's text from the EPUB and
from another edition, privacy, and the Markdown export.

Expected positions come from the fixture's own text (``STORY.index(phrase)``), never from the
code under test.
"""

from __future__ import annotations

import logging
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.conftest import ko_headers, login, make_sync_key, make_user
from tests.test_jobs import EPUB, ingest_fixture

from lectern.db import session_factory
from lectern.ingest.epub import read_spine
from lectern.ingest.sentences import ensure_text_sentences
from lectern.models import (
    Annotation,
    BookFile,
    FileKind,
    Sentence,
    Shelf,
    ShelfGrant,
    ShelfItem,
    User,
    Work,
)
from lectern.models import ShelfPermission as Perm
from lectern.positions import TextPos, normalize_spine_text, text_to_xpointer

with zipfile.ZipFile(EPUB) as _zf:
    STORY_XHTML = _zf.read(read_spine(EPUB)[2])
STORY = normalize_spine_text(STORY_XHTML)

PHRASE = "Three times Della counted it"
OTHER = "Della finished her cry"


def xp(phrase: str) -> str:
    return text_to_xpointer(TextPos(2, STORY.index(phrase)), STORY_XHTML)


def ann(phrase: str, created: str, **kw) -> dict:
    return {
        "datetime": created,
        "text": phrase,
        "chapter": "The Gift of the Magi",
        "pos0": xp(phrase),
        "pos1": xp(phrase),
        "pageno": 3,
        "drawer": "lighten",
        "color": "yellow",
        **kw,
    }


def put(client, ko, document, items, device="Kindle"):
    return client.put(
        "/koplugin/annotations",
        json={"document": document, "device": device, "annotations": items},
        headers=ko,
    )


@pytest.fixture
def book(client, owner, settings, tmp_path) -> dict:
    work_id = ingest_fixture(settings, tmp_path, audio=False)
    with session_factory()() as db:
        assert ensure_text_sentences(db, settings.library_dir, work_id)  # as the watcher does
        db.commit()
        doc = db.scalar(select(BookFile.partial_md5).where(BookFile.work_id == work_id))
    return {
        "work_id": work_id,
        "doc": doc,
        "kindle": ko_headers("owner", make_sync_key(client, owner["csrf"], "kindle")),
        "kobo": ko_headers("owner", make_sync_key(client, owner["csrf"], "kobo")),
    }


def shown(client, work_id) -> list[dict]:
    r = client.get(f"/api/works/{work_id}/annotations")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    return r.json()["annotations"]


def test_highlight_and_note_show_on_the_book_placed_on_its_text(client, book, caplog):
    caplog.set_level(logging.DEBUG)
    note = "She saved every penny — remember this."
    r = put(client, book["kindle"], book["doc"], [
        ann(PHRASE, "2026-10-01 09:30:00"),
        ann(OTHER, "2026-10-01 09:41:12", note=note),
    ])  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json() == {"document": book["doc"], "added": 2, "updated": 0, "deleted": 0,
                        "total": 2}  # fmt: skip
    got = shown(client, book["work_id"])
    # In reading order (OTHER comes later in the story), wherever they were sent.
    assert STORY.index(PHRASE) < STORY.index(OTHER)
    assert [a["text"] for a in got] == [PHRASE, OTHER]
    assert got[1]["note"] == note and got[0]["note"] is None
    for a, phrase in zip(got, (PHRASE, OTHER), strict=True):
        assert a["location"] == {"spine_index": 2, "char": STORY.index(phrase),
                                 "length": len(phrase)}  # fmt: skip
        char = STORY.index(phrase)
        with session_factory()() as db:
            n = db.scalar(
                select(Sentence.n).where(
                    Sentence.work_id == book["work_id"],
                    Sentence.spine_index == 2,
                    Sentence.char_start <= char,
                    Sentence.char_end > char,
                )
            )
        assert n is not None and a["sentence_n"] == n
        assert a["chapter"] == "The Gift of the Magi" and a["device"] == "Kindle"
    # Private-source data: nothing about them in the log stream.
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert PHRASE not in logged and "every penny" not in logged


def test_resyncs_and_a_second_device_store_one_copy_and_edits_and_deletes_follow(client, book):
    doc, kindle, kobo = book["doc"], book["kindle"], book["kobo"]
    a1 = ann(PHRASE, "2026-10-01 09:30:00")
    a2 = ann(OTHER, "2026-10-01 09:41:12", note="first note")
    assert put(client, kindle, doc, [a1, a2]).json()["added"] == 2
    again = put(client, kindle, doc, [a1, a2]).json()
    assert (again["added"], again["updated"], again["deleted"]) == (0, 0, 0)
    # The same sidecar copied to a Kobo: still one copy each.
    assert put(client, kobo, doc, [a1, a2], device="Kobo").json()["added"] == 0
    with session_factory()() as db:
        assert len(db.scalars(select(Annotation)).all()) == 2

    # The note edited on the Kindle; the Kobo's older copy does not undo it.
    edited = {**a2, "note": "second thoughts", "datetime_updated": "2026-10-02 08:00:00"}
    assert put(client, kindle, doc, [a1, edited]).json()["updated"] == 1
    put(client, kobo, doc, [a1, a2], device="Kobo")
    assert [a["note"] for a in shown(client, book["work_id"])] == [None, "second thoughts"]

    # Deleted on the Kindle while the Kobo still has it: kept. Deleted there too: gone.
    assert put(client, kindle, doc, [edited]).json()["deleted"] == 0
    assert len(shown(client, book["work_id"])) == 2
    assert put(client, kobo, doc, [a2], device="Kobo").json()["deleted"] == 1
    assert [a["text"] for a in shown(client, book["work_id"])] == [OTHER]
    # An empty snapshot (every highlight removed) clears the rest.
    put(client, kindle, doc, [])
    put(client, kobo, doc, [], device="Kobo")
    assert shown(client, book["work_id"]) == []


def test_a_highlight_from_another_edition_lands_on_the_same_passage(client, book, settings):
    """A PDF of the same work (page and text only, laid out differently): the highlight is
    placed on the EPUB's text the reader opens, where the EPUB highlight of it is."""
    with session_factory()() as db:
        work = db.get(Work, book["work_id"])
        pdf = settings.library_dir / work.dir_path / "book.pdf"
        pdf.write_bytes(b"%PDF-1.4 stand-in")
        db.add(BookFile(work_id=work.id, kind=FileKind.EBOOK, rel_path="book.pdf",
                        media_type="application/pdf", size=17, sha256="f" * 64,
                        partial_md5="f" * 32, pages=10))  # fmt: skip
        db.commit()
    # PDF text extraction: line breaks and different case inside the passage.
    pdf_text = "Della  finished\nher CRY"
    r = put(client, book["kindle"], "f" * 32, [
        {"datetime": "2026-10-03 21:00:00", "text": pdf_text, "pos0": "4:72.0:310.5",
         "pos1": "4:200.1:322.0", "page": 4, "pageno": 4},
    ])  # fmt: skip
    assert r.status_code == 200, r.text
    put(client, book["kindle"], book["doc"], [ann(OTHER, "2026-10-01 09:41:12")])
    got = shown(client, book["work_id"])
    assert len(got) == 2
    assert got[0]["location"] == got[1]["location"]
    assert got[0]["location"]["char"] == STORY.index(OTHER)
    pdf_one = next(a for a in got if a["file"]["media_type"] == "application/pdf")
    assert pdf_one["page"] == 4 and pdf_one["text"] == pdf_text


def test_highlights_are_private_and_follow_work_visibility(client, owner, app, book):
    doc = book["doc"]
    put(client, book["kindle"], doc, [ann(PHRASE, "2026-10-01 09:30:00", note="mine")])
    rita = make_user(client, owner["csrf"], "rita", "reader")
    mia = make_user(client, owner["csrf"], "mia", "member")
    rita_http, mia_http = TestClient(app), TestClient(app)
    rita_csrf = login(rita_http, "rita", rita["password"])
    login(mia_http, "mia", mia["password"])
    rita_ko = ko_headers("rita", make_sync_key(rita_http, rita_csrf, "rita's kobo"))

    # A household member who sees the whole library never sees someone else's highlights.
    assert shown(mia_http, book["work_id"]) == []
    assert mia_http.get("/api/annotations").json()["total"] == 0
    assert "mine" not in mia_http.get("/api/annotations.md").text
    # A reader without the book: 404, and her own synced highlights stay hidden.
    assert rita_http.get(f"/api/works/{book['work_id']}/annotations").status_code == 404
    assert rita_http.get(f"/api/works/{book['work_id']}/annotations.md").status_code == 404
    r = put(rita_http, rita_ko, doc, [ann(OTHER, "2026-10-05 10:00:00", note="rita's")])
    assert r.status_code == 200 and r.json()["added"] == 1  # the answer reveals nothing more
    assert rita_http.get("/api/annotations").json()["works"] == []

    # Shared with her: she sees her own highlight only. Unshared: it leaves her view again.
    with session_factory()() as db:
        owner_id = db.scalar(select(User.id).where(User.username == "owner"))
        rita_id = db.scalar(select(User.id).where(User.username == "rita"))
        shelf = Shelf(owner_id=owner_id, name="For Rita")
        db.add(shelf)
        db.flush()
        db.add(ShelfItem(shelf_id=shelf.id, work_id=book["work_id"]))
        db.add(ShelfGrant(shelf_id=shelf.id, user_id=rita_id, permission=Perm.VIEW))
        db.commit()
    assert [a["note"] for a in shown(rita_http, book["work_id"])] == ["rita's"]
    assert [a["note"] for a in shown(client, book["work_id"])] == ["mine"]
    with session_factory()() as db:
        db.delete(db.scalar(select(ShelfGrant).where(ShelfGrant.user_id == rita_id)))
        db.commit()
    assert rita_http.get(f"/api/works/{book['work_id']}/annotations").status_code == 404
    assert rita_http.get("/api/annotations").json()["total"] == 0
    assert "rita's" not in rita_http.get("/api/annotations.md").text

    # Not over the catalogue password or without a session, and not with a wrong sync key.
    anon = TestClient(app)
    assert anon.get("/api/annotations").status_code == 401
    assert put(anon, ko_headers("owner", "wrong"), doc, []).status_code == 401
    # Trashed: hidden from its owner too.
    with session_factory()() as db:
        from lectern.models import utcnow

        db.get(Work, book["work_id"]).trashed_at = utcnow()
        db.commit()
    assert client.get("/api/annotations").json()["total"] == 0


def test_markdown_export_matches_what_is_shown(client, book):
    put(client, book["kindle"], book["doc"], [
        ann(OTHER, "2026-10-01 09:41:12", note="line one\nline two"),
        ann(PHRASE, "2026-10-01 09:30:00"),
    ])  # fmt: skip
    got = shown(client, book["work_id"])
    r = client.get(f"/api/works/{book['work_id']}/annotations.md")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-disposition"].endswith('.md"')
    md = r.text
    assert md.startswith("# The Gift of the Magi\n")
    # Every highlight and note, in the order the app shows them, with page and time.
    at = [md.index(f"> {a['text']}") for a in got]
    assert at == sorted(at)
    assert "**Note:** line one\nline two" in md
    assert "*Page 3 · 2026-10-01 09:30*" in md
    assert md.count("## The Gift of the Magi") == 1  # the chapter heading, once

    everything = client.get("/api/annotations.md").text
    assert everything.startswith("# Highlights\n") and "## The Gift of the Magi" in everything
    assert all(f"> {a['text']}" in everything for a in got)
    summary = client.get("/api/annotations").json()
    assert summary["total"] == 2 and summary["works"][0]["work_id"] == book["work_id"]


def test_bad_payloads_are_refused_without_echoing_them(client, book):
    secret_text = "do not echo me"
    r = put(client, book["kindle"], "../etc", [ann(PHRASE, "2026-10-01 09:30:00")])
    assert r.status_code == 422
    r = put(client, book["kindle"], book["doc"], [{"text": secret_text, "pos0": "x"}])
    assert r.status_code == 422 and secret_text not in r.text
