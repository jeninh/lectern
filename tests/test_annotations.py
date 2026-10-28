"""KOReader highlights and notes (issue #65, lectern.annotations): the plugin's sync route,
dedupe and edits across syncs and devices, placement on the work's text from the EPUB and
from another edition, privacy, the Markdown export, and searching them (issue #72).

Expected positions come from the fixture's own text (``STORY.index(phrase)``), never from the
code under test.
"""

from __future__ import annotations

import logging
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.conftest import (
    api_post,
    ko_headers,
    login,
    make_sync_key,
    make_sync_key_full,
    make_user,
)
from tests.test_jobs import EPUB, ingest_fixture

from lectern import annotations, search
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
from lectern.security.ratelimit import SlidingWindowLimiter

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


def test_a_replaced_device_does_not_keep_deleted_highlights_alive(client, owner, book):
    """Revoke a device, pair its replacement, delete there: gone, not held by the dead key."""
    doc = book["doc"]
    old = make_sync_key_full(client, owner["csrf"], "old kindle")
    old_ko = ko_headers("owner", old["secret"])
    a1, a2 = ann(PHRASE, "2026-10-01 09:30:00"), ann(OTHER, "2026-10-01 09:41:12")
    put(client, old_ko, doc, [a1, a2])
    r = api_post(client, f"/api/devices/sync-keys/{old['sync_key']['id']}/revoke", owner["csrf"])
    assert r.status_code == 200, r.text
    new_ko = ko_headers("owner", make_sync_key(client, owner["csrf"], "new kindle"))
    assert put(client, new_ko, doc, [a1, a2]).json()["added"] == 0
    assert put(client, new_ko, doc, [a2]).json()["deleted"] == 1
    assert [a["text"] for a in shown(client, book["work_id"])] == [OTHER]
    # Deleted there too: the new key held it (it synced a2 above), so it goes.
    assert put(client, new_ko, doc, []).json()["deleted"] == 1
    assert shown(client, book["work_id"]) == []
    # A live second device keeps its highlight when another one deletes it.
    put(client, book["kindle"], doc, [a1])
    put(client, book["kobo"], doc, [a1], device="Kobo")
    assert put(client, book["kindle"], doc, []).json()["deleted"] == 0
    assert len(shown(client, book["work_id"])) == 1


def test_a_new_device_never_deletes_highlights_it_did_not_have(client, owner, book):
    """Only a revoked device synced PHRASE: a new device's snapshot without it (it opened the
    book without the old sidecar) keeps it; Lectern may hold the only copy."""
    doc = book["doc"]
    old = make_sync_key_full(client, owner["csrf"], "old kindle")
    put(client, ko_headers("owner", old["secret"]), doc, [ann(PHRASE, "2026-10-01 09:30:00")])
    api_post(client, f"/api/devices/sync-keys/{old['sync_key']['id']}/revoke", owner["csrf"])
    new_ko = ko_headers("owner", make_sync_key(client, owner["csrf"], "new kobo"))
    r = put(client, new_ko, doc, [ann(OTHER, "2026-10-05 10:00:00")], device="Kobo").json()
    assert (r["added"], r["deleted"]) == (1, 0)
    assert put(client, new_ko, doc, []).json()["deleted"] == 1  # OTHER: it held that one
    assert [a["text"] for a in shown(client, book["work_id"])] == [PHRASE]
    # Another live device's highlight is not deleted by a device that never had it either.
    put(client, book["kindle"], doc, [ann(OTHER, "2026-10-06 10:00:00")])
    assert put(client, new_ko, doc, []).json()["deleted"] == 0
    assert len(shown(client, book["work_id"])) == 2


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


# --- searching your highlights (issue #72) -------------------------------------------------------


def found(http, q="", **params) -> list[dict]:
    r = http.get("/api/annotations/search", params={"q": q, "partial": "false", **params})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    return r.json()["results"]


def texts(http, q="", **params) -> list[str]:
    return [h["annotation"]["text"] for h in found(http, q, **params)]


def test_a_word_from_a_highlight_or_a_note_finds_it_and_edits_and_deletes_follow(
    client, book, caplog
):
    caplog.set_level(logging.DEBUG)
    doc, kindle = book["doc"], book["kindle"]
    a1 = ann(PHRASE, "2026-10-01 09:30:00")
    a2 = ann(OTHER, "2026-10-01 09:41:12", note="Remember the Café scene")
    put(client, kindle, doc, [a1, a2])

    # A word of the highlighted text: the hit says where the passage is, the word marked.
    [hit] = found(client, "counted")
    assert hit["work_id"] == book["work_id"] and hit["field"] == "text"
    assert hit["annotation"]["location"] == {"spine_index": 2, "char": STORY.index(PHRASE),
                                             "length": len(PHRASE)}  # fmt: skip
    assert ["counted", True] in hit["text_parts"]
    assert "".join(t for t, _ in hit["text_parts"]) == PHRASE
    assert hit["snippet"]["parts"] and hit["note_parts"] is None
    # A word of a note, case and accents folded both ways.
    [hit] = found(client, "CAFE")
    assert hit["annotation"]["text"] == OTHER and hit["field"] == "note"
    assert ["Café", True] in hit["note_parts"]
    # One field only: "Della" is in both highlights' text, never in a note.
    assert sorted(texts(client, "della")) == sorted([PHRASE, OTHER])
    assert texts(client, "note: della") == [] and texts(client, "highlight: cafe") == []
    assert texts(client, "note:remember") == [OTHER]
    # The phrase first, then passages with every word; a prefix while typing.
    assert texts(client, "della cry") == [OTHER]
    assert texts(client, "coun", partial="true") == [PHRASE]
    assert texts(client, "coun") == []

    # The note edited on the device: the old words no longer find it, the new ones do.
    edited = {**a2, "note": "rewritten entirely", "datetime_updated": "2026-10-02 08:00:00"}
    put(client, kindle, doc, [a1, edited])
    assert texts(client, "cafe") == [] and texts(client, "rewritten") == [OTHER]
    # Deleted on the device: gone from search.
    put(client, kindle, doc, [edited])
    assert texts(client, "counted") == [] and texts(client, "della") == [OTHER]

    # The rebuild command's work gives the same answers.
    with session_factory()() as db:
        assert annotations.reindex(db) == 1
        db.commit()
    assert texts(client, "rewritten") == [OTHER] and texts(client, "counted") == []
    # Lectern logs neither (the test client's own request log is not the server's).
    logged = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("lectern"))
    assert "rewritten" not in logged and "Café" not in logged


def test_search_is_only_your_own_highlights_on_books_you_can_see(client, owner, app, book):
    doc = book["doc"]
    put(client, book["kindle"], doc, [ann(PHRASE, "2026-10-01 09:30:00", note="owner secret")])
    rita = make_user(client, owner["csrf"], "rita", "reader")
    mia = make_user(client, owner["csrf"], "mia", "member")
    rita_http, mia_http = TestClient(app), TestClient(app)
    rita_csrf = login(rita_http, "rita", rita["password"])
    mia_csrf = login(mia_http, "mia", mia["password"])
    rita_ko = ko_headers("rita", make_sync_key(rita_http, rita_csrf, "rita's kobo"))
    mia_ko = ko_headers("mia", make_sync_key(mia_http, mia_csrf, "mia's kobo"))
    put(rita_http, rita_ko, doc, [ann(OTHER, "2026-10-05 10:00:00", note="rita secret")])
    put(mia_http, mia_ko, doc, [ann(PHRASE, "2026-10-06 10:00:00", note="mia secret")])

    # Each person finds only their own, though the words are in everyone's.
    assert [h["annotation"]["note"] for h in found(client, "secret")] == ["owner secret"]
    assert [h["annotation"]["note"] for h in found(mia_http, "secret")] == ["mia secret"]
    assert found(mia_http, "owner") == [] and found(client, "rita") == []
    # The reader cannot see the book: her own highlight in it is not found either.
    assert found(rita_http, "secret") == [] and found(rita_http) == []
    assert "rita secret" not in rita_http.get("/api/annotations.md", params={"q": "secret"}).text

    with session_factory()() as db:
        owner_id = db.scalar(select(User.id).where(User.username == "owner"))
        rita_id = db.scalar(select(User.id).where(User.username == "rita"))
        shelf = Shelf(owner_id=owner_id, name="For Rita")
        db.add(shelf)
        db.flush()
        db.add(ShelfItem(shelf_id=shelf.id, work_id=book["work_id"]))
        db.add(ShelfGrant(shelf_id=shelf.id, user_id=rita_id, permission=Perm.VIEW))
        db.commit()
    assert [h["annotation"]["note"] for h in found(rita_http, "secret")] == ["rita secret"]
    # Unshared from her: out of her results again.
    with session_factory()() as db:
        db.delete(db.scalar(select(ShelfGrant).where(ShelfGrant.user_id == rita_id)))
        db.commit()
    assert found(rita_http, "secret") == []
    # Trashed: out of its owner's results too. Without a session: nothing.
    with session_factory()() as db:
        from lectern.models import utcnow

        db.get(Work, book["work_id"]).trashed_at = utcnow()
        db.commit()
    assert found(client, "secret") == []
    assert TestClient(app).get("/api/annotations/search", params={"q": "x"}).status_code == 401


def test_highlights_page_filters_and_exports_what_it_shows(client, book):
    put(client, book["kindle"], book["doc"], [
        ann(PHRASE, "2026-09-30 22:00:00"),
        ann(OTHER, "2026-10-02 09:41:12", note="a note"),
    ])  # fmt: skip
    with session_factory()() as db:
        w = db.get(Work, book["work_id"])
        w.tags = ["christmas"]
        author = w.author
        db.commit()
    # No query: every highlight, in reading order, under the filters.
    assert texts(client) == [PHRASE, OTHER]
    assert texts(client, since="2026-10-01") == [OTHER]
    assert texts(client, until="2026-09-30") == [PHRASE]
    assert texts(client, since="2026-10-03") == []
    assert texts(client, author=author, tag="christmas", work=book["work_id"]) == [PHRASE, OTHER]
    assert texts(client, author="Somebody Else") == [] and texts(client, tag="summer") == []
    assert texts(client, work=book["work_id"] + 1) == []
    assert texts(client, "della", since="2026-10-01") == [OTHER]
    assert client.get("/api/annotations/search", params={"since": "Oct 1"}).status_code == 422
    r = client.get("/api/annotations/search", params={"limit": 1}).json()
    assert (len(r["results"]), r["total"], r["truncated"]) == (1, 2, True)
    assert client.get("/api/annotations").json()["works"][0]["tags"] == ["christmas"]

    # The export of the current results has exactly those.
    md = client.get("/api/annotations.md", params={"q": "della", "since": "2026-10-01"}).text
    assert f"> {OTHER}" in md and PHRASE not in md
    md = client.get("/api/annotations.md", params={"q": "nothing matches this"}).text
    assert "No highlights yet" in md and OTHER not in md


def test_highlight_search_limits_match_the_search_inside_books(client, app, book):
    secret = "x" * (search.MAX_QUERY_CHARS + 1)
    r = client.get("/api/annotations/search", params={"q": secret})
    assert r.status_code == 422 and r.json()["detail"] == "query_too_long"
    assert secret not in r.text
    r = client.get("/api/annotations/search", params={"q": " ".join("abcdefghijklm")})
    assert r.status_code == 422 and r.json()["detail"] == "too_many_words"
    # Its own budget per person: the palette's two searches do not halve each other's.
    app.state.limiters["highlights"] = SlidingWindowLimiter(limit=2, window_seconds=60)
    assert found(client, "della") == found(client, "della")
    r = client.get("/api/annotations/search", params={"q": "della"})
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    assert r.json()["detail"] == "too_many_searches"
    assert client.get("/api/annotations/search").status_code == 200  # listing is not a search
    assert client.get("/api/search/text", params={"q": "della"}).status_code == 200


# --- quotes feed and highlight of the day (issue #83) -------------------------------------------

THIRD = "So Della did it"
FOURTH = "A furnished flat at $8 per week"


def feed(http, **params) -> list[str]:
    r = http.get("/api/quotes", params=params)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    return [q["annotation"]["text"] for q in r.json()["results"]]


def today(http, day: str) -> dict:
    r = http.get("/api/quotes/today", params={"day": day})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    return r.json()


def mark(http, csrf, annotation_id, **body):
    return api_post(http, f"/api/annotations/{annotation_id}/mark", csrf, body)


def test_quotes_feed_orders_filters_and_follows_favourites_and_hides(client, owner, book):
    put(client, book["kindle"], book["doc"], [
        ann(OTHER, "2026-10-02 09:41:12", note="a note"),
        ann(PHRASE, "2026-09-30 22:00:00"),
        ann(THIRD, "2026-10-05 08:00:00"),
    ])  # fmt: skip
    # Newest first by KOReader's creation time (not reading order, not sync order).
    assert feed(client) == [THIRD, OTHER, PHRASE]
    # A shuffle is a stable permutation per seed, and pages through that one order.
    a = feed(client, order="shuffle", seed="abc")
    assert sorted(a) == sorted([THIRD, OTHER, PHRASE])
    assert feed(client, order="shuffle", seed="abc") == a
    pages = [*feed(client, order="shuffle", seed="abc", limit=2), *feed(
        client, order="shuffle", seed="abc", limit=2, offset=2)]  # fmt: skip
    assert pages == a
    assert {tuple(feed(client, order="shuffle", seed=str(s))) for s in range(12)} != {tuple(a)}
    # The highlights page's filters apply.
    assert feed(client, since="2026-10-01") == [THIRD, OTHER]

    ids = {h["annotation"]["text"]: h["annotation"]["id"] for h in found(client)}
    csrf = owner["csrf"]
    r = mark(client, csrf, ids[OTHER], favourite=True)
    assert r.status_code == 200 and r.json()["annotation"]["favourite"] is True
    assert mark(client, csrf, ids[THIRD], hidden=True).json()["annotation"]["hidden"] is True
    # Hidden ones leave the feed (and come back under show=hidden); favourites filter.
    assert feed(client) == [OTHER, PHRASE]
    assert feed(client, show="favourites") == [OTHER]
    assert feed(client, show="hidden") == [THIRD]
    # A mark only changes the fields sent; the search shows the marks too.
    mark(client, csrf, ids[OTHER], hidden=False)
    hit = next(h for h in found(client) if h["annotation"]["id"] == ids[OTHER])
    assert (hit["annotation"]["favourite"], hit["annotation"]["hidden"]) == (True, False)
    # A mark survives the next sync of the same highlight.
    put(client, book["kindle"], book["doc"], [
        ann(OTHER, "2026-10-02 09:41:12", note="an edited note", datetime_updated="2026-10-06 10:00:00"),
        ann(PHRASE, "2026-09-30 22:00:00"),
        ann(THIRD, "2026-10-05 08:00:00"),
    ])  # fmt: skip
    assert feed(client, show="favourites") == [OTHER] and feed(client, show="hidden") == [THIRD]
    # Writes need the CSRF token.
    r = client.post(f"/api/annotations/{ids[PHRASE]}/mark", json={"hidden": True})
    assert r.status_code == 403

    # By shelf: only works on a shelf you can see.
    with session_factory()() as db:
        owner_id = db.scalar(select(User.id).where(User.username == "owner"))
        on, off = Shelf(owner_id=owner_id, name="Christmas"), Shelf(owner_id=owner_id, name="Sea")
        db.add_all([on, off])
        db.flush()
        db.add(ShelfItem(shelf_id=on.id, work_id=book["work_id"]))
        db.commit()
        on_id, off_id = on.id, off.id
    assert feed(client, shelf=on_id) == [OTHER, PHRASE]
    assert feed(client, shelf=off_id) == []
    assert client.get("/api/quotes", params={"shelf": off_id + 100}).status_code == 404


def test_highlight_of_the_day_is_one_per_day_and_can_be_turned_off(client, owner, book):
    put(client, book["kindle"], book["doc"], [
        ann(PHRASE, "2026-09-30 22:00:00"),
        ann(OTHER, "2026-10-02 09:41:12"),
        ann(THIRD, "2026-10-05 08:00:00"),
        ann(FOURTH, "2026-10-06 08:00:00"),
    ])  # fmt: skip
    days = [f"2026-11-{d:02d}" for d in range(1, 31)]
    picks = [today(client, d)["quote"]["annotation"]["text"] for d in days]
    # The same all day (asking again, or after a re-sync that changes nothing)...
    put(client, book["kindle"], book["doc"], [
        ann(PHRASE, "2026-09-30 22:00:00"),
        ann(OTHER, "2026-10-02 09:41:12"),
        ann(THIRD, "2026-10-05 08:00:00"),
        ann(FOURTH, "2026-10-06 08:00:00"),
    ])  # fmt: skip
    assert [today(client, d)["quote"]["annotation"]["text"] for d in days] == picks
    # ...a different one the next day, and over a month more than a couple of them.
    assert all(a != b for a, b in zip(picks, picks[1:], strict=False))
    assert len(set(picks)) >= 3
    # Whose day it is comes from the app (the person's local date), so "today" is theirs.
    assert today(client, "2026-11-02")["day"] == "2026-11-02"
    assert client.get("/api/quotes/today", params={"day": "2026-13-40"}).status_code == 422
    assert client.get("/api/quotes/today", params={"day": "tomorrow"}).status_code == 422
    assert today(client, "2026-11-02")["quote"]["title"]

    # A hidden highlight is never the highlight of the day.
    ids = {h["annotation"]["text"]: h["annotation"]["id"] for h in found(client)}
    mark(client, owner["csrf"], ids[picks[0]], hidden=True)
    after = [today(client, d)["quote"]["annotation"]["text"] for d in days]
    assert picks[0] not in after
    # Only one left to show: that one, every day.
    for t in (PHRASE, OTHER, THIRD, FOURTH):
        mark(client, owner["csrf"], ids[t], hidden=t != FOURTH)
    assert {today(client, d)["quote"]["annotation"]["text"] for d in days[:3]} == {FOURTH}

    # Turned off: no card; on again: back.
    r = api_post(client, "/api/quotes/settings", owner["csrf"], {"daily": False})
    assert r.status_code == 200 and r.json() == {"daily": False}
    off = today(client, days[0])
    assert (off["enabled"], off["quote"]) == (False, None)
    assert client.get("/api/quotes").json()["daily"] is False
    api_post(client, "/api/quotes/settings", owner["csrf"], {"daily": True})
    assert today(client, days[0])["quote"]["annotation"]["text"] == FOURTH


def test_quotes_are_only_your_own_on_books_you_can_see(client, owner, app, book):
    doc = book["doc"]
    put(client, book["kindle"], doc, [ann(PHRASE, "2026-10-01 09:30:00", note="mine")])
    rita = make_user(client, owner["csrf"], "rita", "reader")
    mia = make_user(client, owner["csrf"], "mia", "member")
    rita_http, mia_http = TestClient(app), TestClient(app)
    rita_csrf = login(rita_http, "rita", rita["password"])
    mia_csrf = login(mia_http, "mia", mia["password"])
    rita_ko = ko_headers("rita", make_sync_key(rita_http, rita_csrf, "rita's kobo"))
    put(rita_http, rita_ko, doc, [ann(OTHER, "2026-10-05 10:00:00", note="rita's")])
    mine = found(client)[0]["annotation"]["id"]

    # A household member who sees the whole library sees none of the owner's highlights, and
    # cannot mark them (nor learn they exist).
    assert feed(mia_http) == [] and today(mia_http, "2026-11-01")["quote"] is None
    assert mark(mia_http, mia_csrf, mine, hidden=True).status_code == 404
    assert mark(mia_http, mia_csrf, mine + 999, hidden=True).status_code == 404
    assert feed(client) == [PHRASE]
    # A reader without the book: her own highlight stays out of her feed and her Home.
    assert feed(rita_http) == [] and today(rita_http, "2026-11-01")["quote"] is None
    with session_factory()() as db:
        owner_id = db.scalar(select(User.id).where(User.username == "owner"))
        rita_id = db.scalar(select(User.id).where(User.username == "rita"))
        shelf = Shelf(owner_id=owner_id, name="For Rita")
        db.add(shelf)
        db.flush()
        db.add(ShelfItem(shelf_id=shelf.id, work_id=book["work_id"]))
        db.add(ShelfGrant(shelf_id=shelf.id, user_id=rita_id, permission=Perm.VIEW))
        db.commit()
        shelf_id = shelf.id
    # Shared: hers, never the owner's.
    assert feed(rita_http) == [OTHER] and feed(rita_http, shelf=shelf_id) == [OTHER]
    assert today(rita_http, "2026-11-01")["quote"]["annotation"]["note"] == "rita's"
    assert mark(rita_http, rita_csrf, mine, favourite=True).status_code == 404
    # Unshared again: out of her feed and Home, and she cannot mark it; the shelf is gone too.
    with session_factory()() as db:
        db.delete(db.scalar(select(ShelfGrant).where(ShelfGrant.user_id == rita_id)))
        db.commit()
    assert feed(rita_http) == [] and today(rita_http, "2026-11-01")["quote"] is None
    assert rita_http.get("/api/quotes", params={"shelf": shelf_id}).status_code == 404
    theirs = rita_http.get("/api/quotes", params={"show": "hidden"}).json()["results"]
    assert theirs == []
    anon = TestClient(app)
    assert anon.get("/api/quotes").status_code == 401
    assert anon.get("/api/quotes/today").status_code == 401
