"""Full-text search inside books: the index, its upkeep, locations, and who sees what."""

from __future__ import annotations

import sqlite3
import zlib
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import settings as settings_
from hypothesis import strategies as st
from tests.conftest import (
    OWNER_PASSWORD,
    api_delete,
    api_post,
    drain_inbox,
    login,
    make_epub,
    make_user,
    put_json,
)

from lectern import search
from lectern.db import session_factory
from lectern.positions import normalize_spine_text
from lectern.security.ratelimit import SlidingWindowLimiter

MOBY_1 = "<h1>Loomings</h1><p>Call me Ishmael. Some years ago, never mind how long precisely.</p>"
MOBY_2 = (
    "<p>It is a way I have of driving off the spleen.</p>"
    "<p>Whenever I find myself growing <em>grim</em> about the mouth, I account it high time "
    "to get to sea as soon as I can. The café on the quay sold crème brûlée.</p>"
)


@pytest.fixture(autouse=True)
def inbox(settings):
    settings.inbox_path.mkdir(parents=True, exist_ok=True)


def reindex(settings) -> dict:
    return search.reconcile(session_factory(), settings, budget_s=None)


def find(client, q: str, **params):
    r = client.get("/api/search/text", params={"q": q, **params})
    assert r.status_code == 200, r.text
    return r.json()


def ids(client) -> dict[str, int]:
    return {w["title"]: w["id"] for w in client.get("/api/library").json()["works"]}


def fts_rows(settings, q: str) -> list[int]:
    """Work ids the raw index has for ``q``, whoever may see them."""
    con = search.connect(settings.data_dir / "search.db")
    try:
        return sorted(
            r[0]
            for r in con.execute(
                "SELECT DISTINCT c.work_id FROM fts JOIN chunks c ON c.id = fts.rowid "
                "WHERE fts MATCH ?",
                (q,),
            )
        )
    finally:
        con.close()


def text_pdf(path: Path, pages: list[str]) -> Path:
    """A PDF with a real text layer (Helvetica), one string per page."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]  # fmt: skip
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode()}\nendstream")
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {len(objs)} 0 R "
            "/Resources << /Font << /F1 3 0 R >> >> >>"
        )
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return path


@pytest.fixture
def moby(client, owner, settings) -> int:
    make_epub(settings.inbox_path / "moby.epub", title="Moby-Dick", authors=["Herman Melville"],
              chapters=[MOBY_1, MOBY_2])  # fmt: skip
    drain_inbox(settings)
    reindex(settings)
    return ids(client)["Moby-Dick"]


def test_a_phrase_returns_a_snippet_and_the_reader_location(client, owner, settings, moby):
    found = find(client, "grim about the mouth", partial=False)
    assert found["too_broad"] is False
    (hit,) = found["results"]
    assert (hit["work_id"], hit["title"], hit["author"]) == (moby, "Moby-Dick", "Herman Melville")
    assert hit["format"] == "epub" and hit["spine_index"] == 1
    # The location is on the reader's text timeline: the same string sits at that offset.
    text = normalize_spine_text(f"<html><body>{MOBY_2}</body></html>".encode())
    assert text[hit["char"] : hit["char"] + hit["length"]] == "grim about the mouth"
    assert hit["char"] == text.index("grim about the mouth")
    assert hit["match"] == "grim about the mouth"
    # The snippet: context on both sides, only the phrase marked, cut at a word.
    parts = hit["snippet"]["parts"]
    assert [p for p, m in parts if m] == ["grim about the mouth"]
    # Cut before (at most 60 characters back, at a word), not after (the passage ends within
    # reach). Snippets read with a space between paragraphs, where the timeline has none.
    assert hit["snippet"]["before"] is True and hit["snippet"]["after"] is False
    joined = "".join(p for p, _ in parts)
    assert joined == "of driving off the spleen. Whenever I find myself growing grim about the " \
        "mouth, I account it high time to get to sea as soon as I can. The café on the quay " \
        "sold crème brûlée."  # fmt: skip
    assert "spleen.Whenever" in text


def test_typing_accents_and_words_anywhere(client, owner, settings, moby):
    # The last word matches as a prefix while it is being typed.
    (hit,) = find(client, "call me ishm")["results"]
    # "LoomingsCall me…" on the timeline: the heading and the paragraph have no space between
    timeline = normalize_spine_text(f"<html><body>{MOBY_1}</body></html>".encode())
    assert timeline.index("Call me Ishmael") == 8
    assert (hit["spine_index"], hit["char"], hit["length"]) == (0, 8, 15)
    assert hit["match"] == "Call me Ishmael"
    assert find(client, "call me ishm", partial=False)["results"] == []
    # a prefix of three letters is a word, not a prefix: "the*" would read half the index
    assert find(client, "call me ish")["results"] == []
    # Diacritics fold both ways.
    (hit,) = find(client, "cafe creme")["results"]
    assert hit["match"] == "café"
    # a decomposed accent (e + U+0301) is the same word, not two
    assert find(client, "cre\u0301me bru\u0302le\u0301e")["results"][0]["match"] == "crème brûlée"
    # Not a phrase, but every word in one passage: still found, after phrases.
    (hit,) = find(client, "spleen driving")["results"]
    assert hit["spine_index"] == 1 and hit["match"] == "driving"
    # FTS syntax in a query is just words.
    for q in ('"ishmael', "ishmael*)", "(ishmael", "^ishmael:", "-ishmael"):
        assert find(client, q)["results"][0]["match"] == "Ishmael", q
    assert find(client, "ishmael OR queequeg")["results"] == []  # OR is a word here
    assert find(client, "zzzz")["results"] == []
    assert find(client, "a")["results"] == []


def test_query_limits_rate_limit_and_sign_in(client, owner, settings, moby):
    r = client.get("/api/search/text", params={"q": "x" * 201})
    assert (r.status_code, r.json()["detail"]) == (422, "query_too_long")
    r = client.get("/api/search/text", params={"q": " ".join(["word"] * 13)})
    assert (r.status_code, r.json()["detail"]) == (422, "too_many_words")
    assert client.get("/api/search/text", params={"q": "x" * 401}).status_code == 422
    client.app.state.limiters["search"] = SlidingWindowLimiter(limit=2, window_seconds=60)
    assert find(client, "ishmael")["results"]
    assert find(client, "ishmael")["results"]
    r = client.get("/api/search/text", params={"q": "ishmael"})
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    client.post("/logout", headers={"origin": "http://testserver"})
    client.cookies.clear()
    assert client.get("/api/search/text", params={"q": "ishmael"}).status_code == 401


def test_a_search_over_its_time_budget_is_too_broad(client, owner, settings, moby):
    settings.search_time_budget_ms = 0
    assert find(client, "ishmael") == {"results": [], "too_broad": True}


def test_visibility_readers_members_and_the_trash(client, owner, settings):
    csrf = owner["csrf"]
    for name, words in (("Shared", "lighthouse keeper"), ("Hidden", "lighthouse secret")):
        make_epub(settings.inbox_path / f"{name}.epub", title=name,
                  chapters=[f"<p>The {words} waited by the shore.</p>"])  # fmt: skip
    drain_inbox(settings)
    reindex(settings)
    w = ids(client)
    reader = make_user(client, csrf, "rex", "reader")
    member = make_user(client, csrf, "mia", "member")
    shelf = api_post(client, "/api/shelves", csrf, {"name": "Kids"}).json()["id"]
    put_json(client, f"/api/shelves/{shelf}/items", csrf, {"work_ids": [w["Shared"]]})
    put_json(client, f"/api/shelves/{shelf}/grants/{reader['id']}", csrf, {"permission": "view"})
    # Both are in the index; only what each person may see comes back.
    assert fts_rows(settings, "lighthouse") == sorted(w.values())

    login(client, "rex", reader["password"])
    assert [h["work_id"] for h in find(client, "lighthouse")["results"]] == [w["Shared"]]
    # A word only the hidden book has answers exactly like a word no book has: no hits, no
    # counts, nothing else in the response.
    assert find(client, "secret") == find(client, "nosuchwordanywhere")
    assert find(client, "secret") == {"results": [], "too_broad": False}
    assert find(client, "lighthouse secret")["results"] == []

    login(client, "mia", member["password"])
    assert sorted(h["work_id"] for h in find(client, "lighthouse")["results"]) == sorted(w.values())

    # Trashed: gone from results at once (before the indexer has run), for the owner too...
    csrf = login(client, "owner", OWNER_PASSWORD)
    assert api_post(client, f"/api/works/{w['Hidden']}/trash", csrf).status_code == 200
    assert [h["work_id"] for h in find(client, "lighthouse")["results"]] == [w["Shared"]]
    assert fts_rows(settings, "secret") == [w["Hidden"]]
    # ...and out of the index at the next pass.
    assert reindex(settings) == {"removed": 1, "indexed": 0, "pending": 0}
    assert fts_rows(settings, "secret") == []
    # A reader's shelf losing the book takes it out of their results, nothing to reindex.
    put_json(client, f"/api/shelves/{shelf}/items", csrf, {"work_ids": []})
    login(client, "rex", reader["password"])
    assert find(client, "lighthouse")["results"] == []

    # Restore: indexed again and found again.
    csrf = login(client, "owner", OWNER_PASSWORD)
    assert api_post(client, f"/api/trash/works/{w['Hidden']}/restore", csrf).status_code == 200
    assert reindex(settings)["indexed"] == 1
    assert [h["work_id"] for h in find(client, "secret")["results"]] == [w["Hidden"]]
    # Deleting for good removes it for good.
    api_post(client, f"/api/works/{w['Hidden']}/trash", csrf)
    assert api_delete(client, f"/api/trash/works/{w['Hidden']}", csrf).status_code == 204
    reindex(settings)
    assert fts_rows(settings, "secret") == []


def test_edits_reindex_and_reindexing_is_idempotent(client, owner, settings, moby):
    con = search.connect(settings.data_dir / "search.db")
    try:
        before = con.execute("SELECT id, work_id, loc, char_start, z FROM chunks").fetchall()
    finally:
        con.close()
    assert before
    # Nothing changed: a pass does nothing, and the chunks are exactly the same rows.
    assert reindex(settings) == {"removed": 0, "indexed": 0, "pending": 0}
    # Forcing a rebuild of the same files gives the same chunks again.
    with session_factory()() as db:
        want = search.wanted(db)[moby]
    con = search.connect(settings.data_dir / "search.db")
    try:
        search.index_work(con, settings, moby, want)
        search.index_work(con, settings, moby, want)
        after = con.execute("SELECT work_id, loc, char_start, z FROM chunks").fetchall()
        con.execute("INSERT INTO fts (fts, rank) VALUES ('integrity-check', 1)")
    finally:
        con.close()
    assert after == [r[1:] for r in before]
    assert len(find(client, "ishmael")["results"]) == 1

    # A second EPUB made primary: its text replaces the first one's.
    make_epub(settings.inbox_path / "moby2.epub", title="Moby-Dick", authors=["Herman Melville"],
              chapters=["<p>Queequeg was a native of Rokovoko.</p>"])  # fmt: skip
    drain_inbox(settings)
    ed = client.get(f"/api/works/{moby}/edit").json()
    assert len(ed["files"]) == 2
    assert reindex(settings) == {"removed": 0, "indexed": 0, "pending": 0}  # still the first
    second = next(f for f in ed["files"] if not f["primary"])
    r = client.patch(f"/api/works/{moby}", json={"version": ed["version"],
                     "primary_file_id": second["id"]},
                     headers={"origin": "http://testserver", "x-csrf-token": owner["csrf"]})  # fmt: skip
    assert r.status_code == 200, r.text
    assert reindex(settings)["indexed"] == 1
    assert find(client, "ishmael")["results"] == []
    (hit,) = find(client, "queequeg")["results"]
    assert hit["file_id"] == second["id"]


def test_a_pdf_is_found_by_page_once_its_text_is_read(client, owner, settings):
    from lectern.pdftext.store import sweep

    pages = [
        "A map of the delta and its many channels, drawn by hand in the dry season.",
        "The Orinoco floods in May, and the drizzly season lasts until November.",
    ]
    text_pdf(settings.inbox_path / "atlas.pdf", pages)
    drain_inbox(settings)
    (wid,) = ids(client).values()
    # Search reads the PDF's canonical text (lectern.pdftext), never the PDF itself: nothing
    # to index until that text is ready, and nothing noted as an error meanwhile.
    reindex(settings)
    assert find(client, "orinoco floods")["results"] == []
    assert sweep(session_factory(), settings.library_dir) == 1
    assert reindex(settings)["indexed"] == 1
    (hit,) = find(client, "orinoco floods")["results"]
    assert (hit["work_id"], hit["format"], hit["page"]) == (wid, "pdf", 2)
    assert "char" not in hit and hit["match"] == "Orinoco floods"
    file_id = client.get(f"/api/works/{wid}/edit").json()["files"][0]["id"]
    assert hit["file_id"] == file_id
    assert find(client, "delta channels")["results"][0]["page"] == 1
    con = search.connect(settings.data_dir / "search.db")
    try:  # one section, one chunk: the page comes from the chunk's page map
        assert [r[0] for r in con.execute("SELECT pages FROM chunks")] == ["0:1,75:2"]
    finally:
        con.close()


def test_unreadable_and_missing_files(client, owner, settings, moby):
    # A file gone from disk (a folder mid-rename) is tried again later, not marked done.
    with session_factory()() as db:
        want = search.wanted(db)[moby]
    path = settings.library_dir / want.dir_path / want.sources[0].rel_path
    data = path.read_bytes()
    path.unlink()
    con = search.connect(settings.data_dir / "search.db")
    try:
        con.execute("UPDATE indexed SET source = 'stale'")
    finally:
        con.close()
    retry: dict = {}
    done = search.reconcile(session_factory(), settings, budget_s=None, retry=retry)
    assert done == {"removed": 0, "indexed": 0, "pending": 1} and moby in retry
    # backing off: the next pass does not try again yet
    assert search.reconcile(session_factory(), settings, budget_s=None, retry=retry)["pending"] == 0
    # The old chunks stayed searchable meanwhile.
    assert find(client, "ishmael")["results"]
    # Back, but damaged: indexed with nothing found, noted, not retried every pass.
    path.write_bytes(b"not a zip" + zlib.compress(data))
    assert reindex(settings)["indexed"] == 1
    assert find(client, "ishmael")["results"] == []
    con = search.connect(settings.data_dir / "search.db")
    try:
        (error,) = con.execute("SELECT error FROM indexed").fetchone()
    finally:
        con.close()
    assert error == f"{want.sources[0].rel_path}: EpubError"
    assert reindex(settings) == {"removed": 0, "indexed": 0, "pending": 0}


def test_a_long_book_is_capped(client, owner, settings):
    settings.search_max_chars_per_work = 10_000
    sentence = "The quick brown fox jumps over the lazy dog. "
    make_epub(settings.inbox_path / "long.epub", title="Long",
              chapters=[f"<p>{sentence * 300}</p>", "<p>Unreachable epilogue text.</p>"])  # fmt: skip
    drain_inbox(settings)
    reindex(settings)
    con = search.connect(settings.data_dir / "search.db")
    try:
        (chars,) = con.execute("SELECT chars FROM indexed").fetchone()
        sizes = [len(search._inflate(z)) for (z,) in con.execute("SELECT z FROM chunks")]
    finally:
        con.close()
    assert chars == 10_000 and sum(sizes) <= 10_000
    assert max(sizes) <= search.CHUNK_CHARS
    assert find(client, "quick brown fox")["results"]
    assert find(client, "unreachable epilogue")["results"] == []


def test_a_changed_layout_rebuilds_the_file(settings):
    path = search.init(settings)
    con = sqlite3.connect(path)
    con.execute("UPDATE meta SET value = '0' WHERE key = 'schema'")
    con.execute("INSERT INTO chunks (work_id, file_id, kind, loc, char_start, z) "
                "VALUES (1, 1, 0, 0, 0, x'00')")  # fmt: skip
    con.commit()
    con.close()
    search.init(settings)
    con = search.connect(path)
    try:
        assert con.execute("SELECT count(*) FROM chunks").fetchone() == (0,)
        assert con.execute("SELECT value FROM meta").fetchone() == (str(search.SCHEMA_VERSION),)
    finally:
        con.close()
    # A damaged file (a full disk, a bad copy) does not stop the server: it is rebuilt.
    path.write_bytes(b"this is not a database" * 100)
    search.init(settings)
    con = search.connect(path)
    try:
        assert con.execute("SELECT count(*) FROM indexed").fetchone() == (0,)
    finally:
        con.close()


def test_snippet_windows_the_first_match():
    marked = "word " * 40 + "\x02Ishmael\x03 and " + "more " * 60 + "\x02Ishmael\x03 end"
    s = search.snippet(marked)
    plain = marked.replace("\x02", "").replace("\x03", "")
    assert s["start"] == plain.index("Ishmael") and s["length"] == 7
    assert [p for p, m in s["parts"] if m] == ["Ishmael"]  # the second match is past the window
    joined = "".join(p for p, _ in s["parts"])
    assert joined.startswith("word") and joined.endswith("more")
    assert len(joined) <= search.SNIPPET_BEFORE + 7 + search.SNIPPET_AFTER
    assert s["before"] and s["after"]
    assert search.snippet("no markers here") is None


_TAGS = ["p", "div", "h1", "li", "em", "span", "b", "script", "br"]
_WORDS = st.text(alphabet="ab .\n\t\u00a0é", min_size=0, max_size=6)


@st.composite
def _markup(draw, depth=0):
    out = []
    for _ in range(draw(st.integers(0, 3))):
        if depth < 3 and draw(st.booleans()):
            tag = draw(st.sampled_from(_TAGS))
            inner = "" if tag == "br" else draw(_markup(depth + 1))
            out.append(f"<{tag}>{inner}</{tag}>")
        else:
            out.append(draw(_WORDS).replace("&", ""))
        if draw(st.booleans()):
            out.append("<!-- c -->")
    return "".join(out)


@settings_(max_examples=300, deadline=None)
@given(_markup())
def test_index_text_maps_back_to_the_reader_timeline(body):
    xhtml = f"<html xmlns='http://www.w3.org/1999/xhtml'><body>{body}</body></html>".encode()
    text, inserted = search.spine_text(xhtml)
    gone = set(inserted)
    assert all(text[i] == " " for i in inserted) and inserted == sorted(gone)
    assert "".join(c for i, c in enumerate(text) if i not in gone) == normalize_spine_text(xhtml)
    assert text == " ".join(text.split())  # one space between words, like the timeline


def test_paragraphs_without_whitespace_between_them_are_separate_words():
    xhtml = (
        b"<html><body><h1>Loomings</h1><p>Call me <em>Ish</em>mael.</p><p>Next</p></body></html>"
    )
    assert normalize_spine_text(xhtml) == "LoomingsCall me Ishmael.Next"
    assert search.spine_text(xhtml) == ("Loomings Call me Ishmael. Next", [8, 25])
