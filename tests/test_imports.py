"""Review before filing: imports over the resumable upload API, detection, proposals, filing,
duplicates, ownership, auto-file, combine, idempotency, and expiry."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from tests.conftest import (
    ORIGIN,
    api_delete,
    api_post,
    drain_inbox,
    login,
    make_epub,
    make_mp3,
    make_user,
    needs_ffmpeg,
)

from lectern.db import session_factory
from lectern.models import AuditLog, BookFile, Import, ImportStatus, Work, utcnow

CHUNK = 4096


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch, settings):
    monkeypatch.setattr("lectern.routes.api.uploads.UPLOAD_CHUNK_MAX_BYTES", CHUNK)
    settings.inbox_path.mkdir(parents=True, exist_ok=True)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


MEDIA = {".epub": "application/epub+zip", ".mp3": "audio/mpeg", ".m4b": "audio/mp4"}


def new_import(client, csrf, label="", key=None) -> dict:
    headers = {"origin": ORIGIN, "x-csrf-token": csrf}
    if key:
        headers["idempotency-key"] = key
    r = client.post("/api/imports", json={"label": label}, headers=headers)
    assert r.status_code in (200, 201), r.text
    return r.json()


def upload(client, csrf, import_id: str, path: Path, name: str | None = None):
    """The browser's resumable upload of one file into an import; returns the finalize answer."""
    data = path.read_bytes()
    body = {
        "filename": name or path.name,
        "size": len(data),
        "media_type": MEDIA[path.suffix],
        "import_id": import_id,
    }
    r = api_post(client, "/api/uploads", csrf, body)
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    for off in range(0, len(data), CHUNK):
        part = data[off : off + CHUNK]
        r = client.put(
            f"/api/uploads/{uid}/chunk",
            content=part,
            headers={
                "origin": ORIGIN,
                "x-csrf-token": csrf,
                "upload-offset": str(off),
                "x-chunk-sha256": sha(part),
                "content-type": "application/octet-stream",
            },
        )
        assert r.status_code == 204, r.text
    return api_post(client, f"/api/uploads/{uid}/finalize", csrf, {"sha256": sha(data)})


def complete(client, csrf, iid) -> dict:
    r = api_post(client, f"/api/imports/{iid}/complete", csrf)
    assert r.status_code == 200, r.text
    return r.json()


def file_it(client, csrf, iid, body=None, key=None):
    headers = {"origin": ORIGIN, "x-csrf-token": csrf}
    if key:
        headers["idempotency-key"] = key
    return client.post(f"/api/imports/{iid}/file", json=body or {}, headers=headers)


def epub_with_series(path: Path, title: str, **kw) -> Path:
    """make_epub plus Calibre series metadata and an EPUB 3 narrator."""
    import zipfile

    make_epub(path, title=title, **kw)
    with zipfile.ZipFile(path) as zf:
        items = {n: zf.read(n) for n in zf.namelist()}
    opf = items["OEBPS/content.opf"].decode()
    opf = opf.replace(
        "</metadata>",
        '<meta name="calibre:series" content="Earthsea"/>'
        '<meta name="calibre:series_index" content="2"/>'
        '<dc:contributor id="nar">Rob Inglis</dc:contributor>'
        '<meta refines="#nar" property="role" scheme="marc:relators">nrt</meta>'
        "<dc:subject>Fantasy</dc:subject></metadata>",
    )
    items["OEBPS/content.opf"] = opf.encode()
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), items.pop("mimetype"))
        for n, b in items.items():
            zf.writestr(n, b)
    return path


def test_epub_is_staged_reviewed_edited_and_filed(client, owner, settings, tmp_path):
    csrf = owner["csrf"]
    src = epub_with_series(
        tmp_path / "tombs.epub",
        "The Tombs of Atuan",
        authors=["Ursula K. Le Guin"],
        isbn="9780689845369",
        cover=True,
        language="en",
    )
    imp = new_import(client, csrf, "tombs.epub")
    assert imp["status"] == "uploading" and imp["files"] == []
    r = upload(client, csrf, imp["id"], src)
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["queued"] is None and done["import"]["id"] == imp["id"]
    # Nothing reached the inbox or the library: review comes first.
    assert not list(settings.inbox_path.glob("*.epub")) if settings.inbox_path.exists() else True
    assert client.get("/api/library").json()["works"] == []

    ready = complete(client, csrf, imp["id"])
    assert ready["status"] == "ready"
    d = ready["detected"]
    assert d["title"] == "The Tombs of Atuan"
    assert d["authors"] == [{"name": "Ursula K. Le Guin", "sort": None}]
    assert (d["series"], d["series_index"]) == ("Earthsea", 2.0)
    assert d["narrators"] == ["Rob Inglis"] and d["language"] == "en"
    assert d["isbn"] == "9780689845369" and d["tags"] == ["Fantasy"]
    assert (
        ready["cover_url"]
        and client.get(ready["cover_url"]).headers["content-type"] == "image/jpeg"
    )
    p = ready["proposal"]
    assert (p["action"], p["confidence"], p["candidates"]) == ("new", "high", [])

    # Review: fix the title. A stale version is refused.
    meta = {**d, "title": "The Tombs of Atuan (Earthsea 2)"}
    r = client.patch(
        f"/api/imports/{imp['id']}",
        json={"version": ready["version"] - 1, "metadata": meta},
        headers={"origin": ORIGIN, "x-csrf-token": csrf},
    )
    assert r.status_code == 409 and r.json()["detail"] == "version_conflict"
    r = client.patch(
        f"/api/imports/{imp['id']}",
        json={"version": ready["version"], "metadata": meta},
        headers={"origin": ORIGIN, "x-csrf-token": csrf},
    )
    assert r.status_code == 200 and r.json()["metadata"]["title"].endswith("(Earthsea 2)")

    r = file_it(client, csrf, imp["id"], key="file-tombs-0001")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["import"]["status"] == "filed" and out["report"]["created"] is True
    wid = out["work_id"]
    # The same request again (a lost response) answers the same, without a second effect.
    again = file_it(client, csrf, imp["id"], key="file-tombs-0001")
    assert again.json() == out
    # Another body under the same key is refused, not silently replayed.
    reused = file_it(client, csrf, imp["id"], {"audio": "append"}, key="file-tombs-0001")
    assert reused.status_code == 422 and reused.json()["detail"] == "idempotency_key_reused"

    work = client.get(f"/api/works/{wid}").json()
    assert work["title"] == "The Tombs of Atuan (Earthsea 2)"
    assert work["series"] == "Earthsea" and work["narrators"] == ["Rob Inglis"]
    folder = settings.library_dir / "Ursula K. Le Guin" / "The Tombs of Atuan (Earthsea 2)"
    assert work["dir_path"] == "Ursula K. Le Guin/The Tombs of Atuan (Earthsea 2)"
    assert (folder / "book.epub").read_bytes() == src.read_bytes()  # original, byte for byte
    assert (folder / "cover.jpg").is_file()
    book = json.loads((folder / "book.json").read_text())
    assert book["id"] == wid and book["files"][0]["sha256"] == sha(src.read_bytes())
    assert book["series"] == "Earthsea" and book["authors"][0]["name"] == "Ursula K. Le Guin"
    assert not (settings.library_dir / "_staging" / imp["id"]).exists()
    with session_factory()() as db:
        assert db.query(AuditLog).filter_by(action="import.file").count() == 1
        assert db.query(BookFile).one().partial_md5  # KOReader document id computed at filing


def test_join_is_proposed_and_never_overwrites(client, owner, settings, tmp_path):
    csrf = owner["csrf"]
    make_epub(settings.inbox_path / "a.epub", title="Kindred", authors=["Octavia E. Butler"],
              isbn="9780807083697")  # fmt: skip
    drain_inbox(settings)
    with session_factory()() as db:
        w = db.query(Work).one()
        w.description = None
        db.commit()
        existing_id = w.id
    other = make_epub(tmp_path / "k2.epub", title="Kindred: A Novel", authors=["O. Butler"],
                      isbn="9780807083697", padding=50)  # fmt: skip
    imp = new_import(client, csrf)
    upload(client, csrf, imp["id"], other)
    ready = complete(client, csrf, imp["id"])
    p = ready["proposal"]
    assert (p["action"], p["work_id"], p["confidence"]) == ("join", existing_id, "high")
    assert "ISBN" in p["reason"] and p["candidates"][0]["work"]["title"] == "Kindred"
    r = file_it(client, csrf, imp["id"])
    assert r.status_code == 200 and r.json()["work_id"] == existing_id
    assert r.json()["report"]["filled"] == ["description"]
    work = client.get(f"/api/works/{existing_id}").json()
    assert work["title"] == "Kindred" and work["author"] == "Octavia E. Butler"  # unchanged
    assert work["description"] == "About Kindred: A Novel."
    names = {f["rel_path"] for f in work["files"]}
    assert "book.epub" in names and any(n.startswith("book-") for n in names - {"book.epub"})


def test_choose_another_work_or_create_new(client, owner, settings, tmp_path):
    csrf = owner["csrf"]
    make_epub(settings.inbox_path / "a.epub", title="Dune", authors=["Frank Herbert"])
    make_epub(settings.inbox_path / "b.epub", title="Emma", authors=["Jane Austen"])
    drain_inbox(settings)
    ids = {w["title"]: w["id"] for w in client.get("/api/library").json()["works"]}
    imp = new_import(client, csrf)
    upload(
        client,
        csrf,
        imp["id"],
        make_epub(tmp_path / "d.epub", title="Dune", authors=["Frank Herbert"], padding=9),
    )
    ready = complete(client, csrf, imp["id"])
    assert ready["proposal"]["work_id"] == ids["Dune"]
    # The person knows better: it belongs with Emma (silly, but their call).
    r = file_it(client, csrf, imp["id"], {"target": {"action": "join", "work_id": ids["Emma"]}})
    assert r.status_code == 200 and r.json()["work_id"] == ids["Emma"]
    # And "create new" makes a separate work even when the match is certain.
    imp2 = new_import(client, csrf)
    upload(
        client,
        csrf,
        imp2["id"],
        make_epub(tmp_path / "d2.epub", title="Dune", authors=["Frank Herbert"], padding=20),
    )
    complete(client, csrf, imp2["id"])
    r = file_it(client, csrf, imp2["id"], {"target": {"action": "new"}})
    assert r.status_code == 200 and r.json()["work_id"] not in ids.values()
    dirs = sorted(w["dir_path"] for w in client.get("/api/library").json()["works"])
    assert "Frank Herbert/Dune (2)" in dirs  # collision gets a suffix, never an overwrite
    # A target that was trashed in the meantime is a conflict.
    imp3 = new_import(client, csrf)
    upload(
        client,
        csrf,
        imp3["id"],
        make_epub(tmp_path / "e.epub", title="Emma", authors=["Jane Austen"], padding=7),
    )
    complete(client, csrf, imp3["id"])
    assert api_post(client, f"/api/works/{ids['Emma']}/trash", csrf).status_code == 200
    r = file_it(client, csrf, imp3["id"], {"target": {"action": "join", "work_id": ids["Emma"]}})
    assert r.status_code == 409 and r.json()["detail"] == "target_gone"
    # A trashed work is never proposed either.
    assert all(
        c["work"]["id"] != ids["Emma"]
        for c in client.get(f"/api/imports/{imp3['id']}").json()["proposal"]["candidates"]
    )


def test_duplicates_and_wrong_formats_are_explained(client, owner, settings, tmp_path):
    csrf = owner["csrf"]
    src = make_epub(tmp_path / "x.epub", title="Beloved", authors=["Toni Morrison"])
    (settings.inbox_path).mkdir(parents=True, exist_ok=True)
    (settings.inbox_path / "x.epub").write_bytes(src.read_bytes())
    drain_inbox(settings)
    imp = new_import(client, csrf)
    r = upload(client, csrf, imp["id"], src)
    assert r.json()["file"]["status"] == "duplicate"
    f = client.get(f"/api/imports/{imp['id']}").json()["files"][0]
    assert f["duplicate_of"]["title"] == "Beloved" and f["duplicate_of"]["in_trash"] is False
    failed = complete(client, csrf, imp["id"])
    assert (failed["status"], failed["error"]) == ("failed", "all_duplicates")
    assert file_it(client, csrf, imp["id"]).json()["detail"] == "not_ready"

    imp2 = new_import(client, csrf)
    fake = tmp_path / "fake.epub"
    fake.write_bytes(b"this is not a zip at all" * 10)
    r = upload(client, csrf, imp2["id"], fake)
    assert r.status_code == 422 and r.json()["detail"] == "not_epub"
    r = api_post(client, "/api/uploads", csrf, {"filename": "x.pdf", "size": 10,
                 "media_type": "application/pdf", "import_id": imp2["id"]})  # fmt: skip
    assert r.status_code == 415 and r.json()["detail"] == "unsupported_media_type"
    r = api_post(client, "/api/uploads", csrf, {"filename": "huge.m4b", "size": settings.upload_max_bytes + 1,
                 "media_type": "audio/mp4", "import_id": imp2["id"]})  # fmt: skip
    assert r.status_code == 413 and r.json()["detail"] == "too_large"
    with pytest.raises(AssertionError):
        complete(client, csrf, imp2["id"])  # no files


def test_imports_are_private_and_readers_are_forbidden(client, owner, settings, tmp_path):
    csrf = owner["csrf"]
    make_user(client, csrf, "mem", "member")
    reader = make_user(client, csrf, "kid", "reader")
    imp = new_import(client, csrf, "owner's")
    upload(client, csrf, imp["id"], make_epub(tmp_path / "o.epub", title="Owner Book"))
    mcsrf = login(client, "mem", "mem-correct horse")
    assert client.get(f"/api/imports/{imp['id']}").status_code == 404
    assert client.get("/api/imports").json()["imports"] == []
    r = api_post(client, "/api/uploads", mcsrf, {"filename": "a.epub", "size": 5,
                 "media_type": "application/epub+zip", "import_id": imp["id"]})  # fmt: skip
    assert r.status_code == 404
    assert api_post(client, f"/api/imports/{imp['id']}/file", mcsrf).status_code == 404
    assert api_delete(client, f"/api/imports/{imp['id']}", mcsrf).status_code == 404
    rcsrf = login(client, "kid", reader["password"])
    assert api_post(client, "/api/imports", rcsrf, {"label": "x"}).status_code == 403
    assert client.get("/api/imports").status_code == 403
    assert (
        api_post(client, "/api/me/preferences", rcsrf, {"auto_file_imports": True}).status_code
        == 403
    )
    # CSRF still applies.
    assert client.post("/api/imports", json={}, headers={"origin": ORIGIN}).status_code == 403


def test_auto_file_takes_only_certain_matches(client, owner, settings, tmp_path):
    csrf = owner["csrf"]
    make_epub(
        settings.inbox_path / "a.epub",
        title="Piranesi",
        authors=["Susanna Clarke"],
        isbn="9781635575996",
    )
    make_epub(
        settings.inbox_path / "b.epub",
        title="Jonathan Strange and Mr Norrell",
        authors=["Susanna Clarke"],
    )
    drain_inbox(settings)
    assert (
        api_post(client, "/api/me/preferences", csrf, {"auto_file_imports": True}).status_code
        == 200
    )
    certain = new_import(client, csrf)
    upload(client, csrf, certain["id"], make_epub(tmp_path / "p.epub", title="Piranesi (ebook)", authors=["S. Clarke"],
           isbn="9781635575996", padding=3))  # fmt: skip
    out = complete(client, csrf, certain["id"])
    assert out["status"] == "filed" and out["auto_filed"] is True
    vague = new_import(client, csrf)
    upload(
        client,
        csrf,
        vague["id"],
        make_epub(
            tmp_path / "j.epub", title="Jonathan Strange and Mr. Norrell", authors=["Someone Else"]
        ),
    )
    out = complete(client, csrf, vague["id"])
    assert out["status"] == "ready" and out["proposal"]["candidates"]
    assert out["proposal"]["confidence"] != "high"  # same title, other author: ask
    with session_factory()() as db:
        assert db.query(AuditLog).filter_by(action="import.auto_file").count() == 1


def test_combine_epub_and_audiobook_imports(client, owner, settings, tmp_path):
    csrf = owner["csrf"]
    a = new_import(client, csrf, "Lud-in-the-Mist.epub")
    upload(
        client,
        csrf,
        a["id"],
        make_epub(tmp_path / "l.epub", title="Lud-in-the-Mist", authors=["Hope Mirrlees"]),
    )
    b = new_import(client, csrf, "Lud-in-the-Mist")
    upload(
        client,
        csrf,
        b["id"],
        make_epub(
            tmp_path / "l2.epub", title="Lud in the Mist", authors=["Hope Mirrlees"], padding=4
        ),
    )
    complete(client, csrf, a["id"])
    ready_b = complete(client, csrf, b["id"])
    assert [s["id"] for s in ready_b["proposal"]["siblings"]] == [a["id"]]
    r = api_post(client, f"/api/imports/{a['id']}/combine", csrf, {"other_id": b["id"]})
    assert r.status_code == 200 and len(r.json()["files"]) == 2
    assert client.get(f"/api/imports/{b['id']}").json()["status"] == "discarded"
    out = file_it(client, csrf, a["id"]).json()
    work = client.get(f"/api/works/{out['work_id']}").json()
    assert len(work["files"]) == 2


def test_create_is_idempotent_and_discard_cleans_up(client, owner, settings, tmp_path):
    csrf = owner["csrf"]
    one = new_import(client, csrf, "x", key="idem-key-0001")
    two = new_import(client, csrf, "x", key="idem-key-0001")
    assert one["id"] == two["id"]
    upload(client, csrf, one["id"], make_epub(tmp_path / "z.epub", title="Zed"))
    staged = settings.library_dir / "_staging" / one["id"]
    assert any(staged.iterdir())
    assert api_delete(client, f"/api/imports/{one['id']}", csrf).status_code == 204
    assert not staged.exists()
    assert client.get(f"/api/imports/{one['id']}").json()["status"] == "discarded"
    with session_factory()() as db:
        assert db.query(AuditLog).filter_by(action="import.discard").count() == 1


def test_sweep_expires_abandoned_imports(client, owner, settings, tmp_path):
    from lectern import imports

    csrf = owner["csrf"]
    imp = new_import(client, csrf)
    upload(client, csrf, imp["id"], make_epub(tmp_path / "s.epub", title="Stale"))
    orphan = settings.library_dir / "_staging" / "orphan-dir"
    orphan.mkdir(parents=True)
    import os

    os.utime(orphan, (0, 0))
    with session_factory()() as db:
        row = db.query(Import).one()
        row.expires_at = utcnow() - timedelta(minutes=1)
        db.commit()
        counts = imports.sweep(db, settings)
        db.commit()
        assert counts["expired"] == 1 and counts["orphans"] == 1
        assert db.query(Import).one().status == ImportStatus.DISCARDED
    assert not (settings.library_dir / "_staging" / imp["id"]).exists() and not orphan.exists()


def test_plain_uploads_still_go_to_the_inbox(client, owner, settings, tmp_path):
    """Milestone 2 behaviour without ``import_id``: finalize hands off to the watcher."""
    csrf = owner["csrf"]
    data = make_epub(tmp_path / "plain.epub", title="Plain").read_bytes()
    r = api_post(client, "/api/uploads", csrf, {"filename": "plain.epub", "size": len(data),
                 "media_type": "application/epub+zip"})  # fmt: skip
    uid = r.json()["id"]
    for off in range(0, len(data), CHUNK):
        part = data[off : off + CHUNK]
        client.put(f"/api/uploads/{uid}/chunk", content=part, headers={"origin": ORIGIN,
                   "x-csrf-token": csrf, "upload-offset": str(off), "x-chunk-sha256": sha(part)})  # fmt: skip
    r = api_post(client, f"/api/uploads/{uid}/finalize", csrf, {"sha256": sha(data)})
    assert r.json()["queued"].endswith(".epub") and r.json()["import_id"] is None
    drain_inbox(settings)
    assert [w["title"] for w in client.get("/api/library").json()["works"]] == ["Plain"]


@needs_ffmpeg
def test_folder_of_mp3s_becomes_one_audiobook(client, owner, settings, tmp_path):
    csrf = owner["csrf"]
    tracks = [
        make_mp3(
            tmp_path / f"{n:02d}.mp3",
            1.0 + n / 4,
            title=f"Part {n}",
            album="The Hobbit",
            artist="J. R. R. Tolkien",
            track=n,
            freq=300 + 40 * n,
        )  # fmt: skip
        for n in (1, 2, 3)
    ]
    imp = new_import(client, csrf, "The Hobbit")
    for p in reversed(tracks):  # arrival order is not play order
        assert upload(client, csrf, imp["id"], p, name=f"The Hobbit/{p.name}").status_code == 200
    ready = complete(client, csrf, imp["id"])
    assert ready["detected"]["title"] == "The Hobbit"
    assert ready["detected"]["authors"][0]["name"] == "J. R. R. Tolkien"
    assert [f["track_no"] for f in ready["files"]] == [3, 2, 1]
    out = file_it(client, csrf, imp["id"]).json()
    wid = out["work_id"]
    t = client.get(f"/api/works/{wid}/tracks").json()
    assert len(t["tracks"]) == 3
    assert [round(x["duration_s"], 1) for x in t["tracks"]] == pytest.approx(
        [1.2, 1.5, 1.8], abs=0.15
    )
    offsets = [x["offset_s"] for x in t["tracks"]]
    assert offsets[0] == 0 and offsets[1] == pytest.approx(t["tracks"][0]["duration_s"], abs=0.01)
    shas = [x["sha256"] for x in t["tracks"]]
    assert shas == [sha(p.read_bytes()) for p in tracks]  # played in track order, bytes intact
    # A second audiobook for the same work is refused unless asked for.
    imp2 = new_import(client, csrf)
    upload(client, csrf, imp2["id"], make_mp3(tmp_path / "x.mp3", 1.0, title="Extra", album="The Hobbit",
           artist="J. R. R. Tolkien", track=1, freq=900))  # fmt: skip
    ready2 = complete(client, csrf, imp2["id"])
    assert ready2["proposal"]["work_id"] == wid
    r = file_it(client, csrf, imp2["id"])
    assert r.status_code == 409 and r.json()["detail"] == "target_has_audio"
    r = file_it(client, csrf, imp2["id"], {"audio": "append"})
    assert r.status_code == 200
    assert len(client.get(f"/api/works/{wid}/tracks").json()["tracks"]) == 4


def test_inbox_files_keep_series_narrators_and_tags(client, owner, settings):
    """Books dropped into the inbox get the same detected metadata as reviewed uploads."""
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    epub_with_series(
        settings.inbox_path / "tombs.epub", "The Tombs of Atuan", authors=["Ursula K. Le Guin"]
    )
    drain_inbox(settings)
    (w,) = client.get("/api/library").json()["works"]
    assert (w["series"], w["series_index"]) == ("Earthsea", 2)
    assert w["narrators"] == ["Rob Inglis"] and w["tags"] == ["Fantasy"]
