"""Your private vocabulary list (issue #80, lectern.vocabulary): explicit saves, dedupe,
filters, edits, privacy and visibility, exports, and that a lookup alone stores nothing."""

from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from tests.conftest import ORIGIN, api_delete, api_post, login, make_user
from tests.test_annotations import STORY
from tests.test_dictionary import ENGLISH, install, stardict
from tests.test_editing import one_book
from tests.test_jobs import ingest_fixture

from lectern.db import session_factory
from lectern.ingest.sentences import ensure_text_sentences
from lectern.models import (
    AuditLog,
    Sentence,
    Shelf,
    ShelfGrant,
    ShelfItem,
    User,
    VocabularyWord,
    Work,
)
from lectern.models import ShelfPermission as Perm
from lectern.trash import purge_work

PHRASE = "Three times Della counted it"


def save(client, csrf, **kw):
    body = {"word": "Running", "lemma": "run", "lang": "en", "definition": "To move fast.", **kw}
    return api_post(client, "/api/vocabulary", csrf, body)


def words(client, qs="") -> dict:
    r = client.get(f"/api/vocabulary{qs}")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    return r.json()


def rows() -> list[VocabularyWord]:
    with session_factory()() as db:
        return list(db.scalars(select(VocabularyWord).order_by(VocabularyWord.id)))


@pytest.fixture
def book(client, owner, settings, tmp_path) -> int:
    settings.inbox_path.mkdir(parents=True, exist_ok=True)  # for a second book
    wid = ingest_fixture(settings, tmp_path, audio=False)
    with session_factory()() as db:
        assert ensure_text_sentences(db, settings.library_dir, wid)
        db.commit()
    return wid


def share_with(username: str, work_id: int) -> int:
    """Put the work on a new shelf granted to ``username``; returns the grant's shelf id."""
    with session_factory()() as db:
        owner_id = db.scalar(select(User.id).where(User.username == "owner"))
        uid = db.scalar(select(User.id).where(User.username == username))
        shelf = Shelf(owner_id=owner_id, name="Shared")
        db.add(shelf)
        db.flush()
        db.add(ShelfItem(shelf_id=shelf.id, work_id=work_id))
        db.add(ShelfGrant(shelf_id=shelf.id, user_id=uid, permission=Perm.VIEW))
        db.commit()
        return shelf.id


def unshare(shelf_id: int) -> None:
    with session_factory()() as db:
        for g in db.scalars(select(ShelfGrant).where(ShelfGrant.shelf_id == shelf_id)):
            db.delete(g)
        db.commit()


def test_save_stores_word_definition_context_and_location_and_resolves_the_sentence(
    client, owner, book
):
    char = STORY.index(PHRASE)
    r = save(client, owner["csrf"], work_id=book, spine_index=2, char=char, length=5,
             context=PHRASE, note="mine", pos="verb", dictionary="Tiny", to="fr")  # fmt: skip
    assert r.status_code == 201, r.text
    assert r.headers["cache-control"] == "no-store"
    with session_factory()() as db:
        n = db.scalar(
            select(Sentence.n).where(
                Sentence.work_id == book, Sentence.spine_index == 2,
                Sentence.char_start <= char, Sentence.char_end > char,
            )
        )  # fmt: skip
    w = r.json()["word"]
    assert r.json()["created"] is True
    assert (w["word"], w["lemma"], w["lang"], w["to"]) == ("Running", "run", "en", "fr")
    assert (w["definition"], w["note"], w["context"]) == ("To move fast.", "mine", PHRASE)
    assert w["location"] == {"spine_index": 2, "char": char, "length": 5}
    assert w["book"]["id"] == book and w["sentence_n"] == n and n is not None
    [row] = rows()
    assert (row.lemma_key, row.seen_count) == ("run", 1)


def test_saving_again_counts_it_and_keeps_the_first_everything(client, owner, book):
    first = save(client, owner["csrf"], note="n1", context="first sentence", work_id=book).json()
    r = save(client, owner["csrf"], word="running", definition="other", note="n2",
             context="second", work_id=book)  # fmt: skip
    assert r.status_code == 200 and r.json()["created"] is False
    w = r.json()["word"]
    assert w["id"] == first["word"]["id"] and w["seen_count"] == 2
    assert (w["definition"], w["note"], w["context"], w["word"]) == (
        "To move fast.", "n1", "first sentence", "Running")  # fmt: skip
    assert w["last_seen_at"] > first["word"]["last_seen_at"]
    assert w["created_at"] == first["word"]["created_at"]
    save(client, owner["csrf"], lang="fr", definition="courir")
    assert [(r.lang, r.seen_count) for r in rows()] == [("en", 2), ("fr", 1)]


def test_save_on_an_invisible_work_is_404_and_stores_nothing(client, owner, book):
    rita = make_user(client, owner["csrf"], "rita", "reader")
    http = TestClient(client.app)
    csrf = login(http, "rita", rita["password"])
    assert save(http, csrf, work_id=book).status_code == 404
    assert save(client, owner["csrf"], work_id=book + 99).status_code == 404
    assert rows() == []
    assert save(http, csrf).status_code == 201  # no book at all is fine


def test_list_filters_by_work_language_and_date_and_has_facets(
    client, owner, book, settings, tmp_path
):
    other = one_book(settings, title="Other Book", authors=["Zed"])
    save(client, owner["csrf"], word="a", lemma="a", work_id=book)
    save(client, owner["csrf"], word="b", lemma="b", lang="fr", work_id=other)
    save(client, owner["csrf"], word="c", lemma="c", lang="fr")
    with session_factory()() as db:
        for w, day in zip(db.scalars(select(VocabularyWord).order_by(VocabularyWord.id)),
                          (3, 15, 28), strict=True):  # fmt: skip
            w.created_at = datetime(2026, 9, day, 23, 30, tzinfo=UTC)
        db.commit()
    got = words(client)
    assert [w["word"] for w in got["words"]] == ["c", "b", "a"] and got["total"] == 3
    assert got["facets"]["languages"] == [{"lang": "en", "count": 1}, {"lang": "fr", "count": 2}]
    assert {w["id"] for w in got["facets"]["works"]} == {book, other}
    names = lambda qs: [w["word"] for w in words(client, qs)["words"]]  # noqa: E731
    assert names(f"?work={book}") == ["a"]
    assert names("?lang=fr") == ["c", "b"]
    assert names("?since=2026-09-15") == ["c", "b"]
    assert names("?until=2026-09-15") == ["b", "a"]  # the day itself is included
    assert names("?since=2026-09-04&until=2026-09-27") == ["b"]
    page = words(client, "?limit=1&offset=1")
    assert [w["word"] for w in page["words"]] == ["b"] and page["total"] == 3
    assert client.get("/api/vocabulary?since=2026-13-45").status_code == 422


def test_patch_and_delete_and_other_peoples_rows_are_404(client, owner):
    wid = save(client, owner["csrf"]).json()["word"]["id"]
    r = client.patch(f"/api/vocabulary/{wid}", json={"note": "  hi  ", "definition": "Moves."},
                     headers={"origin": ORIGIN, "x-csrf-token": owner["csrf"]})  # fmt: skip
    assert r.status_code == 200, r.text
    assert (r.json()["word"]["note"], r.json()["word"]["definition"]) == ("hi", "Moves.")
    mia = make_user(client, owner["csrf"], "mia", "member")
    http = TestClient(client.app)
    csrf = login(http, "mia", mia["password"])
    h = {"origin": ORIGIN, "x-csrf-token": csrf}
    assert http.patch(f"/api/vocabulary/{wid}", json={"note": "x"}, headers=h).status_code == 404
    assert api_delete(http, f"/api/vocabulary/{wid}", csrf).status_code == 404
    assert [r.note for r in rows()] == ["hi"]
    r = api_delete(client, f"/api/vocabulary/{wid}", owner["csrf"])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert rows() == []


def test_writes_need_a_csrf_token(client, owner):
    assert client.post("/api/vocabulary", json={"word": "x", "definition": "y"}).status_code in (
        401, 403)  # fmt: skip
    wid = save(client, owner["csrf"]).json()["word"]["id"]
    assert client.delete(f"/api/vocabulary/{wid}").status_code in (401, 403)
    assert len(rows()) == 1


def test_an_unshared_book_drops_out_of_the_word_but_the_word_stays(client, owner, book):
    rita = make_user(client, owner["csrf"], "rita", "reader")
    shelf = share_with("rita", book)
    http = TestClient(client.app)
    csrf = login(http, "rita", rita["password"])
    char = STORY.index(PHRASE)
    r = save(http, csrf, work_id=book, spine_index=2, char=char, length=5, context=PHRASE,
             note="ritas")  # fmt: skip
    assert r.status_code == 201
    w = words(http)["words"][0]
    assert w["book"]["title"] and w["context"] == PHRASE and w["location"]
    unshare(shelf)
    got = words(http)
    w = got["words"][0]
    assert (w["word"], w["definition"], w["note"]) == ("Running", "To move fast.", "ritas")
    assert (w["book"], w["location"], w["page"], w["context"]) == (None, None, None, None)
    assert got["facets"]["works"] == []
    assert words(http, f"?work={book}")["words"] == []
    for fmt in ("anki", "csv"):
        text = http.get(f"/api/vocabulary/export?format={fmt}").text
        assert PHRASE not in text and "Gift" not in text and "ritas" in text
    [row] = list(csv.DictReader(io.StringIO(http.get("/api/vocabulary/export?format=csv").text)))
    assert (row["context"], row["book"], row["author"], row["link"]) == ("", "", "", "")


def test_deleting_the_work_keeps_the_word_and_merging_repoints_it(client, owner, book, settings):
    save(client, owner["csrf"], work_id=book, context=PHRASE)
    target = one_book(settings, title="Target Book", authors=["Other Writer"])
    r = api_post(client, f"/api/works/{book}/merge", owner["csrf"], {"target_work_id": target})
    assert r.status_code == 200, r.text
    assert rows()[0].work_id == target
    assert words(client)["words"][0]["book"]["id"] == target
    with session_factory()() as db:
        purge_work(db, settings, db.get(Work, target))
        db.commit()
    [row] = rows()
    assert row.work_id is None
    w = words(client)["words"][0]
    assert w["word"] == "Running" and w["book"] is None and w["context"] is None


def test_anki_export_is_five_columns_with_escaped_fields(client, owner, book):
    save(client, owner["csrf"], definition="a\tb\nc <b>&", note="n<i>", work_id=book,
         context="Said \"x\"\nthen <y>")  # fmt: skip
    save(client, owner["csrf"], word="fly", lemma="fly", lang="", definition="insect")
    r = client.get("/api/vocabulary/export?format=anki")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[:4] == ["#separator:tab", "#html:true",
                         "#columns:Front\tBack\tContext\tBook\tTags", "#tags column:5"]  # fmt: skip
    assert len(lines) == 6  # a newline in a field never starts a new line
    fly, run = (ln.split("\t") for ln in lines[4:])  # newest first
    assert len(fly) == 5 and len(run) == 5
    assert fly[:4] == ["fly", "insect", "", ""] and fly[4] == "lectern"
    assert run[0] == "run"
    assert run[1] == "a b<br>c &lt;b&gt;&amp;<br><br>n&lt;i&gt;"
    assert run[2] == "Said &quot;x&quot;<br>then &lt;y&gt;"
    assert run[3] == "The Gift of the Magi"
    tags = run[4].split(" ")
    assert tags == ["lectern", "lang::en", "book::the-gift-of-the-magi"]


def test_csv_export_defuses_formulas_and_has_the_columns(client, owner, book):
    save(client, owner["csrf"], word="=cmd()", lemma="+sum", definition="@x, \"quoted\"", work_id=book,
         spine_index=2, char=10, length=3, context="-1+1")  # fmt: skip
    r = client.get("/api/vocabulary/export?format=csv")
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["cache-control"] == "no-store"
    [row] = list(csv.DictReader(io.StringIO(r.text)))
    assert list(row) == ["word", "lemma", "language", "translation language", "definition",
                         "note", "context", "book", "author", "saved", "seen count",
                         "link"]  # fmt: skip
    assert row["word"] == "'=cmd()" and row["lemma"] == "'+sum"
    assert row["definition"] == '\'@x, "quoted"' and row["context"] == "'-1+1"
    assert row["book"] == "The Gift of the Magi" and row["seen count"] == "1"
    assert row["link"].endswith(f"/app/reader/{book}?at=2:10&len=3")
    assert row["link"].startswith("http")
    assert client.get("/api/vocabulary/export?format=pdf").status_code == 422


def test_other_members_see_nothing_and_nothing_sensitive_is_logged(client, owner, caplog):
    caplog.set_level(logging.DEBUG)
    save(client, owner["csrf"], word="zyzzyva", lemma="zyzzyva", note="secretnote")
    words(client)
    client.get("/api/vocabulary/export?format=csv")
    mia = make_user(client, owner["csrf"], "mia", "member")
    http = TestClient(client.app)
    login(http, "mia", mia["password"])
    got = words(http)
    assert got["words"] == [] and got["total"] == 0 and got["facets"]["languages"] == []
    for fmt in ("anki", "csv"):
        assert "zyzzyva" not in http.get(f"/api/vocabulary/export?format={fmt}").text
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "zyzzyva" not in logged and "secretnote" not in logged
    with session_factory()() as db:
        audit = "\n".join(f"{a.action} {a.target}" for a in db.scalars(select(AuditLog)))
    assert "zyzzyva" not in audit and "vocabulary" not in audit


def test_a_lookup_alone_saves_nothing_and_audits_nothing(client, owner):
    r = install(client, owner["csrf"], stardict(ENGLISH), name="Tiny English")
    assert r.status_code == 201, r.text
    with session_factory()() as db:
        before = db.scalar(select(func.count()).select_from(AuditLog))
    for _ in range(3):
        r = client.get("/api/dictionary/lookup", params={"q": "fly", "lang": "en"})
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        assert r.json()["entries"]
    assert rows() == []
    with session_factory()() as db:
        assert db.scalar(select(func.count()).select_from(AuditLog)) == before
