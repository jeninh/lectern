"""Regressions from the review of "Add & edit books": undo conflicts and per-item bulk
results, XML-illegal control characters, upload extensions, trash purge path resolution,
cover decoding limits, merge cleanup, journalled trash/restore, and opaque revisions."""

from __future__ import annotations

import base64
import errno
import os
import struct
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree

import pytest
from tests.conftest import (
    ORIGIN,
    api_post,
    drain_inbox,
    make_app_password,
    make_epub,
)
from tests.test_editing import editor, one_book, patch, png
from tests.test_imports import CHUNK, complete, new_import, sha, upload

from lectern import fsops, trash
from lectern.db import session_factory
from lectern.ingest.hashing import sha256_file
from lectern.models import (
    AuditLog,
    BookFile,
    FileKind,
    Import,
    TrashedFile,
    UploadSession,
    UploadStatus,
    Work,
    WorkRevision,
    utcnow,
)
from lectern.uploads import part_path


@pytest.fixture(autouse=True)
def _dirs(monkeypatch, settings):
    monkeypatch.setattr("lectern.routes.api.uploads.UPLOAD_CHUNK_MAX_BYTES", CHUNK)
    settings.inbox_path.mkdir(parents=True, exist_ok=True)


def two_epubs(client, settings) -> tuple[int, dict, dict]:
    """One work with two different EPUB files of the same book."""
    make_epub(settings.inbox_path / "a.epub", title="Middlemarch", authors=["George Eliot"], cover=True,
              isbn="9780141439549")  # fmt: skip
    drain_inbox(settings)
    make_epub(settings.inbox_path / "b.epub", title="Middlemarch", authors=["George Eliot"],
              isbn="9780141439549", padding=4000)  # fmt: skip
    drain_inbox(settings)
    with session_factory()() as db:
        wid = db.query(Work).one().id
    first, second = editor(client, wid)["files"]
    return wid, first, second


def remove_second(client, csrf, settings, wid, second) -> tuple[int, bytes]:
    ed = editor(client, wid)
    data = (settings.library_dir / ed["work"]["dir_path"] / second["rel_path"]).read_bytes()
    r = patch(client, csrf, wid, {"version": ed["version"], "remove_file_ids": [second["id"]]})
    assert r.status_code == 200, r.text
    return r.json()["revision_id"], data


def audits(action: str) -> list[AuditLog]:
    with session_factory()() as db:
        return db.query(AuditLog).filter_by(action=action).order_by(AuditLog.id).all()


# --- 1. undo conflicts ----------------------------------------------------------------------------


def test_reuploading_a_removed_format_is_a_duplicate_so_undo_still_works(
    client, owner, settings, tmp_path
):
    csrf = owner["csrf"]
    wid, first, second = two_epubs(client, settings)
    rev, data = remove_second(client, csrf, settings, wid, second)
    again = tmp_path / "again.epub"
    again.write_bytes(data)
    imp = new_import(client, csrf)
    r = upload(client, csrf, imp["id"], again)
    # The trash counts: the file is reported as this work's, and nothing is filed anew.
    assert r.json()["file"] == {**r.json()["file"], "status": "duplicate", "duplicate_work_id": wid}
    assert complete(client, csrf, imp["id"])["error"] == "all_duplicates"
    r = api_post(client, f"/api/works/{wid}/undo", csrf, {"revision_id": rev})
    assert r.status_code == 200, r.text
    assert sorted(f["id"] for f in r.json()["files"]) == sorted([first["id"], second["id"]])
    restored = settings.library_dir / r.json()["work"]["dir_path"] / second["rel_path"]
    assert sha256_file(restored) == second["sha256"]


def test_undo_of_a_removal_whose_file_is_back_elsewhere_is_a_clean_409(client, owner, settings):
    csrf = owner["csrf"]
    wid, first, second = two_epubs(client, settings)
    rev, data = remove_second(client, csrf, settings, wid, second)
    # The same bytes were filed as a new work in the meantime (an inbox drop, an old client).
    other = one_book(settings, title="Elsewhere", authors=["Some Body"])
    with session_factory()() as db:
        w = db.get(Work, other)
        (settings.library_dir / w.dir_path / "copy.epub").write_bytes(data)
        db.add(BookFile(work_id=other, kind=FileKind.EBOOK,
                        rel_path="copy.epub", media_type="application/epub+zip", size=len(data),
                        sha256=second["sha256"], partial_md5="0" * 32))  # fmt: skip
        db.commit()
        tf = db.query(TrashedFile).one()
        trash_path = settings.library_dir / tf.trash_path
    before = editor(client, wid)
    r = api_post(client, f"/api/works/{wid}/undo", csrf, {"revision_id": rev})
    assert r.status_code == 409 and r.json()["detail"] == "undo_conflict"
    # Nothing moved and nothing was half-restored.
    assert trash_path.is_file()
    after = editor(client, wid)
    assert after["version"] == before["version"] and after["files"] == before["files"]
    assert after["last_revision"]["undoable"] is True
    work_dir = settings.library_dir / after["work"]["dir_path"]
    assert not [p for p in work_dir.rglob("*") if ".lectern" in p.name]
    with session_factory()() as db:
        assert db.query(TrashedFile).one().restored_at is None


def test_undo_maps_a_vanished_trash_entry_to_undo_conflict(client, owner, settings, monkeypatch):
    csrf = owner["csrf"]
    wid, _first, second = two_epubs(client, settings)
    rev, _ = remove_second(client, csrf, settings, wid, second)

    def gone(*a, **k):
        raise LookupError("not restorable")

    monkeypatch.setattr("lectern.editing.restore_file", gone)
    r = api_post(client, f"/api/works/{wid}/undo", csrf, {"revision_id": rev})
    assert r.status_code == 409 and r.json()["detail"] == "undo_conflict"


def test_bulk_undo_reports_each_work_and_keeps_going(client, owner, settings, monkeypatch):
    csrf = owner["csrf"]
    for t in ("Alpha", "Beta", "Gamma"):
        make_epub(settings.inbox_path / f"{t}.epub", title=t, authors=["Old Author"])
    drain_inbox(settings)
    lib = {w["title"]: w["id"] for w in client.get("/api/library").json()["works"]}
    ids = [lib["Alpha"], lib["Beta"], lib["Gamma"]]
    out = api_post(client, "/api/works/bulk", csrf, {"work_ids": ids, "action": "set_authors",
                   "authors": [{"name": "New Author"}]}).json()  # fmt: skip
    assert all(x["ok"] for x in out["results"])
    # Beta changed since (undo_conflict); Gamma's folder cannot move back (an I/O error).
    ed = editor(client, lib["Beta"])
    patch(client, csrf, lib["Beta"], {"version": ed["version"],
          "metadata": {**ed["metadata"], "publisher": "Later"}})  # fmt: skip
    from lectern import editing

    real = editing.move_dir

    def flaky(src, dst, **kw):
        if "Gamma" in str(src):
            raise OSError(errno.EIO, "I/O error")
        return real(src, dst, **kw)

    monkeypatch.setattr("lectern.editing.move_dir", flaky)
    r = api_post(client, f"/api/works/bulk/{out['batch_id']}/undo", csrf)
    assert r.status_code == 200, r.text
    got = {x["work_id"]: x for x in r.json()["results"]}
    assert got[lib["Alpha"]] == {"work_id": lib["Alpha"], "ok": True}
    assert got[lib["Beta"]]["error"] == "undo_conflict"
    assert got[lib["Gamma"]]["error"] == "io_error"
    after = {w["id"]: w for w in client.get("/api/library").json()["works"]}
    assert after[lib["Alpha"]]["author"] == "Old Author"
    # Gamma rolled back whole: still the bulk edit's author and folder, undo still offered.
    assert after[lib["Gamma"]]["author"] == "New Author"
    gamma_dir = settings.library_dir / after[lib["Gamma"]]["dir_path"]
    assert after[lib["Gamma"]]["dir_path"].startswith("New Author/")
    assert (gamma_dir / "book.epub").is_file()
    with session_factory()() as db:
        undone = db.query(WorkRevision).filter(WorkRevision.batch_id == out["batch_id"]).all()
        assert {r.work_id for r in undone if r.undone_at is not None} == {lib["Alpha"]}
    assert [a.detail.split()[0] for a in audits("work.undo")] == [f"work:{lib['Alpha']}"]
    summary = audits("work.bulk_undo")[-1].detail
    assert summary.startswith("batch:" + out["batch_id"] + " 1/3 works; failed ")
    assert f"{lib['Gamma']}:io_error" in summary and f"{lib['Beta']}:undo_conflict" in summary


# --- 5. bulk edit partial failure ------------------------------------------------------------------


def test_bulk_edit_io_failure_is_per_item_and_replays_coherently(
    client, owner, settings, monkeypatch
):
    csrf = owner["csrf"]
    for t in ("Alpha", "Beta", "Gamma"):
        make_epub(settings.inbox_path / f"{t}.epub", title=t, authors=["Old Author"])
    drain_inbox(settings)
    lib = {w["title"]: w for w in client.get("/api/library").json()["works"]}
    ids = [lib[t]["id"] for t in ("Alpha", "Beta", "Gamma")]
    from lectern import editing

    real = editing.move_dir

    def flaky(src, dst, **kw):
        if "Beta" in str(src):
            raise fsops.MoveError("copy verification failed")
        return real(src, dst, **kw)

    monkeypatch.setattr("lectern.editing.move_dir", flaky)
    body = {"work_ids": ids, "action": "set_authors", "authors": [{"name": "New Author"}]}
    h = {"origin": ORIGIN, "x-csrf-token": csrf, "idempotency-key": "bulk-io-1"}
    r = client.post("/api/works/bulk", json=body, headers=h)
    assert r.status_code == 200, r.text
    res = {x["work_id"]: x for x in r.json()["results"]}
    assert res[lib["Beta"]["id"]] == {"work_id": lib["Beta"]["id"], "ok": False,
                                      "error": "io_error"}  # fmt: skip
    assert res[lib["Alpha"]["id"]]["ok"] and res[lib["Gamma"]["id"]]["ok"]
    after = {w["id"]: w for w in client.get("/api/library").json()["works"]}
    beta = after[lib["Beta"]["id"]]
    assert beta["author"] == "Old Author" and beta["dir_path"] == lib["Beta"]["dir_path"]
    assert (settings.library_dir / beta["dir_path"] / "book.epub").is_file()
    assert after[lib["Alpha"]["id"]]["dir_path"].startswith("New Author/")
    # Audits match what happened: two edits, one summary naming the failure.
    assert len(audits("work.bulk_edit")) == 2
    assert audits("work.bulk")[-1].detail.endswith(
        f"2/3 works; failed {lib['Beta']['id']}:io_error"
    )
    with session_factory()() as db:
        assert db.query(WorkRevision).filter_by(work_id=lib["Beta"]["id"]).count() == 0
    # A retry with the same key replays the first answer and changes nothing more.
    monkeypatch.setattr("lectern.editing.move_dir", real)
    again = client.post("/api/works/bulk", json=body, headers=h)
    assert again.json() == r.json()
    assert len(audits("work.bulk_edit")) == 2


# --- 2. XML-illegal control characters ------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"metadata": {"title": "Bad\x01Title"}},
        {"metadata": {"title": "Ok", "authors": [{"name": "A\x00uthor"}]}},
        {"metadata": {"title": "Ok", "authors": [{"name": "Author", "sort": "S\x1fort"}]}},
        {"metadata": {"title": "Ok", "description": "vertical\x0btab"}},
        {"metadata": {"title": "Ok", "tags": ["fine", "bell\x07"]}},
        {"metadata": {"title": "Ok", "publisher": "del\x7f"}},
        {"tracks": [{"id": 1, "title": "Track\x02"}]},
        {"tracks": [{"id": 1, "chapters": ["One", "Two\x0c"]}]},
    ],
)
def test_edits_with_control_characters_are_refused_by_type(client, owner, settings, change):
    csrf = owner["csrf"]
    wid = one_book(settings, title="Clean", authors=["Writer"])
    ed = editor(client, wid)
    body = {"version": ed["version"], **change}
    if "metadata" in change:
        body["metadata"] = {**ed["metadata"], **change["metadata"]}
    r = patch(client, csrf, wid, body)
    assert r.status_code == 422, r.text
    assert {e["type"] for e in r.json()["errors"]} == {"control_characters"}
    assert editor(client, wid)["version"] == ed["version"]


def test_tab_newline_and_carriage_return_are_still_fine(client, owner, settings):
    csrf = owner["csrf"]
    wid = one_book(settings, title="Clean", authors=["Writer"])
    ed = editor(client, wid)
    meta = {**ed["metadata"], "description": "Line one\nLine two\r\n\tIndented"}
    r = patch(client, csrf, wid, {"version": ed["version"], "metadata": meta})
    assert r.status_code == 200, r.text
    assert editor(client, wid)["metadata"]["description"] == meta["description"]


def test_import_and_bulk_payloads_with_control_characters_are_refused(
    client, owner, settings, tmp_path
):
    csrf = owner["csrf"]
    imp = new_import(client, csrf)
    upload(client, csrf, imp["id"], make_epub(tmp_path / "x.epub", title="Draft"))
    info = complete(client, csrf, imp["id"])
    h = {"origin": ORIGIN, "x-csrf-token": csrf}
    r = client.patch(f"/api/imports/{imp['id']}", headers=h, json={"version": info["version"],
                     "metadata": {**info["metadata"], "series": "Ser\x03ies"}})  # fmt: skip
    assert r.status_code == 422 and r.json()["errors"][0]["type"] == "control_characters"
    wid = one_book(settings, title="Bulk", authors=["Writer"])
    for extra in ({"action": "set_series", "series": "S\x04"},
                  {"action": "add_tags", "tags": ["t\x05"]},
                  {"action": "set_authors", "authors": [{"name": "N\x06"}]}):  # fmt: skip
        r = api_post(client, "/api/works/bulk", csrf, {"work_ids": [wid], **extra})
        assert r.status_code == 422, (extra, r.text)
        assert r.json()["errors"][0]["type"] == "control_characters"


def test_text_from_files_and_providers_is_stripped_not_refused():
    from lectern import lookup
    from lectern.imports import _valid_metadata

    got = _valid_metadata({"title": "Ti\x01tle", "authors": [{"name": "Au\x02thor"}],
                           "tags": ["Fan\x03tasy"], "description": "a\x0bb\nc"})  # fmt: skip
    assert (got["title"], got["authors"][0]["name"], got["tags"]) == (
        "Title",
        "Author",
        ["Fantasy"],
    )
    assert got["description"] == "ab\nc"
    assert lookup._validated({"title": "T\x01", "publisher": "P\x1f"}) == {
        "title": "T",
        "publisher": "P",
    }


def test_opds_feed_stays_parseable_with_control_characters_in_old_rows(client, owner, settings):
    csrf = owner["csrf"]
    wid = one_book(settings, title="Fine", authors=["Writer"])
    with session_factory()() as db:  # written before validation existed, or by a scanner
        w = db.get(Work, wid)
        w.title, w.author = "Old\x01Title\x7f", "Au\x1fthor"
        w.description, w.publisher, w.language = "De\x0bsc\x00", "Pub\x08", "e\x02n"
        db.commit()
    pw = make_app_password(client, csrf, "Reader")
    basic = {"authorization": "Basic " + base64.b64encode(f"owner:{pw}".encode()).decode()}
    ns = {"a": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/terms/"}
    for url in ("/opds/all", "/opds/recent", "/opds/search?q=Old%01Title"):
        r = client.get(url, headers=basic)
        assert r.status_code == 200, url
        root = ElementTree.fromstring(r.content)  # raises on any XML-illegal character
        if url != "/opds/search?q=Old%01Title":
            entry = root.find("a:entry", ns)
            assert entry.find("a:title", ns).text == "OldTitle"
            assert entry.find("a:author/a:name", ns).text == "Author"
            assert entry.find("a:summary", ns).text == "Desc"


def test_upload_filename_control_characters_are_dropped(client, owner):
    csrf = owner["csrf"]
    r = api_post(client, "/api/uploads", csrf, {"filename": "di\x00r/bo\x01ok.epub", "size": 10,
                 "media_type": "application/epub+zip"})  # fmt: skip
    assert r.status_code == 201 and r.json()["filename"] == "book.epub"


# --- 3. upload extensions --------------------------------------------------------------------------


def test_stored_extension_comes_from_the_media_type_allowlist():
    from lectern.uploads import media_suffix

    assert media_suffix("Book.EPUB", "application/epub+zip") == ".epub"
    assert media_suffix("evil.exe", "application/epub+zip") == ".epub"
    assert media_suffix("book." + "e" * 500, "application/epub+zip") == ".epub"
    assert media_suffix("no-extension", "audio/mpeg") == ".mp3"
    assert media_suffix("a\x00.mp3", "audio/mpeg") == ".mp3"
    assert media_suffix("track.mp3", "audio/mp4") == ".m4b"
    assert media_suffix("Part 1.M4A", "audio/mp4") == ".m4a"
    assert media_suffix("x.m4b", "audio/mp4") == ".m4b"
    assert media_suffix("../../x.epub/..", "application/epub+zip") == ".epub"


def start_upload(client, csrf, import_id, data: bytes, name: str) -> str:
    r = api_post(client, "/api/uploads", csrf, {"filename": name, "size": len(data),
                 "media_type": "application/epub+zip", "import_id": import_id})  # fmt: skip
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    for off in range(0, len(data), CHUNK):
        part = data[off : off + CHUNK]
        r = client.put(f"/api/uploads/{uid}/chunk", content=part, headers={"origin": ORIGIN,
                       "x-csrf-token": csrf, "upload-offset": str(off), "x-chunk-sha256": sha(part),
                       "content-type": "application/octet-stream"})  # fmt: skip
        assert r.status_code == 204, r.text
    return uid


@pytest.mark.parametrize(
    "name",
    [
        "book." + "x" * 400,
        "evil.exe",
        "../../etc/passwd.epub",
        "nul\x00name.epub",
        ".epub",
        "Ünï.EPUB",
    ],
)
def test_staged_import_files_get_a_safe_name_and_allowed_extension(
    client, owner, settings, tmp_path, name
):
    csrf = owner["csrf"]
    data = make_epub(tmp_path / "b.epub", title="Named").read_bytes()
    imp = new_import(client, csrf)
    uid = start_upload(client, csrf, imp["id"], data, name)
    r = api_post(client, f"/api/uploads/{uid}/finalize", csrf, {"sha256": sha(data)})
    assert r.status_code == 200, r.text
    with session_factory()() as db:
        row = db.query(Import).filter_by(public_id=imp["id"]).one()
        (f,) = row.files
        rel = f.rel_path
    assert rel.endswith(".epub") and len(rel) < 100 and "/" not in rel and "\x00" not in rel
    staged = settings.library_dir / "_staging" / imp["id"]
    assert [p.name for p in staged.iterdir() if p.is_file()] == [rel]
    assert sha256_file(staged / rel) == sha(data)  # bytes untouched


def test_refused_staging_fails_the_upload_and_leaves_nothing(client, owner, settings, tmp_path,
                                                             monkeypatch):  # fmt: skip
    csrf = owner["csrf"]
    data = make_epub(tmp_path / "b.epub", title="Full").read_bytes()
    imp = new_import(client, csrf)
    monkeypatch.setattr("lectern.imports.MAX_FILES", 0)
    uid = start_upload(client, csrf, imp["id"], data, "full.epub")
    r = api_post(client, f"/api/uploads/{uid}/finalize", csrf, {"sha256": sha(data)})
    assert r.status_code == 413 and r.json()["detail"] == "too_many_files"
    s = client.get(f"/api/uploads/{uid}").json()
    assert (s["status"], s["error"]) == ("failed", "too_many_files")
    assert not part_path(settings, uid).exists()
    # A filesystem error while staging: a clean JSON error, the session failed, no bytes left.
    monkeypatch.setattr("lectern.imports.MAX_FILES", 1000)

    def broken(src, dst, **kw):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("lectern.imports.move_file", broken)
    uid = start_upload(client, csrf, imp["id"], data, "full.epub")
    r = api_post(client, f"/api/uploads/{uid}/finalize", csrf, {"sha256": sha(data)})
    assert r.status_code == 500 and r.json()["detail"] == "staging_failed"
    with session_factory()() as db:
        row = db.query(UploadSession).filter_by(public_id=uid).one()
        assert row.status == UploadStatus.FAILED
    assert not part_path(settings, uid).exists()
    staged = settings.library_dir / "_staging" / imp["id"]
    assert not staged.exists() or not any(staged.iterdir())


# --- 4. trash purge with a relative or symlinked library ------------------------------------------


@pytest.mark.parametrize("how", ["symlink", "relative"])
def test_purge_resolves_the_library_path(client, owner, settings, tmp_path, monkeypatch, how):
    csrf = owner["csrf"]
    wid, _first, second = two_epubs(client, settings)
    remove_second(client, csrf, settings, wid, second)
    api_post(client, f"/api/works/{wid}/trash", csrf)
    if how == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(settings.library_dir)
    else:
        monkeypatch.chdir(tmp_path)
        alias = Path(os.path.relpath(settings.library_dir, tmp_path))
    odd = settings.model_copy(update={"library_dir": alias})
    with session_factory()() as db:
        w = db.get(Work, wid)
        folder = settings.library_dir / w.dir_path
        tf_path = settings.library_dir / db.query(TrashedFile).one().trash_path
        assert folder.is_dir() and tf_path.is_file()
        w.trashed_at = utcnow() - trash.TRASH_RETENTION * 2
        db.commit()
        assert trash.purge_expired(db, odd)["works"] == 1
        db.commit()
        assert db.get(Work, wid) is None and db.query(TrashedFile).count() == 0
    assert not folder.exists() and not tf_path.exists()


def test_purge_never_deletes_outside_the_trash(client, owner, settings):
    csrf = owner["csrf"]
    wid = one_book(settings, title="Escape", authors=["Writer"])
    api_post(client, f"/api/works/{wid}/trash", csrf)
    outside = settings.library_dir / "Keep" / "Me"
    outside.mkdir(parents=True)
    (outside / "book.epub").write_bytes(b"keep")
    with session_factory()() as db:
        w = db.get(Work, wid)
        w.dir_path = "_trash/../Keep/Me"  # a corrupted row must not reach outside _trash
        trash.purge_work(db, settings, w)
        db.commit()
    assert (outside / "book.epub").read_bytes() == b"keep"


# --- 5. covers --------------------------------------------------------------------------------------


def png_claiming(width: int, height: int) -> bytes:
    """A tiny PNG whose header claims the given canvas (a decompression bomb's shape)."""
    raw = png()
    ihdr = struct.pack(">II", width, height) + raw[24:29]
    crc = struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
    return raw[:16] + ihdr + crc + raw[33:]


def image_bytes(fmt: str) -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (40, 60), (200, 40, 40)).save(buf, fmt)
    return buf.getvalue()


def test_cover_upload_accepts_only_jpeg_png_webp_and_refuses_bombs(client, owner, settings):
    csrf = owner["csrf"]
    wid = one_book(settings, title="Covers", authors=["Writer"])
    h = {"origin": ORIGIN, "x-csrf-token": csrf}

    def send(data):
        return client.post(f"/api/works/{wid}/cover", content=data, headers=h)

    for fmt in ("JPEG", "PNG", "WEBP"):
        assert send(image_bytes(fmt)).status_code == 201, fmt
    for fmt in ("GIF", "BMP", "TIFF", "ICO"):
        r = send(image_bytes(fmt))
        assert (r.status_code, r.json()["detail"]) == (422, "not_an_image"), fmt
    for w, h_ in ((50_000, 50_000), (7_000, 7_000), (20_000, 10)):
        r = send(png_claiming(w, h_))
        assert (r.status_code, r.json()["detail"]) == (422, "cover_too_large"), (w, h_)


def test_embedded_covers_are_bounded_before_decoding(tmp_path, monkeypatch):
    from lectern.ingest import epub
    from lectern.ingest.audio import _save_cover

    dest = tmp_path / "out" / "cover.jpg"
    assert _save_cover(image_bytes("PNG"), dest) and dest.is_file()
    assert _save_cover(image_bytes("GIF"), tmp_path / "gif.jpg")  # embedded GIFs still work
    assert not _save_cover(png_claiming(50_000, 50_000), tmp_path / "bomb.jpg")
    assert not _save_cover(png_claiming(7_000, 7_000), tmp_path / "big.jpg")
    assert not _save_cover(image_bytes("BMP"), tmp_path / "bmp.jpg")
    assert not _save_cover(b"\x89PNG" + b"\0" * (10 * 1024 * 1024), tmp_path / "huge.jpg")
    assert not (tmp_path / "bomb.jpg").exists() and not (tmp_path / "big.jpg").exists()

    book = tmp_path / "b.epub"
    with zipfile.ZipFile(book, "w") as zf:
        zf.writestr("bomb.png", png_claiming(50_000, 50_000))
        zf.writestr("ok.png", image_bytes("PNG"))
        zf.writestr("big.png", b"\0" * 2048, compress_type=zipfile.ZIP_DEFLATED)
    assert epub.extract_cover(book, "ok.png", tmp_path / "ok.jpg")
    assert not epub.extract_cover(book, "bomb.png", tmp_path / "bomb2.jpg")
    assert not epub.extract_cover(book, "missing.png", tmp_path / "m.jpg")
    monkeypatch.setattr(epub, "MAX_COVER_BYTES", 1024)
    assert not epub.extract_cover(book, "big.png", tmp_path / "big2.jpg")
    # A header that understates the entry's size is still cut off at the cap.
    lying = tmp_path / "lying.epub"
    with zipfile.ZipFile(book) as src, zipfile.ZipFile(lying, "w") as zf:
        zf.writestr("big.png", src.read("big.png"), compress_type=zipfile.ZIP_DEFLATED)
    raw = bytearray(lying.read_bytes())
    cd = raw.rfind(b"PK\x01\x02")
    struct.pack_into("<I", raw, cd + 24, 10)  # central directory: uncompressed size 10
    lying.write_bytes(bytes(raw))
    assert not epub.extract_cover(lying, "big.png", tmp_path / "lie.jpg")


# --- 5. merge cleanup -------------------------------------------------------------------------------


def merge_pair(client, csrf, settings):
    """Source: two EPUBs, one removed (in the trash), an edited cover (a revision backup)."""
    wid, _first, second = two_epubs(client, settings)
    remove_second(client, csrf, settings, wid, second)
    h = {"origin": ORIGIN, "x-csrf-token": csrf}
    token = client.post(f"/api/works/{wid}/cover", content=png(), headers=h).json()["token"]
    patch(client, csrf, wid, {"version": editor(client, wid)["version"],
          "cover": {"action": "upload", "token": token}})  # fmt: skip
    target = one_book(settings, title="Target Book", authors=["Other Writer"])
    assert (settings.data_dir / "revisions" / str(wid)).is_dir()
    return wid, target, second


def test_merge_carries_trashed_formats_and_drops_source_history(client, owner, settings):
    csrf = owner["csrf"]
    source, target, second = merge_pair(client, csrf, settings)
    src_dir = settings.library_dir / editor(client, source)["work"]["dir_path"]
    kept = [f["sha256"] for f in editor(client, source)["files"]]
    r = api_post(client, f"/api/works/{source}/merge", csrf, {"target_work_id": target})
    assert r.status_code == 200, r.text
    assert not src_dir.exists()
    assert not (settings.data_dir / "revisions" / str(source)).exists()
    with session_factory()() as db:
        assert db.get(Work, source) is None
        assert db.query(WorkRevision).filter_by(work_id=source).count() == 0
        tf = db.query(TrashedFile).one()
        assert tf.work_id == target and (settings.library_dir / tf.trash_path).is_file()
        tf_id = tf.id
    t = editor(client, target)
    tdir = settings.library_dir / t["work"]["dir_path"]
    assert all(sha256_file(tdir / f["rel_path"]) == f["sha256"] for f in t["files"])
    assert set(kept) <= {f["sha256"] for f in t["files"]}
    # The removed format is still recoverable, now into the merged work.
    trash_list = client.get("/api/trash").json()["files"]
    assert trash_list[0]["work"]["id"] == target
    r = api_post(client, f"/api/trash/files/{tf_id}/restore", csrf)
    assert r.status_code == 200, r.text
    assert second["sha256"] in {f["sha256"] for f in editor(client, target)["files"]}


def test_failed_merge_commit_puts_every_file_back(client, owner, settings, monkeypatch):
    csrf = owner["csrf"]
    source, target, _second = merge_pair(client, csrf, settings)
    sed, ted = editor(client, source), editor(client, target)
    src_dir = settings.library_dir / sed["work"]["dir_path"]
    before = sorted(p.relative_to(src_dir).as_posix() for p in src_dir.rglob("*") if p.is_file())

    def boom(*a, **k):
        raise RuntimeError("database went away")

    monkeypatch.setattr("lectern.routes.api.works.write_book_json", boom)
    with pytest.raises(RuntimeError):
        api_post(client, f"/api/works/{source}/merge", csrf, {"target_work_id": target})
    after = sorted(p.relative_to(src_dir).as_posix() for p in src_dir.rglob("*") if p.is_file())
    assert after == before
    assert all(sha256_file(src_dir / f["rel_path"]) == f["sha256"] for f in sed["files"])
    assert (settings.data_dir / "revisions" / str(source)).is_dir()
    assert editor(client, target)["files"] == ted["files"]
    with session_factory()() as db:
        assert db.get(Work, source) is not None
        assert db.query(TrashedFile).one().work_id == source


# --- 5. journalled trash and restore --------------------------------------------------------------


def fail_audit(monkeypatch, action: str):
    from lectern.routes.api import editor as routes

    real = routes._audit

    def boom(db, request, auth, act, *a, **k):
        if act == action:
            raise RuntimeError("commit would fail")
        return real(db, request, auth, act, *a, **k)

    monkeypatch.setattr(routes, "_audit", boom)


def test_failed_trash_restore_and_file_restore_put_files_back(client, owner, settings, monkeypatch):
    csrf = owner["csrf"]
    wid, _first, second = two_epubs(client, settings)
    ed = editor(client, wid)
    folder = settings.library_dir / ed["work"]["dir_path"]
    digests = {f["rel_path"]: f["sha256"] for f in ed["files"]}

    fail_audit(monkeypatch, "work.trash")
    with pytest.raises(RuntimeError):
        api_post(client, f"/api/works/{wid}/trash", csrf)
    assert all(sha256_file(folder / rel) == d for rel, d in digests.items())
    assert not list((settings.library_dir / "_trash" / "works").glob("*"))
    assert editor(client, wid)["version"] == ed["version"]
    monkeypatch.undo()

    assert api_post(client, f"/api/works/{wid}/trash", csrf).status_code == 200
    with session_factory()() as db:
        trashed = settings.library_dir / db.get(Work, wid).dir_path
    fail_audit(monkeypatch, "work.restore")
    with pytest.raises(RuntimeError):
        api_post(client, f"/api/trash/works/{wid}/restore", csrf)
    assert trashed.is_dir() and not folder.exists()
    with session_factory()() as db:
        assert db.get(Work, wid).trashed_at is not None
    monkeypatch.undo()
    assert api_post(client, f"/api/trash/works/{wid}/restore", csrf).status_code == 200

    remove_second(client, csrf, settings, wid, second)
    with session_factory()() as db:
        tf = db.query(TrashedFile).one()
        tf_id, tf_path = tf.id, settings.library_dir / tf.trash_path
    fail_audit(monkeypatch, "file.restore")
    with pytest.raises(RuntimeError):
        api_post(client, f"/api/trash/files/{tf_id}/restore", csrf)
    assert tf_path.is_file() and sha256_file(tf_path) == second["sha256"]
    assert not (folder / second["rel_path"]).exists()
    monkeypatch.undo()
    r = api_post(client, f"/api/trash/files/{tf_id}/restore", csrf)
    assert r.status_code == 200 and sha256_file(folder / second["rel_path"]) == second["sha256"]
    assert api_post(client, f"/api/trash/files/{tf_id}/restore", csrf).status_code == 404


# --- 5. revisions are opaque ------------------------------------------------------------------------


def test_revision_history_exposes_no_server_paths(client, owner, settings):
    csrf = owner["csrf"]
    wid = one_book(settings, title="Opaque", authors=["Writer"], cover=True)
    h = {"origin": ORIGIN, "x-csrf-token": csrf}
    token = client.post(f"/api/works/{wid}/cover", content=png(), headers=h).json()["token"]
    ed = editor(client, wid)
    patch(client, csrf, wid, {"version": ed["version"], "cover": {"action": "upload", "token": token},
          "metadata": {**ed["metadata"], "publisher": "New"}})  # fmt: skip
    with session_factory()() as db:
        rev = db.query(WorkRevision).one()
        backup = rev.before["cover"]["backup"]
        assert backup.startswith("revisions/")  # kept server-side for undo
    r = client.get(f"/api/works/{wid}/revisions")
    text = r.text
    assert backup not in text and "revisions/" not in text and str(settings.data_dir) not in text
    (entry,) = r.json()["revisions"]
    assert set(entry) == {"id", "kind", "actor", "created_at", "version_before", "version_after",
                          "changed", "undone_at", "batch_id", "undoable"}  # fmt: skip
    assert entry["changed"] == ["cover", "metadata"] and entry["undoable"] is True
    # Undo works from the opaque id alone.
    r = api_post(client, f"/api/works/{wid}/undo", csrf, {"revision_id": entry["id"]})
    assert r.status_code == 200 and r.json()["metadata"]["publisher"] == "Test Press"
    (entry,) = client.get(f"/api/works/{wid}/revisions").json()["revisions"]
    assert entry["undoable"] is False and entry["undone_at"]
