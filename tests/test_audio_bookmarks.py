"""Audiobook bookmarks and clips with notes (issue #81, lectern.audio_bookmarks): made from the
player, placed on the aligned text next to KOReader highlights, kept as time and chapter
without alignment, private and following work visibility, in the highlights export and search,
and carried along when works merge.

The audio timeline here is laid out by the test itself (sentence ``i`` of the fixture's text is
spoken from ``2*i`` s to ``2*i + 1.8`` s), so every expected position comes from the fixture's
text and that layout, never from the code under test.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.conftest import (
    ORIGIN,
    api_delete,
    api_post,
    drain_inbox,
    login,
    make_epub,
    make_user,
)
from tests.test_annotations import OTHER, PHRASE, STORY, ann, book, put  # noqa: F401 - fixture

from lectern.db import session_factory
from lectern.models import AudioBookmark, AudioTrack, Sentence, Shelf, ShelfGrant, ShelfItem, User
from lectern.models import ShelfPermission as Perm
from lectern.progress import SentenceCache

DURATION = 4000.0


def add_track(work_id: int, rel: str = "audio/01.mp3", duration: float = DURATION) -> None:
    with session_factory()() as db:
        n = db.query(AudioTrack).filter_by(work_id=work_id).count()
        db.add(AudioTrack(work_id=work_id, position=n, rel_path=rel, media_type="audio/mpeg",
                          codec="mp3", duration_s=duration, offset_s=0.0, size=1,
                          sha256=f"{work_id:02d}{n:02d}" + "a" * 60))  # fmt: skip
        db.commit()


def align(work_id: int) -> None:
    with session_factory()() as db:
        for s in db.scalars(select(Sentence).where(Sentence.work_id == work_id)):
            s.audio_start_s, s.audio_end_s, s.matched = 2.0 * s.n, 2.0 * s.n + 1.8, True
        db.commit()
    SentenceCache.invalidate(work_id)  # as storing a real alignment does


def sentence_with(work_id: int, phrase: str) -> Sentence:
    char = STORY.index(phrase)
    with session_factory()() as db:
        s = db.scalar(
            select(Sentence).where(
                Sentence.work_id == work_id,
                Sentence.spine_index == 2,
                Sentence.char_start <= char,
                Sentence.char_end > char,
            )
        )
        db.expunge(s)
        return s


def mark(client, csrf, work_id, **body):
    return api_post(client, f"/api/works/{work_id}/audio-bookmarks", csrf, body)


def marks(client, work_id) -> list[dict]:
    r = client.get(f"/api/works/{work_id}/audio-bookmarks")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    return r.json()["bookmarks"]


def patch(client, csrf, mark_id, body):
    return client.patch(f"/api/audio-bookmarks/{mark_id}", json=body,
                        headers={"origin": ORIGIN, "x-csrf-token": csrf})  # fmt: skip


@pytest.fixture
def audiobook(book):  # noqa: F811 - the highlights' fixture book
    add_track(book["work_id"])
    return book


def test_a_bookmark_lands_on_the_highlights_passage_when_aligned(client, owner, audiobook):
    wid = audiobook["work_id"]
    align(wid)
    put(client, audiobook["kindle"], audiobook["doc"], [ann(PHRASE, "2026-10-01 09:30:00")])
    s = sentence_with(wid, PHRASE)
    r = mark(client, owner["csrf"], wid, start_s=2.0 * s.n + 0.7, chapter="The Gift of the Magi")
    assert r.status_code == 201, r.text
    got = r.json()
    assert got["kind"] == "bookmark" and got["end_s"] is None
    assert got["sentence_n"] == got["sentence_end_n"] == s.n
    assert got["location"] == {"spine_index": 2, "char": s.char_start,
                               "length": s.char_end - s.char_start}  # fmt: skip
    assert PHRASE in got["text"] and got["text"] == STORY[s.char_start : s.char_end]
    # The KOReader highlight of the same words is in the same sentence.
    hl = client.get(f"/api/works/{wid}/annotations").json()["annotations"][0]
    assert hl["sentence_n"] == got["sentence_n"]

    # A clip from that sentence into the one with OTHER covers both, inside one spine item.
    o = sentence_with(wid, OTHER)
    assert o.n > s.n
    clip = mark(client, owner["csrf"], wid, start_s=2.0 * s.n + 0.2, end_s=2.0 * o.n + 1.0,
                note="  read this again  ").json()  # fmt: skip
    assert clip["kind"] == "clip" and clip["note"] == "read this again"
    assert (clip["sentence_n"], clip["sentence_end_n"]) == (s.n, o.n)
    assert clip["location"] == {"spine_index": 2, "char": s.char_start,
                                "length": o.char_end - s.char_start}  # fmt: skip
    # Listed in time order.
    assert [m["id"] for m in marks(client, wid)] == [clip["id"], got["id"]]


def test_without_alignment_a_mark_keeps_time_and_chapter_only(client, owner, audiobook):
    wid = audiobook["work_id"]
    r = mark(client, owner["csrf"], wid, start_s=123.456, chapter="Part 2", device="Phone")
    assert r.status_code == 201, r.text
    (m,) = marks(client, wid)
    assert (m["start_s"], m["chapter"], m["device"]) == (123.46, "Part 2", "Phone")
    assert m["location"] is None and m["sentence_n"] is None and m["text"] is None
    # Aligned later: the same mark now lands on the sentence spoken at 123.46 s (n = 61).
    align(wid)
    (m,) = marks(client, wid)
    assert m["sentence_n"] == 61


def test_clips_are_checked_and_edited(client, owner, audiobook, book):  # noqa: F811
    wid, csrf = audiobook["work_id"], owner["csrf"]
    assert mark(client, csrf, wid, start_s=10, end_s=9).status_code == 422
    assert mark(client, csrf, wid, start_s=10, end_s=10.2).status_code == 422
    assert mark(client, csrf, wid, start_s=0, end_s=31 * 60).status_code == 422
    assert mark(client, csrf, wid, start_s=-1).status_code == 422
    # Past the end: clamped to the audiobook's length.
    assert mark(client, csrf, wid, start_s=DURATION + 50).json()["start_s"] == DURATION
    m = mark(client, csrf, wid, start_s=60, end_s=90).json()
    r = patch(client, csrf, m["id"], {"note": "the good bit", "end_s": 75})
    assert r.status_code == 200, r.text
    assert (r.json()["end_s"], r.json()["note"]) == (75, "the good bit")
    r = patch(client, csrf, m["id"], {"bookmark": True}).json()
    assert r["kind"] == "bookmark" and r["end_s"] is None and r["note"] == "the good bit"
    assert patch(client, csrf, m["id"], {"note": "   "}).json()["note"] is None
    assert api_delete(client, f"/api/audio-bookmarks/{m['id']}", csrf).status_code == 200
    assert len(marks(client, wid)) == 1
    # Writes need the CSRF token; a book without audio has nothing to mark.
    r = client.post(f"/api/works/{wid}/audio-bookmarks", json={"start_s": 1})
    assert r.status_code == 403
    with session_factory()() as db:
        db.query(AudioTrack).filter_by(work_id=wid).delete()
        db.commit()
    assert mark(client, csrf, wid, start_s=1).status_code == 409


def test_marks_are_private_and_follow_work_visibility(client, owner, app, audiobook):
    wid, csrf = audiobook["work_id"], owner["csrf"]
    mine = mark(client, csrf, wid, start_s=5, note="mine").json()
    rita = make_user(client, csrf, "rita", "reader")
    mia = make_user(client, csrf, "mia", "member")
    rita_http, mia_http = TestClient(app), TestClient(app)
    rita_csrf = login(rita_http, "rita", rita["password"])
    mia_csrf = login(mia_http, "mia", mia["password"])

    # A member who sees the whole library never sees, edits, or deletes someone else's marks.
    assert marks(mia_http, wid) == []
    assert patch(mia_http, mia_csrf, mine["id"], {"note": "hers"}).status_code == 404
    assert api_delete(mia_http, f"/api/audio-bookmarks/{mine['id']}", mia_csrf).status_code == 404
    assert "mine" not in mia_http.get("/api/annotations.md").text
    assert mia_http.get("/api/annotations").json()["audio_total"] == 0
    # A reader without the book: 404 to list and to make one.
    assert rita_http.get(f"/api/works/{wid}/audio-bookmarks").status_code == 404
    assert mark(rita_http, rita_csrf, wid, start_s=1).status_code == 404

    # Shared with her: she marks it, sees only hers; unshared, hers leave her view too.
    with session_factory()() as db:
        owner_id = db.scalar(select(User.id).where(User.username == "owner"))
        rita_id = db.scalar(select(User.id).where(User.username == "rita"))
        shelf = Shelf(owner_id=owner_id, name="For Rita")
        db.add(shelf)
        db.flush()
        db.add(ShelfItem(shelf_id=shelf.id, work_id=wid))
        db.add(ShelfGrant(shelf_id=shelf.id, user_id=rita_id, permission=Perm.VIEW))
        db.commit()
    hers = mark(rita_http, rita_csrf, wid, start_s=7, note="rita's")
    assert hers.status_code == 201, hers.text
    assert [m["note"] for m in marks(rita_http, wid)] == ["rita's"]
    assert [m["note"] for m in marks(client, wid)] == ["mine"]
    with session_factory()() as db:
        db.delete(db.scalar(select(ShelfGrant).where(ShelfGrant.user_id == rita_id)))
        db.commit()
    assert rita_http.get(f"/api/works/{wid}/audio-bookmarks").status_code == 404
    r = patch(rita_http, rita_csrf, hers.json()["id"], {"note": "x"})
    assert r.status_code == 404
    assert rita_http.get("/api/annotations").json()["audio_total"] == 0
    assert "rita's" not in rita_http.get("/api/annotations.md").text


def test_marks_are_in_the_highlights_export_summary_and_search(client, owner, audiobook):
    wid, csrf = audiobook["work_id"], owner["csrf"]
    align(wid)
    s = sentence_with(wid, PHRASE)
    mark(client, csrf, wid, start_s=2.0 * s.n + 0.5, end_s=2.0 * s.n + 31,
         chapter="The Gift of the Magi", note="Pérez said this matters")  # fmt: skip
    mark(client, csrf, wid, start_s=3725, note="# not a heading")

    # Only audio marks, no highlights yet: the book still shows on the highlights page.
    summary = client.get("/api/annotations").json()
    assert (summary["total"], summary["audio_total"]) == (0, 2)
    assert summary["works"][0]["audio_count"] == 2 and summary["works"][0]["count"] == 0

    md = client.get(f"/api/works/{wid}/annotations.md").text
    assert "## Audio bookmarks" in md and f"> {STORY[s.char_start : s.char_end]}" in md
    assert "**Note:** Pérez said this matters" in md
    clip_at = 2 * s.n
    span = f"{clip_at // 60}:{clip_at % 60:02d}–{(clip_at + 31) // 60}:{(clip_at + 31) % 60:02d}"
    assert f"*Audio clip {span} · The Gift of the Magi · " in md
    assert "*Audio bookmark 1:02:05 · " in md and "**Note:** \\# not a heading" in md
    # With a highlight, both are under the book, highlights first.
    put(client, audiobook["kindle"], audiobook["doc"], [ann(OTHER, "2026-10-01 09:41:12")])
    md = client.get("/api/annotations.md").text
    assert md.index("## The Gift") < md.index(OTHER) < md.index("### Audio bookmarks")

    def search(q, **kw):
        r = client.get("/api/annotations/search", params={"q": q, **kw})
        assert r.status_code == 200, r.text
        return [h["bookmark"]["note"] for h in r.json()["audio"]]

    assert search("perez") == ["Pérez said this matters"]  # accents and case folded
    assert search("matt") == ["Pérez said this matters"]  # typing: a prefix
    assert search("matt", partial="false") == []
    assert search("note: heading") == ["# not a heading"]
    assert search("highlight: heading") == []  # marks have no highlighted text
    assert len(search("")) == 2
    assert search("", since="2999-01-01") == []
    filtered = client.get("/api/annotations.md", params={"q": "perez"}).text
    assert "Pérez" in filtered and "not a heading" not in filtered and OTHER not in filtered


def test_merging_works_carries_marks_to_where_their_audio_now_plays(client, owner, settings):
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    make_epub(settings.inbox_path / "a.epub", title="Alpha Book", authors=["Ann Writer"])
    make_epub(settings.inbox_path / "b.epub", title="Beta Book", authors=["Bo Writer"])
    drain_inbox(settings)
    ids = {w["title"]: w["id"] for w in client.get("/api/library").json()["works"]}
    target, source = ids["Alpha Book"], ids["Beta Book"]
    add_track(target, "audio/01 a.mp3", 100.0)
    add_track(source, "audio/02 b.mp3", 50.0)
    csrf = owner["csrf"]
    mark(client, csrf, source, start_s=10, note="moment")
    mark(client, csrf, source, start_s=5, end_s=20, note="clip")
    r = api_post(client, f"/api/works/{source}/merge", csrf, {"target_work_id": target})
    assert r.status_code == 200, r.text
    # The source's track now plays after the target's 100 s one.
    got = {m["note"]: (m["start_s"], m["end_s"]) for m in marks(client, target)}
    assert got == {"moment": (110.0, None), "clip": (105.0, 120.0)}
    with session_factory()() as db:
        assert db.query(AudioBookmark).filter_by(work_id=source).count() == 0
