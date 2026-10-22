"""DRM-free MOBI and AZW3 books converted to EPUB at ingest; DRM-protected ones refused.

The fixtures (tests/fixtures/kindle, see PROVENANCE.md) are calibre-built from a tiny original
EPUB, so expected values (title, two authors, ISBN, ASIN, cover colour, chapter text) come from
that source, never from the code under test. DRM-flagged copies are made here by setting the
encryption field of a fixture's MOBI header, which is all a reader looks at before refusing.
"""

from __future__ import annotations

import io
import os
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from tests.conftest import api_post, drain_inbox, make_epub
from tests.test_pdf_comics import file_import, new_import, send, upload, works

from lectern.ingest import formats, kindle
from lectern.ingest.epub import read_epub_metadata, read_spine
from lectern.models import BookFile, UploadSession

FIXTURES = Path(__file__).parent / "fixtures" / "kindle"
MOBI, AZW3 = formats.MOBI, formats.AZW3
TITLE = "The Lighthouse Keeper’s Almanac"
KINDLE_FILES = [
    ("lighthouse.mobi", MOBI),
    ("lighthouse-joint.mobi", MOBI),
    ("lighthouse.azw3", AZW3),
]


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch, settings):
    monkeypatch.setattr("lectern.routes.api.uploads.UPLOAD_CHUNK_MAX_BYTES", 4096)
    settings.inbox_path.mkdir(parents=True, exist_ok=True)


def _records(data: bytes) -> list[int]:
    n = struct.unpack_from(">H", data, 76)[0]
    return [struct.unpack_from(">I", data, 78 + 8 * i)[0] for i in range(n)]


def drm_copy(src: Path, dest: Path, *, kf8_only: bool = False) -> Path:
    """``src`` with the MOBI header's encryption type set to 2 (Mobipocket/Kindle DRM): in
    record 0, or only in the KF8 header of a joint file (after its BOUNDARY record)."""
    data = bytearray(src.read_bytes())
    offsets = _records(bytes(data))
    targets = [offsets[0]]
    if kf8_only:
        ends = offsets[1:] + [len(data)]
        boundary = next(
            i for i, (a, b) in enumerate(zip(offsets, ends, strict=True))
            if data[a:b] == b"BOUNDARY"
        )  # fmt: skip
        targets = [offsets[boundary + 1]]
    for at in targets:
        assert data[at + 16 : at + 20] == b"MOBI"
        struct.pack_into(">H", data, at + 12, 2)
    dest.write_bytes(bytes(data))
    return dest


def epubcheck(path: Path) -> None:
    jar = os.environ.get("EPUBCHECK_JAR")
    if not jar:
        if os.environ.get("EPUBCHECK_REQUIRED"):
            pytest.fail("EPUBCHECK_REQUIRED is set but EPUBCHECK_JAR is not")
        pytest.skip("set EPUBCHECK_JAR to run epubcheck")
    res = subprocess.run(
        ["java", "-jar", jar, str(path)], capture_output=True, text=True, timeout=120
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "0 errors / 0 warnings" in res.stdout, res.stdout


# --- conversion ------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "media_type"), KINDLE_FILES)
def test_conversion_is_a_valid_epub_with_the_books_metadata_text_and_cover(
    tmp_path, name, media_type
):
    out = tmp_path / "book.epub"
    formats.to_epub(FIXTURES / name, media_type, out)
    epubcheck(out)
    meta = read_epub_metadata(out)
    assert meta.title == TITLE
    assert meta.authors == ["Ada Quill", "Bram Ellis"]
    assert (meta.isbn, meta.language, meta.publisher) == (
        "9780000000002",
        "en",
        "Lectern Test Fixtures",
    )
    assert "mobi-asin:B0LECTERN1" in meta.identifiers
    assert meta.published == "2024-03-05"
    assert set(meta.tags) == {"Lighthouses", "Weather"}
    assert meta.cover_href
    with zipfile.ZipFile(out) as zf:
        cover = Image.open(io.BytesIO(zf.read(meta.cover_href))).convert("RGB")
        text = " ".join(zf.read(n).decode() for n in read_spine(out))
        names = zf.namelist()
    assert cover.size == (600, 900)
    r, g, b = cover.getpixel((20, 20))
    assert b > 70 and r < 40  # the dark blue of the source cover, not the lamp drawing
    assert "corrected him every week" in text and "a little wrong" in text
    assert "café" in text and "naïve" in text  # encoding survived
    assert any(n.endswith((".png", ".jpeg", ".jpg")) and "cover" not in n for n in names)
    assert "<script" not in text


def test_conversion_is_deterministic(tmp_path):
    a, b = tmp_path / "a.epub", tmp_path / "b.epub"
    formats.to_epub(FIXTURES / "lighthouse.azw3", AZW3, a)
    formats.to_epub(FIXTURES / "lighthouse.azw3", AZW3, b)
    assert a.read_bytes() == b.read_bytes()


@pytest.mark.parametrize(
    ("name", "kf8_only"),
    [("lighthouse.mobi", False), ("lighthouse.azw3", False), ("lighthouse-joint.mobi", True)],
)
def test_drm_is_refused_before_any_parser_runs(tmp_path, monkeypatch, name, kf8_only):
    locked = drm_copy(FIXTURES / name, tmp_path / name, kf8_only=kf8_only)
    monkeypatch.setattr(kindle, "_convert", lambda *a: pytest.fail("parser ran on a DRM file"))
    with pytest.raises(formats.FormatError) as exc:
        formats.to_epub(locked, formats.media_type_for(locked), tmp_path / "out.epub")
    assert exc.value.code == "drm_protected"
    assert str(exc.value) == "This book is DRM-protected; Lectern can only add DRM-free books."
    assert not (tmp_path / "out.epub").exists()


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"PK\x03\x04" + b"\0" * 200, "not_mobi"),  # a zip renamed .mobi
        (b"\0" * 60 + b"BOOKMOBI" + b"\0" * 10, "not_mobi"),  # no records
        (b"\0" * 60 + b"TEXtREAd" + b"\0" * 200, "not_mobi"),  # PalmDoc, not MOBI
    ],
)
def test_wrong_files_are_refused(tmp_path, data, code):
    path = tmp_path / "x.mobi"
    path.write_bytes(data)
    with pytest.raises(formats.FormatError) as exc:
        formats.to_epub(path, MOBI, tmp_path / "out.epub")
    assert exc.value.code == code


def test_hostile_files_fail_safely(tmp_path):
    good = (FIXTURES / "lighthouse.azw3").read_bytes()
    # Truncated after the headers: the header check passes, the child fails cleanly.
    cut = tmp_path / "cut.azw3"
    cut.write_bytes(good[: _records(good)[2] + 10])
    # Record offsets pointing past the end of the file.
    lying = bytearray(good)
    struct.pack_into(">I", lying, 78 + 8, len(good) + 10_000)
    liar = tmp_path / "liar.azw3"
    liar.write_bytes(bytes(lying))
    # Text records replaced with junk.
    offs = _records(good)
    junk = bytearray(good)
    junk[offs[1] : offs[1] + 4000] = os.urandom(4000)
    noise = tmp_path / "noise.azw3"
    noise.write_bytes(bytes(junk))
    for path in (cut, liar, noise):
        out = tmp_path / f"{path.stem}.epub"
        try:
            formats.to_epub(path, AZW3, out)
        except formats.FormatError as exc:
            assert exc.code in {"not_mobi", "mobi_too_complex"}, (path.name, exc.code)
            assert not out.exists()
        else:  # junk text that still unpacks must still be a well-formed EPUB
            read_epub_metadata(out)


def test_a_converter_that_runs_away_is_stopped(tmp_path, monkeypatch):
    """The child is killed at the wall clock and its limits; a hang is a refusal, not a stuck
    server. A child that never answers stands in for a parser stuck in a loop."""
    monkeypatch.setattr(kindle, "TIMEOUT_S", 1)
    real = subprocess.run

    def stuck(argv, **kw):
        return real([argv[0], "-c", "import time; time.sleep(30)"], **kw)

    monkeypatch.setattr(kindle.subprocess, "run", stuck)
    with pytest.raises(formats.FormatError) as exc:
        formats.to_epub(FIXTURES / "lighthouse.mobi", MOBI, tmp_path / "out.epub")
    assert exc.value.code == "mobi_too_complex"


def test_child_limits_its_own_memory(tmp_path):
    """The child sets RLIMIT_AS before it parses: allocating past it fails inside the child."""
    code = (
        "from lectern.ingest import kindle; kindle._limit(kindle.FILE_LIMIT_BYTES)\n"
        "try:\n  b = bytearray(kindle.MEMORY_LIMIT_BYTES + (1 << 28))\n  print('allocated')\n"
        "except MemoryError:\n  print('refused')\n"
    )
    res = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True, text=True, timeout=60, env={**os.environ},
    )  # fmt: skip
    assert res.stdout.strip() == "refused", res.stdout + res.stderr


# --- every ingest path -----------------------------------------------------------------------


def test_upload_into_review_files_an_epub_that_joins_the_existing_work(
    client, owner, settings, tmp_path
):
    csrf = owner["csrf"]
    make_epub(settings.inbox_path / "a.epub", title=TITLE, authors=["Ada Quill"])
    drain_inbox(settings)
    existing = works(client)[TITLE]

    iid = new_import(client, csrf)
    r = upload(client, csrf, FIXTURES / "lighthouse.azw3", AZW3, iid)
    assert r.status_code == 200, r.text
    imp = client.get(f"/api/imports/{iid}").json()
    (f,) = imp["files"]
    assert (f["media_type"], f["converted_from"], f["kind"]) == (formats.EPUB, "AZW3", "ebook")
    out = file_import(client, csrf, iid)
    det = client.get(f"/api/imports/{iid}").json()["detected"]
    assert [a["name"] for a in det["authors"]] == ["Ada Quill", "Bram Ellis"]
    assert det["isbn"] == "9780000000002"
    assert out["work_id"] == existing["id"]  # exact title and first author: certain match

    w = client.get(f"/api/works/{existing['id']}").json()
    epubs = sorted(x["rel_path"] for x in w["files"] if x["media_type"] == formats.EPUB)
    assert len(epubs) == 2 and epubs[0].startswith("book-") and epubs[1] == "book.epub"
    assert not list(settings.library_dir.rglob("*.azw3"))
    assert not list(settings.library_dir.rglob("*.mobi"))


def test_new_work_from_a_mobi_gets_its_metadata_and_cover(client, owner, settings):
    csrf = owner["csrf"]
    iid = new_import(client, csrf)
    assert upload(client, csrf, FIXTURES / "lighthouse.mobi", MOBI, iid).status_code == 200
    wid = file_import(client, csrf, iid)["work_id"]
    w = client.get(f"/api/works/{wid}").json()
    assert w["title"] == TITLE and w["isbn"] == "9780000000002" and w["language"] == "en"
    assert [f["rel_path"] for f in w["files"]] == ["book.epub"]
    assert w["has_cover"]
    cover = Image.open(io.BytesIO(client.get(w["cover_url"]).content)).convert("RGB")
    r, g, b = cover.getpixel((10, 10))
    assert b > 70 and r < 40


def test_plain_upload_reaches_the_inbox_as_an_epub(client, owner, settings):
    csrf = owner["csrf"]
    r = upload(client, csrf, FIXTURES / "lighthouse-joint.mobi", MOBI)
    assert r.status_code == 200, r.text
    assert r.json()["queued"].endswith(".epub") and r.json()["title"] == TITLE
    assert not list(settings.inbox_path.glob("*.mobi"))
    drain_inbox(settings)
    assert works(client)[TITLE]["author"] == "Ada Quill"


def test_inbox_converts_mobi_and_azw3_and_removes_the_original(client, owner, settings):
    shutil.copy(FIXTURES / "lighthouse.azw3", settings.inbox_path / "almanac.azw3")
    drain_inbox(settings)
    w = works(client)[TITLE]
    files = client.get(f"/api/works/{w['id']}").json()["files"]
    assert [(f["rel_path"], f["media_type"]) for f in files] == [("book.epub", formats.EPUB)]
    assert not (settings.inbox_path / "almanac.azw3").exists()
    assert not list(settings.inbox_path.glob(".converting-*"))
    # The same book as MOBI is a different conversion of the same work: it joins it.
    shutil.copy(FIXTURES / "lighthouse.mobi", settings.inbox_path / "almanac.mobi")
    drain_inbox(settings)
    files = client.get(f"/api/works/{w['id']}").json()["files"]
    assert len(files) == 2 and len(works(client)) == 1


@pytest.mark.parametrize("into_import", [False, True])
def test_drm_upload_is_refused_in_plain_words_and_nothing_is_filed(
    client, owner, settings, tmp_path, into_import
):
    csrf = owner["csrf"]
    locked = drm_copy(FIXTURES / "lighthouse.azw3", tmp_path / "locked.azw3")
    iid = new_import(client, csrf) if into_import else None
    r = upload(client, csrf, locked, AZW3, iid)
    assert (r.status_code, r.json()["detail"]) == (422, "drm_protected")
    assert not [p for p in settings.inbox_path.iterdir() if p.is_file()]
    assert not list(settings.library_dir.rglob("*.epub"))
    assert not list((settings.data_dir / "uploads").glob("*"))
    from lectern.db import session_factory

    with session_factory()() as db:
        assert db.query(BookFile).count() == 0
        (row,) = db.query(UploadSession).all()
        assert (row.status.value, row.error) == ("failed", "drm_protected")
    if into_import:
        assert client.get(f"/api/imports/{iid}").json()["files"] == []


def test_inbox_leaves_a_drm_file_where_it_is(client, owner, settings, caplog):
    locked = drm_copy(FIXTURES / "lighthouse.mobi", settings.inbox_path / "locked.mobi")
    with caplog.at_level("WARNING", logger="lectern.ingest"):
        drain_inbox(settings)
    assert locked.is_file() and not (settings.inbox_path / "_rejected" / "locked.mobi").exists()
    assert works(client) == {}
    said = [r.getMessage() for r in caplog.records if "locked.mobi" in r.getMessage()]
    assert len(said) == 1 and "DRM-protected; Lectern can only add DRM-free books" in said[0]


def test_mcp_upload_refuses_drm_with_the_message(client, owner, tmp_path):
    """The MCP tool goes through the same finalize; its error carries the plain message."""
    from fastapi import HTTPException

    from lectern.mcp_server import _http_error

    err = _http_error(HTTPException(422, {"detail": "drm_protected"}))
    assert str(err) == (
        "drm_protected (This book is DRM-protected; Lectern can only add DRM-free books.)"
    )
    from lectern.mcp_server import _EXTENSIONS

    assert _EXTENSIONS[".mobi"] == MOBI and _EXTENSIONS[".azw3"] == AZW3


def test_a_busy_converter_keeps_the_upload_for_a_retry(client, owner, settings, monkeypatch):
    csrf = owner["csrf"]
    monkeypatch.setattr(kindle, "BUSY_WAIT_S", 0.01)
    uid, digest = send(client, csrf, FIXTURES / "lighthouse.mobi", MOBI)
    for _ in range(formats.MAX_PARSERS):
        formats.PARSER_SLOTS.acquire()
    try:
        r = api_post(client, f"/api/uploads/{uid}/finalize", csrf, {"sha256": digest})
        assert (r.status_code, r.json()["detail"]) == (503, "convert_busy")
    finally:
        for _ in range(formats.MAX_PARSERS):
            formats.PARSER_SLOTS.release()
    r = api_post(client, f"/api/uploads/{uid}/finalize", csrf, {"sha256": digest})
    assert r.status_code == 200, r.text
