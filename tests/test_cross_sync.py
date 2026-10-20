"""Cross-format sync: KOReader xpointer <-> canonical sentence position <-> audio time."""

from __future__ import annotations

import zipfile

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hsettings
from hypothesis import strategies as st
from sqlalchemy import select
from tests.conftest import ORIGIN, api_post, ko_headers, login, make_sync_key
from tests.test_jobs import EPUB, ingest_fixture, recorded

from lectern.align.worker import LocalWorker
from lectern.db import session_factory
from lectern.ingest.epub import read_spine
from lectern.ingest.hashing import partial_md5
from lectern.models import BookFile, ProgressSource, ReadingProgress, Work, WorkProgress
from lectern.positions import (
    SentencePos,
    TextPos,
    normalize_spine_text,
    text_to_xpointer,
    xpointer_to_text,
)
from lectern.progress import sentence_index, sentence_to_xpointer, xpointer_to_sentence

with zipfile.ZipFile(EPUB) as _zf:
    STORY_XHTML = _zf.read(read_spine(EPUB)[2])
STORY = normalize_spine_text(STORY_XHTML)


@pytest.fixture
def aligned(client, owner, settings, tmp_path) -> dict:
    """The fixture work, aligned with the recorded transcript; returns ids and the KOReader
    document hash."""
    work_id = ingest_fixture(settings, tmp_path)
    api_post(client, f"/api/works/{work_id}/align", owner["csrf"], {})
    worker = LocalWorker(
        session_factory(),
        data_dir=settings.data_dir,
        library=settings.library_dir,
        transcribe=recorded,
    )
    assert worker.run_once()
    with session_factory()() as db:
        book = db.scalar(select(BookFile).where(BookFile.work_id == work_id))
        assert book.partial_md5 == partial_md5(
            settings.library_dir / db.get(Work, work_id).dir_path / book.rel_path
        )
        doc = book.partial_md5
    return {
        "work_id": work_id,
        "doc": doc,
        "ko": ko_headers("owner", make_sync_key(client, owner["csrf"], "kobo")),
    }


def xpointer_at(phrase: str) -> str:
    return text_to_xpointer(TextPos(2, STORY.index(phrase)), STORY_XHTML)


def sentence_of(client, work_id: int, phrase: str) -> dict:
    char = STORY.index(phrase)
    r = client.get(f"/api/works/{work_id}/sentences", params={"spine": 2, "char": char})
    return r.json()["sentences"][0]


def put_web(client, owner, work_id, **body):
    return client.put(
        f"/api/works/{work_id}/progress",
        json=body,
        headers={"origin": ORIGIN, "x-csrf-token": owner["csrf"]},
    )


def test_koreader_position_becomes_the_canonical_position(client, aligned):
    work_id, doc, ko = aligned["work_id"], aligned["doc"], aligned["ko"]
    xp = xpointer_at("Three times Della counted it")
    put = client.put(
        "/syncs/progress",
        json={
            "document": doc,
            "progress": xp,
            "percentage": 0.07,
            "device": "Kobo",
            "device_id": "K1",
        },
        headers=ko,
    )
    assert put.status_code == 200
    target = sentence_of(client, work_id, "Three times Della counted it")
    got = client.get(f"/api/works/{work_id}/progress").json()
    assert got["source"] == "koreader" and got["raw"] == xp and got["device"] == "Kobo"
    assert got["position"] == {"sentence_n": target["n"], "offset": 0}
    # audio resumes where KOReader stopped: the start of that sentence on the timeline
    assert target["matched"]
    assert got["audio_s"] == pytest.approx(target["audio_start_s"], abs=1e-3)
    # KOReader's own record is untouched and still keyed by the document hash
    back = client.get(f"/syncs/progress/{doc}", headers=ko).json()
    assert back["progress"] == xp and back["device_id"] == "K1" and back["percentage"] == 0.07


def test_newer_audio_progress_is_projected_back_to_koreader(client, owner, aligned):
    work_id, doc, ko = aligned["work_id"], aligned["doc"], aligned["ko"]
    client.put(
        "/syncs/progress",
        json={
            "document": doc,
            "progress": xpointer_at("One dollar and eighty-seven"),
            "percentage": 0.05,
        },
        headers=ko,
    )
    later = sentence_of(client, work_id, "There was clearly nothing")
    t = (later["audio_start_s"] + later["audio_end_s"]) / 2
    r = put_web(client, owner, work_id, source="audio", device="Phone", audio_s=t)
    assert r.status_code == 200, r.text
    assert r.json()["position"]["sentence_n"] == later["n"] and r.json()["source"] == "audio"

    got = client.get(f"/syncs/progress/{doc}", headers=ko).json()
    assert got["device"] == "Phone" and got["device_id"] == "lectern-audio"
    # the projected xpointer lands inside the sentence the listener is in
    pos = xpointer_to_text(got["progress"], STORY_XHTML)
    assert later["char_start"] <= pos.char < later["char_end"]
    assert got["percentage"] == pytest.approx(later["n"] / 293, abs=1e-4)

    # KOReader reads on and syncs: its own position is newest again
    xp = xpointer_at("Della finished her cry")
    client.put(
        "/syncs/progress", json={"document": doc, "progress": xp, "percentage": 0.2}, headers=ko
    )
    assert client.get(f"/syncs/progress/{doc}", headers=ko).json()["progress"] == xp
    assert client.get(f"/api/works/{work_id}/progress").json()["source"] == "koreader"


def test_web_text_position_and_sentence_position_writes(client, owner, aligned):
    work_id, doc, ko = aligned["work_id"], aligned["doc"], aligned["ko"]
    s = sentence_of(client, work_id, "Della finished her cry")
    r = put_web(client, owner, work_id, source="web", spine_index=2, char=s["char_start"] + 5)
    assert r.json()["position"] == {"sentence_n": s["n"], "offset": 5}
    assert r.json()["raw"] == f"2/{s['char_start'] + 5}"
    r = put_web(client, owner, work_id, source="web", sentence_n=s["n"], offset=10_000)
    assert r.json()["position"]["offset"] == s["char_end"] - s["char_start"]  # clamped
    xp = client.get(f"/syncs/progress/{doc}", headers=ko).json()["progress"]
    assert xpointer_to_text(xp, STORY_XHTML).char == s["char_end"]
    bad = put_web(client, owner, work_id, source="koreader", sentence_n=1)
    assert bad.status_code == 422  # only the sync projection writes that source
    two = put_web(client, owner, work_id, source="web", sentence_n=1, audio_s=3.0)
    assert two.status_code == 422


def test_unknown_documents_and_unaligned_works_sync_as_before(client, owner, settings, tmp_path):
    ko = ko_headers("owner", make_sync_key(client, owner["csrf"], "kobo"))
    xp = "/body/DocFragment[3]/body/p[1]/text().0"
    client.put(
        "/syncs/progress",
        json={"document": "a" * 32, "progress": xp, "percentage": 0.1},
        headers=ko,
    )
    assert client.get(f"/syncs/progress/{'a' * 32}", headers=ko).json()["progress"] == xp
    work_id = ingest_fixture(settings, tmp_path, audio=False)
    with session_factory()() as db:
        doc = db.scalar(select(BookFile.partial_md5).where(BookFile.work_id == work_id))
    client.put(
        "/syncs/progress", json={"document": doc, "progress": xp, "percentage": 0.1}, headers=ko
    )
    assert client.get(f"/syncs/progress/{doc}", headers=ko).json()["progress"] == xp
    with session_factory()() as db:
        assert db.scalar(select(WorkProgress)) is None
        assert (
            db.scalar(select(ReadingProgress.progress).where(ReadingProgress.document == doc)) == xp
        )
    assert (
        put_web(client, owner, work_id, source="web", sentence_n=0).json()["detail"]
        == "no_sentences"
    )


def test_progress_is_per_user_and_hidden_works_are_404(client, owner, aligned):
    work_id = aligned["work_id"]
    put_web(client, owner, work_id, source="web", sentence_n=20)
    api_post(
        client,
        "/api/admin/users",
        owner["csrf"],
        {"username": "member", "role": "member", "password": "member password 123"},
    )
    api_post(
        client,
        "/api/admin/users",
        owner["csrf"],
        {"username": "reader", "role": "reader", "password": "reader password 123"},
    )
    login(client, "member", "member password 123")
    assert client.get(f"/api/works/{work_id}/progress").json()["position"] is None
    login(client, "reader", "reader password 123")
    assert client.get(f"/api/works/{work_id}/progress").status_code == 404
    assert client.get(f"/api/works/{work_id}/sentences", params={"from": 0}).status_code == 404
    assert client.get(f"/api/works/{work_id}/tracks").status_code == 404


def test_track_streaming_supports_ranges_and_app_passwords(client, owner, aligned):
    import base64

    from tests.conftest import make_app_password

    track = client.get(f"/api/works/{aligned['work_id']}/tracks").json()["tracks"][0]
    full = client.get(track["stream_url"])
    assert full.status_code == 200 and full.headers["accept-ranges"] == "bytes"
    assert full.headers["etag"] == f'"{track["sha256"]}"'
    part = client.get(track["stream_url"], headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == full.content[10:20]
    assert part.headers["content-range"] == f"bytes 10-19/{track['size']}"
    assert (
        client.get(track["stream_url"], headers={"If-None-Match": full.headers["etag"]}).status_code
        == 304
    )
    from fastapi.testclient import TestClient

    anon = TestClient(client.app)
    assert anon.get(track["stream_url"]).status_code == 401
    pw = make_app_password(client, owner["csrf"], "player")
    basic = "Basic " + base64.b64encode(f"owner:{pw}".encode()).decode()
    assert (
        anon.get(
            track["stream_url"], headers={"Authorization": basic, "Range": "bytes=0-0"}
        ).status_code
        == 206
    )


# --- property: the conversions compose ------------------------------------------------------


@pytest.fixture
def index_and_epub(aligned, settings):
    with session_factory()() as db:
        work = db.get(Work, aligned["work_id"])
        idx = sentence_index(db, work.id)
        epub = settings.library_dir / work.dir_path / work.files[0].rel_path
    return idx, epub


def test_property_sentence_xpointer_sentence_round_trip(index_and_epub):
    idx, epub = index_and_epub

    @hsettings(max_examples=200, deadline=None, suppress_health_check=list(HealthCheck))
    @given(st.integers(0, len(idx) - 1), st.integers(0, 400))
    def check(n, offset):
        s = idx.sentence(n)
        pos = SentencePos(n, min(offset, s.char_end - s.char_start - 1))
        xp = sentence_to_xpointer(epub, idx, pos)
        assert xpointer_to_sentence(epub, idx, xp) == pos
        # and through audio for matched sentences: the time maps back into the same sentence
        if s.matched and s.audio_end_s > s.audio_start_s:
            t = idx.to_time(pos)
            assert idx.at_time(t).n == n

    check()


def test_projection_survives_a_damaged_xpointer(client, aligned):
    doc, ko = aligned["doc"], aligned["ko"]
    for junk in ("/body/DocFragment[99]/body/p[1]/text().0", "garbage", "/body/DocFragment[3]/x[["):
        r = client.put(
            "/syncs/progress",
            json={"document": doc, "progress": junk, "percentage": 0.5},
            headers=ko,
        )
        assert r.status_code == 200
        assert client.get(f"/syncs/progress/{doc}", headers=ko).json()["progress"] == junk
    with session_factory()() as db:
        wp = db.scalar(select(WorkProgress))
        # DocFragment[3] with a broken path clamps into the story; the others are not projected
        assert wp is None or wp.source == ProgressSource.KOREADER


# --- review fixes: visibility and edition --------------------------------------------------


def _reader_with_key(client, owner) -> tuple[int, dict]:
    from lectern.models import User

    api_post(
        client,
        "/api/admin/users",
        owner["csrf"],
        {"username": "rita", "role": "reader", "password": "reader password 123"},
    )
    csrf = login(client, "rita", "reader password 123")
    ko = ko_headers("rita", make_sync_key(client, csrf, "kobo"))
    with session_factory()() as db:
        return db.scalar(select(User.id).where(User.username == "rita")), ko


def test_koreader_projection_ignores_works_the_reader_cannot_see(client, owner, aligned):
    from lectern.models import Shelf, ShelfGrant, ShelfItem, ShelfPermission, User

    work_id, doc = aligned["work_id"], aligned["doc"]
    reader_id, ko = _reader_with_key(client, owner)
    xp = xpointer_at("Three times Della counted it")
    r = client.put(
        "/syncs/progress", json={"document": doc, "progress": xp, "percentage": 0.1}, headers=ko
    )
    assert r.status_code == 200  # plain sync still works for the reader's own record
    with session_factory()() as db:
        assert db.scalar(select(WorkProgress).where(WorkProgress.user_id == reader_id)) is None
        # a canonical position that exists anyway (e.g. from before a grant was revoked) and
        # is newer than the device's record must not leak back through GET
        db.add(WorkProgress(user_id=reader_id, work_id=work_id, sentence_n=40, sentence_offset=0,
                            source=ProgressSource.WEB, raw="40:0", device="Web"))  # fmt: skip
        db.commit()
    got = client.get(f"/syncs/progress/{doc}", headers=ko).json()
    assert got["progress"] == xp and not got["device_id"].startswith("lectern")

    # once the work is on a granted shelf, the same reader gets the projection
    with session_factory()() as db:
        owner_id = db.scalar(select(User.id).where(User.username == "owner"))
        shelf = Shelf(owner_id=owner_id, name="Shared")
        db.add(shelf)
        db.flush()
        db.add(ShelfItem(shelf_id=shelf.id, work_id=work_id))
        db.add(ShelfGrant(shelf_id=shelf.id, user_id=reader_id, permission=ShelfPermission.VIEW))
        db.commit()
    client.put(
        "/syncs/progress", json={"document": doc, "progress": xp, "percentage": 0.1}, headers=ko
    )
    with session_factory()() as db:
        wp = db.scalar(select(WorkProgress).where(WorkProgress.user_id == reader_id))
        assert wp.source == ProgressSource.KOREADER and wp.raw == xp


def test_projection_only_for_the_aligned_edition(client, owner, aligned, settings):
    from lectern.ingest.hashing import sha256_file
    from lectern.models import FileKind

    work_id, doc, ko = aligned["work_id"], aligned["doc"], aligned["ko"]
    with session_factory()() as db:
        work = db.get(Work, work_id)
        src = settings.library_dir / work.dir_path / work.files[0].rel_path
        other = src.with_name("book-2.epub")
        other.write_bytes(src.read_bytes())
        with zipfile.ZipFile(other, "a") as zf:
            zf.comment = b"another edition"
        db.add(BookFile(work_id=work_id, kind=FileKind.EBOOK, rel_path="book-2.epub",
                        media_type="application/epub+zip", size=other.stat().st_size,
                        sha256=sha256_file(other), partial_md5="e" * 32))  # fmt: skip
        db.commit()
    doc2 = "e" * 32  # KOReader's hash of the other file (its sampled bytes may coincide)
    xp = xpointer_at("Della finished her cry")
    client.put(
        "/syncs/progress", json={"document": doc2, "progress": xp, "percentage": 0.2}, headers=ko
    )
    with session_factory()() as db:
        assert db.scalar(select(WorkProgress)) is None  # not projected from another edition
    put_web(client, owner, work_id, source="web", sentence_n=30)
    got = client.get(f"/syncs/progress/{doc2}", headers=ko).json()
    assert got["progress"] == xp and not got["device_id"].startswith("lectern")
    # the aligned edition still gets it
    assert client.get(f"/syncs/progress/{doc}", headers=ko).json()["device_id"] == "lectern-web"
