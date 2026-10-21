"""The work editor: versioned saves, folder moves that keep hashes and sync, undo, tracks,
formats, covers, shelves, trash, bulk edits, roles, and the filesystem primitives."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import threading
from datetime import timedelta
from io import BytesIO
from pathlib import Path

import pytest
from tests.conftest import (
    ORIGIN,
    api_delete,
    api_post,
    drain_inbox,
    ko_headers,
    login,
    make_app_password,
    make_epub,
    make_mp3,
    make_sync_key,
    make_user,
    needs_ffmpeg,
    put_json,
)

from lectern import fsops
from lectern.db import session_factory
from lectern.ingest.hashing import sha256_file
from lectern.models import (
    Alignment,
    AlignmentStatus,
    AuditLog,
    Job,
    JobStatus,
    TrashedFile,
    Work,
    WorkRevision,
    utcnow,
)
from lectern.sentence_map import audio_fingerprint
from lectern.workmeta import metadata_of


@pytest.fixture(autouse=True)
def _inbox(settings):
    settings.inbox_path.mkdir(parents=True, exist_ok=True)


def patch(client, csrf, wid, body, key=None):
    headers = {"origin": ORIGIN, "x-csrf-token": csrf}
    if key:
        headers["idempotency-key"] = key
    return client.patch(f"/api/works/{wid}", json=body, headers=headers)


def editor(client, wid) -> dict:
    r = client.get(f"/api/works/{wid}/edit")
    assert r.status_code == 200, r.text
    return r.json()


def one_book(settings, **kw) -> int:
    make_epub(settings.inbox_path / "b.epub", **kw)
    drain_inbox(settings)
    with session_factory()() as db:
        return db.query(Work).order_by(Work.id.desc()).first().id


def png(color=(10, 120, 200)) -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (60, 90), color).save(buf, "PNG")
    return buf.getvalue()


def test_metadata_save_moves_folder_and_keeps_hashes_and_sync(client, owner, settings):
    csrf = owner["csrf"]
    wid = one_book(settings, title="The Dispossessed", authors=["Ursula K. Le Guin"], cover=True)
    before = editor(client, wid)
    old_dir = settings.library_dir / before["work"]["dir_path"]
    epub = old_dir / "book.epub"
    digest, pmd5 = sha256_file(epub), before["files"][0]["partial_md5"]
    # KOReader has a position keyed by the document's partial MD5.
    key = make_sync_key(client, csrf, "Kobo")
    h = {**ko_headers("owner", key), "accept": "application/vnd.koreader.v1+json"}
    r = client.put("/syncs/progress", json={"document": pmd5, "progress": "/body/DocFragment[1]",
                   "percentage": 0.4, "device": "Kobo", "device_id": "k1"}, headers=h)  # fmt: skip
    assert r.status_code == 200, r.text

    meta = {
        **before["metadata"],
        "title": "The Dispossessed: An Ambiguous Utopia",
        "authors": [
            {"name": "Ursula K. Le Guin"},
            {"name": "Second Author", "sort": "Author, Second"},
        ],
        "series": "Hainish Cycle",
        "series_index": 5,
        "tags": ["SF", "sf", "Utopia"],
        "narrators": ["Don Leslie"],
        "language": "en-GB",
        "isbn": "0-06-051275-X",
        "published": "1974",
    }
    r = patch(client, csrf, wid, {"version": before["version"], "metadata": meta})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["folder"]["moved"] is True and set(out["changed"]) == {"metadata", "dir_path"}
    ed = out["editor"]
    assert ed["version"] == before["version"] + 1
    m = ed["metadata"]
    assert m["tags"] == ["SF", "Utopia"] and m["isbn"] == "9780060512750"
    # Derived sort keys come back empty (the form shows a placeholder); chosen ones verbatim.
    assert [a["sort"] for a in m["authors"]] == [None, "Author, Second"]
    assert m["sort_title"] is None
    new_dir = settings.library_dir / "Ursula K. Le Guin" / "The Dispossessed An Ambiguous Utopia"
    assert ed["work"]["dir_path"] == "Ursula K. Le Guin/The Dispossessed An Ambiguous Utopia"
    assert not old_dir.exists() and (new_dir / "book.epub").is_file()
    assert sha256_file(new_dir / "book.epub") == digest  # never rewritten
    assert ed["work"]["author"] == "Ursula K. Le Guin & Second Author"
    book = json.loads((new_dir / "book.json").read_text())
    assert book["title"].startswith("The Dispossessed:") and book["files"][0]["sha256"] == digest
    # KOReader still finds the same document and its position; downloads still work.
    got = client.get(f"/syncs/progress/{pmd5}", headers=h).json()
    assert got["percentage"] == 0.4
    assert client.get(f"/library/files/{ed['files'][0]['id']}/download").content == epub_bytes(
        new_dir
    )
    # OPDS shows the new title.
    app_pw = make_app_password(client, csrf, "Reader app")
    import base64

    basic = {"authorization": "Basic " + base64.b64encode(f"owner:{app_pw}".encode()).decode()}
    assert "An Ambiguous Utopia" in client.get("/opds/all", headers=basic).text
    with session_factory()() as db:
        row = db.query(AuditLog).filter_by(action="work.edit").one()
        assert "metadata" in row.detail and "folder" in row.detail


def epub_bytes(d: Path) -> bytes:
    return (d / "book.epub").read_bytes()


def test_stale_version_is_a_conflict_with_the_current_state(client, owner, settings):
    csrf = owner["csrf"]
    wid = one_book(settings, title="Solaris", authors=["Stanislaw Lem"])
    ed = editor(client, wid)
    first = patch(
        client,
        csrf,
        wid,
        {"version": ed["version"], "metadata": {**ed["metadata"], "description": "One"}},
    )
    assert first.status_code == 200
    second = patch(
        client,
        csrf,
        wid,
        {"version": ed["version"], "metadata": {**ed["metadata"], "title": "Overwrite"}},
    )
    assert second.status_code == 409 and second.json()["detail"] == "version_conflict"
    cur = second.json()["current"]
    assert cur["metadata"]["description"] == "One" and cur["metadata"]["title"] == "Solaris"
    assert editor(client, wid)["metadata"]["title"] == "Solaris"
    # A replayed request (same Idempotency-Key) returns the first answer, not a conflict.
    v = editor(client, wid)["version"]
    body = {"version": v, "metadata": {**cur["metadata"], "publisher": "Faber"}}
    a = patch(client, csrf, wid, body, key="save-solaris-01")
    b = patch(client, csrf, wid, body, key="save-solaris-01")
    assert (
        a.status_code == b.status_code == 200 and a.json()["revision_id"] == b.json()["revision_id"]
    )
    # Invalid fields are refused by the schema, with nothing applied.
    bad = patch(
        client,
        csrf,
        wid,
        {"version": v + 1, "metadata": {**cur["metadata"], "isbn": "ISBN-SECRET-123"}},
    )
    assert bad.status_code == 422
    # The form can say which field to fix; the submitted value is never echoed back.
    err = bad.json()
    assert err["detail"] == "Invalid request"
    assert err["errors"][0]["loc"] == ["metadata", "isbn"] and "ISBN" in err["errors"][0]["msg"]
    assert "SECRET" not in bad.text
    assert editor(client, wid)["metadata"]["isbn"] is None


def test_concurrent_saves_one_wins(client, owner, settings):
    """Two editors saving the same version at once: exactly one succeeds."""
    from lectern import editing
    from lectern.models import User

    wid = one_book(settings, title="Roadside Picnic", authors=["Arkady Strugatsky"])
    v = editor(client, wid)["version"]
    results: list[str] = []
    barrier = threading.Barrier(2)

    def run(title: str) -> None:
        with session_factory()() as db:
            user = db.query(User).filter_by(username="owner").one()
            work = db.get(Work, wid)
            meta = {**metadata_of(work), "title": title}
            barrier.wait()
            with fsops.LIBRARY_LOCK:
                db.refresh(work)
                journal = editing.Journal()
                try:
                    editing.save(
                        db,
                        settings,
                        user,
                        work,
                        editing.WorkPatch(version=v, metadata=meta),
                        journal,
                    )
                    db.commit()
                    results.append("ok")
                except editing.EditError as exc:
                    db.rollback()
                    journal.rollback()
                    results.append(exc.code)

    threads = [threading.Thread(target=run, args=(t,)) for t in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["ok", "version_conflict"]
    assert editor(client, wid)["version"] == v + 1


def test_one_save_undo_restores_everything_it_changed(client, owner, settings):
    csrf = owner["csrf"]
    wid = one_book(settings, title="Ficciones", authors=["Jorge Luis Borges"], cover=True)
    shelf = api_post(client, "/api/shelves", csrf, {"name": "Favourites"}).json()["id"]
    ed = editor(client, wid)
    old_cover = (settings.library_dir / ed["work"]["dir_path"] / "cover.jpg").read_bytes()
    r = client.post(f"/api/works/{wid}/cover", content=png(), headers={"origin": ORIGIN, "x-csrf-token": csrf,
                    "content-type": "image/png"})  # fmt: skip
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    assert client.get(r.json()["preview_url"]).headers["content-type"] == "image/jpeg"
    body = {
        "version": ed["version"],
        "metadata": {**ed["metadata"], "title": "Fictions", "tags": ["Stories"]},
        "shelves": [shelf],
        "cover": {"action": "upload", "token": token},
        "playback_default": "read",
        "readalong_enabled": False,
    }
    r = patch(client, csrf, wid, body)
    assert r.status_code == 200, r.text
    after = r.json()["editor"]
    assert after["cover"]["source"] == "upload" and after["shelves"][0]["member"] is True
    assert after["last_revision"]["undoable"] is True
    new_dir = settings.library_dir / after["work"]["dir_path"]
    assert (new_dir / "cover.jpg").read_bytes() != old_cover
    rev = r.json()["revision_id"]
    r = api_post(client, f"/api/works/{wid}/undo", csrf, {"revision_id": rev})
    assert r.status_code == 200, r.text
    back = r.json()
    assert back["metadata"]["title"] == "Ficciones" and back["metadata"]["tags"] == []
    assert back["shelves"][0]["member"] is False
    assert back["playback_default"] is None and back["readalong_enabled"] is True
    assert back["work"]["dir_path"] == ed["work"]["dir_path"]
    assert (settings.library_dir / ed["work"]["dir_path"] / "cover.jpg").read_bytes() == old_cover
    assert back["last_revision"]["undoable"] is False
    again = api_post(client, f"/api/works/{wid}/undo", csrf, {"revision_id": rev})
    assert again.status_code == 409 and again.json()["detail"] == "undo_unavailable"
    # Undo after someone else changed the book is a conflict, not a silent revert.
    r = patch(
        client,
        csrf,
        wid,
        {"version": back["version"], "metadata": {**back["metadata"], "title": "X"}},
    )
    rev2 = r.json()["revision_id"]
    v = r.json()["editor"]["version"]
    patch(
        client,
        csrf,
        wid,
        {"version": v, "metadata": {**r.json()["editor"]["metadata"], "title": "Y"}},
    )
    r = api_post(client, f"/api/works/{wid}/undo", csrf, {"revision_id": rev2})
    assert r.status_code == 409 and r.json()["detail"] == "undo_conflict"


def test_two_epubs_primary_choice_removal_to_trash_and_restore(client, owner, settings):
    csrf = owner["csrf"]
    make_epub(
        settings.inbox_path / "a.epub",
        title="Middlemarch",
        authors=["George Eliot"],
        isbn="9780141439549",
    )
    drain_inbox(settings)
    make_epub(settings.inbox_path / "b.epub", title="Middlemarch", authors=["George Eliot"],
              isbn="9780141439549", padding=4000)  # fmt: skip
    drain_inbox(settings)
    with session_factory()() as db:
        wid = db.query(Work).one().id
    ed = editor(client, wid)
    first, second = ed["files"]
    assert first["primary"] and not second["primary"]
    count = client.get(f"/api/works/{wid}/sentences?from=0").json()["count"]
    r = patch(client, csrf, wid, {"version": ed["version"], "primary_file_id": second["id"]})
    assert r.status_code == 200 and r.json()["changed"] == ["primary_file_id"]
    # The reader opens the chosen edition and sentences come from it now.
    assert (
        client.get(f"/library/works/{wid}/book.epub").content
        == (settings.library_dir / ed["work"]["dir_path"] / second["rel_path"]).read_bytes()
    )
    assert client.get(f"/api/works/{wid}/sentences?from=0").json()["count"] > count
    # Refuse a file of another book.
    v = r.json()["editor"]["version"]
    assert patch(client, csrf, wid, {"version": v, "primary_file_id": 99999}).status_code == 422
    # Remove the primary: it goes to the trash, the other EPUB takes over.
    r = patch(client, csrf, wid, {"version": v, "remove_file_ids": [second["id"]]})
    assert r.status_code == 200, r.text
    ed2 = r.json()["editor"]
    assert [f["id"] for f in ed2["files"]] == [first["id"]] and ed2["files"][0]["primary"]
    trash = client.get("/api/trash").json()
    assert [f["name"] for f in trash["files"]] == [second["rel_path"]]
    assert trash["files"][0]["work"]["id"] == wid
    with session_factory()() as db:
        tf = db.query(TrashedFile).one()
        assert (settings.library_dir / tf.trash_path).is_file()
        assert tf.trash_path.startswith("_trash/files/")
    r = api_post(client, f"/api/trash/files/{trash['files'][0]['id']}/restore", csrf)
    assert r.status_code == 200, r.text
    assert sorted(f["id"] for f in editor(client, wid)["files"]) == sorted(
        [first["id"], second["id"]]
    )
    assert client.get("/api/trash").json()["files"] == []


@needs_ffmpeg
def test_tracks_reorder_rename_chapters_invalidate_alignment_and_survive_rescan(
    client, owner, settings
):
    from lectern.align.tracks import sync_tracks

    csrf = owner["csrf"]
    book = settings.inbox_path / "Book"
    book.mkdir()
    for n in (1, 2, 3):
        make_mp3(book / f"{n:02d}.mp3", 1.0, title=f"Chapter {n}", album="Kim", artist="Rudyard Kipling",
                 track=n, freq=200 + 60 * n)  # fmt: skip
    drain_inbox(settings)
    make_epub(settings.inbox_path / "kim.epub", title="Kim", authors=["Rudyard Kipling"])
    drain_inbox(settings)
    with session_factory()() as db:
        wid = db.query(Work).one().id
    ed = editor(client, wid)
    ids = [t["id"] for t in ed["tracks"]]
    shas = [t["sha256"] for t in ed["tracks"]]
    # Pretend it was aligned with the medium model.
    with session_factory()() as db:
        db.add(Alignment(work_id=wid, model="medium", status=AlignmentStatus.SUCCEEDED,
                         epub_sha256=ed["files"][0]["sha256"],
                         audio_fingerprint=audio_fingerprint(shas)))  # fmt: skip
        db.commit()
    order = [ids[2], ids[0], ids[1]]
    body = {
        "version": ed["version"],
        "tracks": [
            {"id": ids[2], "title": "Prologue / ../../escape", "chapters": ["The Lama"]},
            {"id": ids[0]},
            {"id": ids[1], "title": "Le Grand Trunk Road"},
        ],
    }
    r = patch(client, csrf, wid, body)
    assert r.status_code == 200, r.text
    tracks = r.json()["editor"]["tracks"]
    assert [t["id"] for t in tracks] == order
    assert [t["title"] for t in tracks] == [
        "Prologue / ../../escape",
        "Chapter 1",
        "Le Grand Trunk Road",
    ]
    assert tracks[0]["chapters"][0]["title"] == "The Lama"
    rels = [t["rel_path"] for t in tracks]
    # The title's slashes cannot make a path: the file stays one name inside audio/.
    assert Path(rels[0]).parts == ("audio", Path(rels[0]).name) and rels[0].endswith(" escape.mp3")
    assert all(r.startswith("audio/D01-T00") for r in rels)
    work_dir = settings.library_dir / ed["work"]["dir_path"]
    for t in tracks:
        assert sha256_file(work_dir / t["rel_path"]) == t["sha256"]  # bytes untouched
    t = client.get(f"/api/works/{wid}/tracks").json()
    assert [x["id"] for x in t["tracks"]] == order and t["tracks"][1]["offset_s"] > 0
    with session_factory()() as db:
        assert db.query(Alignment).one().status == AlignmentStatus.STALE
        job = db.query(Job).filter_by(work_id=wid, kind="align", status=JobStatus.QUEUED).one()
        assert job.payload["model"] == "medium"  # re-run with the model it was aligned with
        assert job.payload["audio_fingerprint"] == audio_fingerprint([shas[2], shas[0], shas[1]])
        # The minute-by-minute rescan agrees with the renamed files: nothing re-probed.
        work = db.get(Work, wid)
        assert sync_tracks(db, settings.library_dir, work) is False
        db.commit()
    assert editor(client, wid)["tracks"][0]["chapters"][0]["title"] == "The Lama"
    # Wrong track set or chapter count is refused.
    v = editor(client, wid)["version"]
    assert (
        patch(client, csrf, wid, {"version": v, "tracks": [{"id": ids[0]}]}).json()["detail"]
        == "tracks_mismatch"
    )
    bad = {"version": v, "tracks": [{"id": i, "chapters": ["a", "b"]} for i in order]}
    assert patch(client, csrf, wid, bad).json()["detail"] == "chapters_mismatch"
    # Remove a track, then undo: the file and order come back.
    r = patch(client, csrf, wid, {"version": v, "remove_track_ids": [ids[0]]})
    assert [x["id"] for x in r.json()["editor"]["tracks"]] == [ids[2], ids[1]]
    r = api_post(client, f"/api/works/{wid}/undo", csrf, {"revision_id": r.json()["revision_id"]})
    assert r.status_code == 200, r.text
    assert [x["sha256"] for x in r.json()["tracks"]] == [shas[2], shas[0], shas[1]]


def test_cover_embedded_remove_and_bad_images(client, owner, settings):
    csrf = owner["csrf"]
    wid = one_book(settings, title="Orlando", authors=["Virginia Woolf"], cover=True)
    ed = editor(client, wid)
    assert ed["cover"]["embedded_available"] is True
    r = patch(client, csrf, wid, {"version": ed["version"], "cover": {"action": "remove"}})
    assert r.json()["editor"]["cover"]["has_cover"] is False
    # Without art the same URL serves the generated cover (lectern.covergen).
    work_dir = settings.library_dir / editor(client, wid)["work"]["dir_path"]
    assert not (work_dir / "cover.jpg").exists()
    got = client.get(f"/library/works/{wid}/cover.jpg")
    assert got.status_code == 200 and got.content == (work_dir / "derived/cover.jpg").read_bytes()
    v = r.json()["editor"]["version"]
    r = patch(client, csrf, wid, {"version": v, "cover": {"action": "embedded"}})
    assert r.json()["editor"]["cover"]["source"] == "embedded"
    assert client.get(r.json()["editor"]["cover"]["url"]).status_code == 200
    h = {"origin": ORIGIN, "x-csrf-token": csrf}
    assert (
        client.post(f"/api/works/{wid}/cover", content=b"<svg/>", headers=h).json()["detail"]
        == "not_an_image"
    )
    big = b"\xff" * (10 * 1024 * 1024 + 1)
    assert client.post(f"/api/works/{wid}/cover", content=big, headers=h).status_code == 413
    # A token is bound to its uploader.
    token = client.post(f"/api/works/{wid}/cover", content=png(), headers=h).json()["token"]
    make_user(client, csrf, "mem", "member")
    mcsrf = login(client, "mem", "mem-correct horse")
    v = editor(client, wid)["version"]
    r = patch(client, mcsrf, wid, {"version": v, "cover": {"action": "upload", "token": token}})
    assert r.status_code == 404 and r.json()["detail"] == "cover_not_found"


def test_readers_cannot_edit_and_members_can(client, owner, settings):
    csrf = owner["csrf"]
    wid = one_book(settings, title="Shared Book", authors=["A. Writer"])
    shelf = api_post(client, "/api/shelves", csrf, {"name": "Kids"}).json()["id"]
    api_post(client, f"/api/shelves/{shelf}/items", csrf, {"work_id": wid})
    reader = make_user(client, csrf, "kid", "reader")
    put_json(client, f"/api/shelves/{shelf}/grants/{reader['id']}", csrf, {"permission": "edit"})
    make_user(client, csrf, "mem", "member")
    rcsrf = login(client, "kid", reader["password"])
    assert client.get(f"/api/works/{wid}").status_code == 200  # can see it
    assert client.get(f"/api/works/{wid}/edit").status_code == 403
    assert patch(client, rcsrf, wid, {"version": 1, "readalong_enabled": False}).status_code == 403
    assert api_post(client, f"/api/works/{wid}/trash", rcsrf).status_code == 403
    assert (
        api_post(
            client, "/api/works/bulk", rcsrf, {"work_ids": [wid], "action": "trash"}
        ).status_code
        == 403
    )
    assert client.get("/api/trash").status_code == 403
    assert (
        api_post(client, "/api/metadata/lookup", rcsrf, {"isbn": "9780441478125"}).status_code
        == 403
    )
    mcsrf = login(client, "mem", "mem-correct horse")
    ed = editor(client, wid)
    assert [
        s["name"] for s in ed["shelves"]
    ] == []  # the owner's shelf is not the member's to change
    r = patch(client, mcsrf, wid, {"version": ed["version"], "shelves": [shelf]})
    assert r.status_code == 403 and r.json()["detail"] == "shelf_not_editable"
    r = patch(
        client,
        mcsrf,
        wid,
        {"version": ed["version"], "metadata": {**ed["metadata"], "title": "Renamed"}},
    )
    assert r.status_code == 200
    assert (
        client.patch(
            f"/api/works/{wid}", json={"version": 2}, headers={"origin": ORIGIN}
        ).status_code
        == 403
    )


def test_trash_hides_everywhere_restores_and_purges(client, owner, settings):
    csrf = owner["csrf"]
    wid = one_book(settings, title="Trash Me", authors=["Some One"], cover=True)
    ed = editor(client, wid)
    file_id = ed["files"][0]["id"]
    folder = settings.library_dir / ed["work"]["dir_path"]
    r = api_post(client, f"/api/works/{wid}/trash", csrf, {"version": ed["version"] + 5})
    assert r.status_code == 409
    r = api_post(client, f"/api/works/{wid}/trash", csrf, {"version": ed["version"]})
    assert r.status_code == 200, r.text
    assert not folder.exists()
    assert client.get("/api/library").json()["works"] == []
    for url in (f"/api/works/{wid}", f"/library/files/{file_id}/download", f"/library/works/{wid}/cover.jpg",
                f"/api/works/{wid}/edit"):  # fmt: skip
        assert client.get(url).status_code == 404, url
    t = client.get("/api/trash").json()
    assert t["works"][0]["work"]["id"] == wid and t["works"][0]["days_left"] >= 29
    with session_factory()() as db:
        w = db.get(Work, wid)
        assert (
            w.dir_path.startswith("_trash/works/")
            and (settings.library_dir / w.dir_path / "book.epub").is_file()
        )
    # A member can restore but not purge.
    make_user(client, csrf, "mem", "member")
    mcsrf = login(client, "mem", "mem-correct horse")
    assert api_delete(client, f"/api/trash/works/{wid}", mcsrf).status_code == 403
    r = api_post(client, f"/api/trash/works/{wid}/restore", mcsrf)
    assert r.status_code == 200 and r.json()["dir_path"] == ed["work"]["dir_path"]
    assert (folder / "book.epub").is_file() and client.get(f"/api/works/{wid}").status_code == 200
    # Background cleanup purges after 30 days, not before.
    from lectern import trash

    csrf = login(client, "owner", "correct horse battery staple")
    api_post(client, f"/api/works/{wid}/trash", csrf)
    with session_factory()() as db:
        assert trash.purge_expired(db, settings)["works"] == 0
        w = db.get(Work, wid)
        trashed_dir = settings.library_dir / w.dir_path
        w.trashed_at = utcnow() - timedelta(days=31)
        db.commit()
        assert trash.purge_expired(db, settings)["works"] == 1
        db.commit()
        assert db.get(Work, wid) is None
    assert not trashed_dir.exists()
    with session_factory()() as db:
        assert {a.action for a in db.query(AuditLog)} >= {"work.trash", "work.restore"}


def test_bulk_edits_with_partial_failure_and_undo(client, owner, settings):
    csrf = owner["csrf"]
    for t in ("Alpha", "Beta", "Gamma"):
        make_epub(settings.inbox_path / f"{t}.epub", title=t, authors=["Old Author"])
    drain_inbox(settings)
    ids = sorted(w["id"] for w in client.get("/api/library").json()["works"])
    shelf = api_post(client, "/api/shelves", csrf, {"name": "Series"}).json()["id"]
    r = api_post(client, "/api/works/bulk", csrf, {"work_ids": [*ids[:2], 9999], "action": "set_authors",
                 "authors": [{"name": "New Author"}]})  # fmt: skip
    assert r.status_code == 200, r.text
    out = r.json()
    assert [x["ok"] for x in out["results"]] == [True, True, False]
    assert out["results"][2]["error"] == "not_found"
    lib = {w["id"]: w for w in client.get("/api/library").json()["works"]}
    assert lib[ids[0]]["author"] == "New Author" and lib[ids[2]]["author"] == "Old Author"
    assert lib[ids[0]]["dir_path"].startswith("New Author/")
    api_post(
        client,
        "/api/works/bulk",
        csrf,
        {"work_ids": ids, "action": "set_series", "series": "Greek", "series_index": 1},
    )
    api_post(
        client,
        "/api/works/bulk",
        csrf,
        {"work_ids": ids, "action": "add_tags", "tags": ["letters"]},
    )
    api_post(
        client,
        "/api/works/bulk",
        csrf,
        {"work_ids": ids, "action": "add_to_shelves", "shelf_ids": [shelf]},
    )
    assert client.get(f"/api/shelves/{shelf}").json()["work_count"] == 3
    lib = {w["id"]: w for w in client.get("/api/library").json()["works"]}
    assert all(lib[i]["series"] == "Greek" and lib[i]["tags"] == ["letters"] for i in ids)
    r = api_post(client, f"/api/works/bulk/{out['batch_id']}/undo", csrf)
    assert [x["ok"] for x in r.json()["results"]] == [False, False]  # later bulk edits changed them
    trash = api_post(
        client, "/api/works/bulk", csrf, {"work_ids": ids[:2], "action": "trash"}
    ).json()
    assert len(client.get("/api/library").json()["works"]) == 1
    r = api_post(client, f"/api/works/bulk/{trash['batch_id']}/undo", csrf)
    assert all(x["ok"] for x in r.json()["results"])
    assert len(client.get("/api/library").json()["works"]) == 3


def test_rename_into_an_existing_folder_gets_a_suffix(client, owner, settings):
    csrf = owner["csrf"]
    make_epub(settings.inbox_path / "a.epub", title="Twin", authors=["Same Person"])
    make_epub(settings.inbox_path / "b.epub", title="Other", authors=["Same Person"])
    drain_inbox(settings)
    lib = {w["title"]: w for w in client.get("/api/library").json()["works"]}
    ed = editor(client, lib["Other"]["id"])
    r = patch(
        client,
        csrf,
        lib["Other"]["id"],
        {"version": ed["version"], "metadata": {**ed["metadata"], "title": "Twin"}},
    )
    assert r.json()["folder"]["dir_path"] == "Same Person/Twin (2)"
    assert (settings.library_dir / "Same Person" / "Twin" / "book.epub").is_file()


def test_failed_commit_rolls_the_folder_move_back(client, owner, settings, monkeypatch):
    csrf = owner["csrf"]
    wid = one_book(settings, title="Rollback", authors=["Care Ful"])
    ed = editor(client, wid)
    folder = settings.library_dir / ed["work"]["dir_path"]

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("lectern.idempotency.store", boom)
    with pytest.raises(RuntimeError):
        patch(
            client,
            csrf,
            wid,
            {"version": ed["version"], "metadata": {**ed["metadata"], "title": "Moved"}},
        )
    assert (folder / "book.epub").is_file()
    assert not (settings.library_dir / "Care Ful" / "Moved").exists()
    assert editor(client, wid)["work"]["dir_path"] == ed["work"]["dir_path"]


def test_readalong_toggle_and_regeneration_rules(client, owner, settings):
    csrf = owner["csrf"]
    wid = one_book(settings, title="Toggle", authors=["Some Body"])
    ed = editor(client, wid)
    patch(
        client,
        csrf,
        wid,
        {"version": ed["version"], "readalong_enabled": False, "playback_default": "listen"},
    )
    ra = client.get(f"/api/works/{wid}/read-aloud").json()
    assert ra["readalong_enabled"] is False and ra["readalong"] is False
    assert client.get(f"/api/works/{wid}").json()["playback_default"] == "listen"
    r = api_post(client, f"/api/works/{wid}/read-aloud?force=true", csrf)
    assert r.status_code == 409 and r.json()["detail"] == "no_alignment"
    assert patch(client, csrf, wid, {"version": 99, "playback_default": "sing"}).status_code == 422


# --- filesystem primitives ----------------------------------------------------------------------


def test_checked_refuses_traversal(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    os.symlink(tmp_path / "outside", root / "link")
    for bad in ("../x", "/etc/passwd", "a/../../x", "link/file", ""):
        with pytest.raises(fsops.MoveError):
            fsops.checked(root, bad)
    assert fsops.checked(root, "a/b.mp3") == root / "a" / "b.mp3"


def test_cross_filesystem_moves_copy_verify_and_clean_up(tmp_path, monkeypatch):
    src_dir = tmp_path / "src"
    (src_dir / "audio").mkdir(parents=True)
    (src_dir / "book.epub").write_bytes(b"epub-bytes" * 100)
    (src_dir / "audio" / "t1.mp3").write_bytes(b"mp3" * 1000)
    digests = {
        "book.epub": hashlib.sha256(b"epub-bytes" * 100).hexdigest(),
        "audio/t1.mp3": hashlib.sha256(b"mp3" * 1000).hexdigest(),
    }
    real_rename, real_link = os.rename, os.link

    def exdev_rename(a, b):
        if ".lectern-move-" in str(a):
            return real_rename(a, b)  # the final rename happens on the destination filesystem
        raise OSError(errno.EXDEV, "cross-device")

    def exdev_link(a, b):
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr(os, "rename", exdev_rename)
    monkeypatch.setattr(os, "link", exdev_link)
    dst = tmp_path / "other-fs" / "Author" / "Title"
    fsops.move_dir(src_dir, dst, digests=digests)
    assert not src_dir.exists()
    assert (
        hashlib.sha256((dst / "audio" / "t1.mp3").read_bytes()).hexdigest()
        == digests["audio/t1.mp3"]
    )
    assert not list(dst.parent.glob(".lectern-move-*"))
    # A copy that does not verify leaves the source intact and no partial copy behind.
    src2 = tmp_path / "src2"
    src2.mkdir()
    (src2 / "book.epub").write_bytes(b"real")
    with pytest.raises(fsops.MoveError):
        fsops.move_dir(src2, tmp_path / "other-fs" / "X", digests={"book.epub": "0" * 64})
    assert (src2 / "book.epub").read_bytes() == b"real"
    assert not (tmp_path / "other-fs" / "X").exists() and not list(
        (tmp_path / "other-fs").glob(".lectern-move-*")
    )
    f = tmp_path / "one.mp3"
    f.write_bytes(b"x" * 10)
    fsops.move_file(
        f, tmp_path / "other-fs" / "one.mp3", sha256=hashlib.sha256(b"x" * 10).hexdigest()
    )
    assert not f.exists() and (tmp_path / "other-fs" / "one.mp3").read_bytes() == b"x" * 10
    monkeypatch.setattr(os, "rename", real_rename)
    monkeypatch.setattr(os, "link", real_link)
    g = tmp_path / "g"
    g.write_bytes(b"g")
    with pytest.raises(FileExistsError):
        fsops.move_file(g, tmp_path / "other-fs" / "one.mp3")  # never overwrites
    assert g.exists()


def test_revision_history_is_listed(client, owner, settings):
    csrf = owner["csrf"]
    wid = one_book(settings, title="History", authors=["Writer"])
    ed = editor(client, wid)
    patch(
        client,
        csrf,
        wid,
        {"version": ed["version"], "metadata": {**ed["metadata"], "publisher": "Penguin"}},
    )
    revs = client.get(f"/api/works/{wid}/revisions").json()["revisions"]
    assert revs[0]["changed"] == ["metadata"] and revs[0]["actor"] == "owner"
    assert revs[0]["undoable"] is True and revs[0]["kind"] == "edit"
    with session_factory()() as db:
        assert db.query(WorkRevision).count() == 1
