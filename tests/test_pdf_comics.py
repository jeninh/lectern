"""PDF and CBZ comics: magic bytes and parser limits, metadata and covers, filing into works next
to an EPUB, editing and trash, OPDS media types, device queues, KOReader page sync, the web
page viewer, and read-along shown as unavailable without an EPUB.

Expected values come from the fixtures built here (titles, authors, page counts, digests),
never from the code under test.
"""

from __future__ import annotations

import base64
import hashlib
import io
import threading
import time
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from lxml import etree
from PIL import Image
from pypdf import PdfWriter
from tests.conftest import (
    ORIGIN,
    api_post,
    drain_inbox,
    ko_headers,
    login,
    make_epub,
    make_user,
    put_json,
)

from lectern.db import session_factory
from lectern.ingest import formats
from lectern.ingest import pdf as pdf_ingest
from lectern.ingest.comic import MAX_ENTRY_BYTES, open_comic
from lectern.ingest.hashing import partial_md5
from lectern.models import AuditLog, BookFile, DownloadEvent, ReadingProgress, UploadSession, Work

PDF = "application/pdf"
CBZ = "application/vnd.comicbook+zip"
CHUNK = 4096
ATOM = "{http://www.w3.org/2005/Atom}"


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch, settings):
    monkeypatch.setattr("lectern.routes.api.uploads.UPLOAD_CHUNK_MAX_BYTES", CHUNK)
    settings.inbox_path.mkdir(parents=True, exist_ok=True)


def make_pdf(
    path: Path,
    *,
    title: str | None = "The Atlas of Small Rivers",
    author: str | None = "Ada Lovelace; Grace Hopper",
    pages: int = 3,
    color=(10, 120, 200),
    password: str | None = None,
    javascript: bool = False,
) -> Path:
    imgs = [Image.new("RGB", (200, 300), color)] + [
        Image.new("RGB", (200, 300), (255, 255, 255)) for _ in range(pages - 1)
    ]
    raw = io.BytesIO()
    imgs[0].save(raw, "PDF", save_all=True, append_images=imgs[1:])
    w = PdfWriter(clone_from=io.BytesIO(raw.getvalue()))
    meta = {}
    if title:
        meta["/Title"] = title
    if author:
        meta["/Author"] = author
    if meta:
        w.add_metadata(meta)
    if javascript:
        with warnings.catch_warnings():  # pypdf renamed the call; the PDF it writes is the same
            warnings.simplefilter("ignore", DeprecationWarning)
            w.add_js("app.alert('hello');")
    if password:
        w.encrypt(password)
    w.write(path)
    return path


def png(color=(200, 30, 30), size=(60, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def make_cbz(path: Path, *, pages: int = 3, comicinfo: str | None = None, cover_color=(0, 160, 60)):
    with zipfile.ZipFile(path, "w") as z:
        # Stored out of order, with names that sort wrongly as plain strings (10 < 2).
        for n in reversed(range(1, pages + 1)):
            z.writestr(f"pages/{n}.png", png(cover_color if n == 1 else (n * 20, n * 20, n * 20)))
        z.writestr("__MACOSX/._1.png", b"junk")
        if comicinfo is not None:
            z.writestr("ComicInfo.xml", comicinfo)
    return path


SAGA_INFO = """<?xml version="1.0"?>
<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Series>Saga</Series><Number>3</Number><Title>Chapter Three</Title>
  <Writer>Brian K. Vaughan</Writer><Penciller>Fiona Staples</Penciller>
  <Publisher>Image</Publisher><Year>2012</Year><Month>5</Month>
  <Genre>Science Fiction, Fantasy</Genre><LanguageISO>en</LanguageISO>
  <Summary>Hazel's parents run.</Summary><Manga>YesAndRightToLeft</Manga>
</ComicInfo>"""


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def upload(client, csrf, path: Path, media_type: str, import_id: str | None = None):
    uid, digest = send(client, csrf, path, media_type, import_id)
    return api_post(client, f"/api/uploads/{uid}/finalize", csrf, {"sha256": digest})


def send(client, csrf, path: Path, media_type: str, import_id: str | None = None):
    """Open an upload session and send every chunk; returns (upload id, sha256) unfinalized."""
    data = path.read_bytes()
    body = {"filename": path.name, "size": len(data), "media_type": media_type}
    if import_id:
        body["import_id"] = import_id
    r = api_post(client, "/api/uploads", csrf, body)
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    for off in range(0, len(data), CHUNK):
        part = data[off : off + CHUNK]
        r = client.put(
            f"/api/uploads/{uid}/chunk",
            content=part,
            headers={"origin": ORIGIN, "x-csrf-token": csrf, "upload-offset": str(off),
                     "x-chunk-sha256": sha(part), "content-type": "application/octet-stream"},
        )  # fmt: skip
        assert r.status_code == 204, r.text
    return uid, sha(data)


def new_import(client, csrf) -> str:
    r = api_post(client, "/api/imports", csrf, {"label": ""})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def file_import(client, csrf, iid, body=None) -> dict:
    r = api_post(client, f"/api/imports/{iid}/complete", csrf)
    assert r.status_code == 200, r.text
    r = api_post(client, f"/api/imports/{iid}/file", csrf, body or {})
    assert r.status_code == 200, r.text
    return r.json()


def basic(username: str, secret: str) -> dict:
    return {"authorization": "Basic " + base64.b64encode(f"{username}:{secret}".encode()).decode()}


def works(client) -> dict[str, dict]:
    return {w["title"]: w for w in client.get("/api/library").json()["works"]}


# --- parsing and safety ---------------------------------------------------------------------


def test_pdf_metadata_cover_and_page_count(tmp_path):
    p = formats.probe(make_pdf(tmp_path / "a.pdf", pages=4), PDF)
    assert p.meta.title == "The Atlas of Small Rivers"
    assert p.meta.authors == ["Ada Lovelace", "Grace Hopper"]
    assert p.pages == 4
    # The cover is the first page (blue), not a later (white) one.
    img = Image.open(io.BytesIO(p.cover)).convert("RGB")
    r, g, b = img.getpixel((img.width // 2, img.height // 2))
    assert b > 150 and r < 60


def test_pdf_xmp_fills_what_info_lacks(tmp_path):
    path = make_pdf(tmp_path / "x.pdf", title=None, author=None)
    w = PdfWriter(clone_from=str(path))
    xmp = b"""<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title><rdf:Alt><rdf:li xml:lang="x-default">Tides of Glass</rdf:li></rdf:Alt></dc:title>
<dc:creator><rdf:Seq><rdf:li>Mary Shelley</rdf:li><rdf:li>Percy Shelley</rdf:li></rdf:Seq></dc:creator>
<dc:language><rdf:Bag><rdf:li>fr</rdf:li></rdf:Bag></dc:language>
</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>"""
    w.xmp_metadata = xmp
    w.write(path)
    meta = formats.probe(path, PDF).meta
    assert (meta.title, meta.authors, meta.language) == (
        "Tides of Glass",
        ["Mary Shelley", "Percy Shelley"],
        "fr",
    )


def test_pdf_with_javascript_is_read_without_running_it(tmp_path):
    # The parser child never initialises PDFium's form environment, the only place document
    # JavaScript would run; the file is still an ordinary, readable PDF.
    p = formats.probe(make_pdf(tmp_path / "js.pdf", javascript=True), PDF)
    assert p.pages == 3 and p.meta.title == "The Atlas of Small Rivers"


@pytest.mark.parametrize(
    ("build", "media_type", "code"),
    [
        (lambda d: make_pdf(d / "e.pdf", password="secret"), PDF, "pdf_encrypted"),
        (lambda d: make_cbz(d / "c.cbz"), PDF, "not_pdf"),  # a zip renamed .pdf
        (lambda d: make_pdf(d / "p.pdf"), CBZ, "not_comic"),  # a PDF renamed .cbz
        (lambda d: (d / "t.pdf").write_bytes(b"%PDF-1.7\nnothing else") and d / "t.pdf", PDF, "not_pdf"),
        (lambda d: make_epub(d / "b.epub"), CBZ, "not_comic"),  # a zip, but no page images
    ],
)  # fmt: skip
def test_wrong_or_unreadable_files_are_refused(tmp_path, build, media_type, code):
    with pytest.raises(formats.FormatError) as exc:
        formats.probe(build(tmp_path), media_type)
    assert exc.value.code == code


def test_comic_zip_bomb_and_oversized_entries_are_refused_before_reading(tmp_path):
    bomb = tmp_path / "bomb.cbz"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("1.png", png())
        z.writestr("2.png", b"\0" * (8 * 1024 * 1024))  # 8 MiB that deflates to ~8 KiB
    with pytest.raises(formats.FormatError) as exc:
        open_comic(bomb)
    assert exc.value.code == "archive_too_large"

    big = tmp_path / "big.cbz"
    with zipfile.ZipFile(big, "w", zipfile.ZIP_STORED) as z:
        z.writestr("1.png", png())
        z.writestr("2.png", b"\x89PNG" + b"x" * (MAX_ENTRY_BYTES + 1))
    with pytest.raises(formats.FormatError) as exc:
        open_comic(big)
    assert exc.value.code == "archive_too_large"


def test_comicinfo_metadata_and_natural_page_order(tmp_path):
    path = make_cbz(tmp_path / "s.cbz", pages=12, comicinfo=SAGA_INFO)
    comic = open_comic(path)
    assert comic.pages == [f"pages/{n}.png" for n in range(1, 13)]  # 2 before 10, junk skipped
    assert comic.rtl is True
    p = formats.probe(path, CBZ)
    m = p.meta
    assert (m.title, m.series, m.series_index) == ("Chapter Three", "Saga", 3.0)
    assert (m.authors, m.publisher, m.published) == (["Brian K. Vaughan"], "Image", "2012-05")
    assert (m.tags, m.language, m.description) == (["Science Fiction", "Fantasy"], "en",
                                                   "Hazel's parents run.")  # fmt: skip
    assert p.pages == 12 and p.cover == png((0, 160, 60))


def test_comic_without_comicinfo_is_named_after_its_file(tmp_path):
    p = formats.probe(make_cbz(tmp_path / "Bone 01.cbz"), CBZ)
    assert (p.meta.title, p.meta.authors, p.pages) == ("Bone 01", [], 3)


# --- upload, review, filing ---------------------------------------------------------------------


def test_upload_refuses_mismatched_magic_bytes(client, owner, tmp_path):
    csrf = owner["csrf"]
    fake = make_cbz(tmp_path / "fake.pdf")
    r = upload(client, csrf, fake, PDF, new_import(client, csrf))
    assert (r.status_code, r.json()["detail"]) == (422, "not_pdf")
    fake = make_pdf(tmp_path / "fake.cbz")
    r = upload(client, csrf, fake, CBZ)  # plain inbox upload too
    assert (r.status_code, r.json()["detail"]) == (422, "not_comic")
    r = upload(client, csrf, make_pdf(tmp_path / "locked.pdf", password="x"), PDF)
    assert (r.status_code, r.json()["detail"]) == (422, "pdf_encrypted")
    assert not list((tmp_path / "library").rglob("*.pdf"))


def test_pdf_joins_the_epub_of_the_same_book(client, owner, settings, tmp_path):
    csrf = owner["csrf"]
    make_epub(settings.inbox_path / "a.epub", title="The Atlas of Small Rivers",
              authors=["Ada Lovelace"])  # fmt: skip
    drain_inbox(settings)
    epub_work = works(client)["The Atlas of Small Rivers"]

    pdf = make_pdf(tmp_path / "atlas.pdf", author="Ada Lovelace")
    iid = new_import(client, csrf)
    r = upload(client, csrf, pdf, PDF, iid)
    assert r.status_code == 200, r.text
    imp = client.get(f"/api/imports/{iid}").json()
    assert imp["files"][0]["kind"] == "ebook" and imp["files"][0]["pages"] == 3
    out = file_import(client, csrf, iid)
    assert out["work_id"] == epub_work["id"]  # matched, not a second work

    w = client.get(f"/api/works/{epub_work['id']}").json()
    by_type = {f["media_type"]: f for f in w["files"]}
    assert set(by_type) == {"application/epub+zip", PDF}
    assert by_type[PDF]["rel_path"] == "book.pdf"
    assert by_type["application/epub+zip"]["rel_path"] == "book.epub"
    # KOReader's document id is computed at ingest from the exact bytes.
    assert by_type[PDF]["partial_md5"] == partial_md5(pdf)


def test_new_comic_gets_metadata_and_first_page_cover(client, owner, settings, tmp_path):
    csrf = owner["csrf"]
    iid = new_import(client, csrf)
    r = upload(client, csrf, make_cbz(tmp_path / "saga3.cbz", comicinfo=SAGA_INFO), CBZ, iid)
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Chapter Three"
    out = file_import(client, csrf, iid)
    det = client.get(f"/api/imports/{iid}").json()["detected"]
    assert (det["series"], det["series_index"], det["publisher"]) == ("Saga", 3.0, "Image")
    wid = out["work_id"]
    w = client.get(f"/api/works/{wid}").json()
    assert w["has_cover"] and w["files"][0]["rel_path"] == "book.cbz"
    cover = client.get(w["cover_url"])
    r_, g, b = Image.open(io.BytesIO(cover.content)).convert("RGB").getpixel((5, 5))
    assert g > 120 and r_ < 40  # the green first page


def test_inbox_takes_pdf_and_cbz_and_rejects_impostors(client, owner, settings, tmp_path):
    make_pdf(settings.inbox_path / "rivers.pdf")
    make_cbz(settings.inbox_path / "saga.cbz", comicinfo=SAGA_INFO)
    (settings.inbox_path / "bad.cbz").write_bytes(make_pdf(tmp_path / "x.pdf").read_bytes())
    drain_inbox(settings)
    w = works(client)
    assert {"The Atlas of Small Rivers", "Chapter Three"} <= set(w)
    assert w["Chapter Three"]["series"] == "Saga" and w["Chapter Three"]["has_cover"]
    assert w["The Atlas of Small Rivers"]["author"] == "Ada Lovelace"
    assert (settings.inbox_path / "_rejected" / "bad.cbz").is_file()


# --- editing, shelves, trash, bulk --------------------------------------------------------------


def _pdf_work(settings, client) -> dict:
    make_pdf(settings.inbox_path / "rivers.pdf")
    drain_inbox(settings)
    return works(client)["The Atlas of Small Rivers"]


def test_edit_shelve_bulk_and_trash_a_pdf_work(client, owner, settings):
    csrf = owner["csrf"]
    w = _pdf_work(settings, client)
    ed = client.get(f"/api/works/{w['id']}/edit").json()
    assert [(f["format"], f["primary"]) for f in ed["files"]] == [("PDF", False)]
    assert ed["cover"]["embedded_available"] is True
    headers = {"origin": ORIGIN, "x-csrf-token": csrf}
    # A PDF can never be the primary EPUB (the reader, sentences, alignment).
    r = client.patch(f"/api/works/{w['id']}", json={"version": ed["version"],
                     "primary_file_id": ed["files"][0]["id"]}, headers=headers)  # fmt: skip
    assert (r.status_code, r.json()["detail"]) == (422, "primary_not_an_epub_of_this_book")
    meta = {**ed["metadata"], "title": "Small Rivers", "tags": ["maps"]}
    r = client.patch(f"/api/works/{w['id']}", json={"version": ed["version"], "metadata": meta},
                     headers=headers)  # fmt: skip
    assert r.status_code == 200, r.text
    after = client.get(f"/api/works/{w['id']}").json()
    assert after["title"] == "Small Rivers" and after["dir_path"].endswith("Small Rivers")
    assert (settings.library_dir / after["dir_path"] / "book.pdf").is_file()

    shelf = api_post(client, "/api/shelves", csrf, {"name": "Maps"}).json()["id"]
    assert put_json(client, f"/api/shelves/{shelf}/items", csrf,
                    {"work_ids": [w["id"]]}).status_code == 200  # fmt: skip
    r = api_post(client, "/api/works/bulk", csrf,
                 {"work_ids": [w["id"]], "action": "add_tags", "tags": ["atlas"]})  # fmt: skip
    assert r.status_code == 200 and r.json()["results"][0]["ok"]
    v = client.get(f"/api/works/{w['id']}").json()["version"]
    assert api_post(client, f"/api/works/{w['id']}/trash", csrf, {"version": v}).status_code == 200
    assert client.get(f"/api/works/{w['id']}").status_code == 404
    assert api_post(client, f"/api/trash/works/{w['id']}/restore", csrf).status_code == 200
    back = client.get(f"/api/works/{w['id']}").json()
    assert set(back["tags"]) == {"maps", "atlas"}
    assert (settings.library_dir / back["dir_path"] / "book.pdf").is_file()


# --- OPDS, device queue, KOReader -------------------------------------------------------------


def test_opds_media_types_queue_and_delivery(client, owner, settings):
    csrf = owner["csrf"]
    make_pdf(settings.inbox_path / "rivers.pdf")
    make_cbz(settings.inbox_path / "saga.cbz", comicinfo=SAGA_INFO)
    drain_inbox(settings)
    w = works(client)
    r = api_post(client, "/api/devices", csrf, {"label": "Kobo"})
    dev = r.json()["device"]["app_password"]["id"]
    opds = basic("owner", r.json()["opds_secret"])
    ids = [w["The Atlas of Small Rivers"]["id"], w["Chapter Three"]["id"]]
    r = api_post(client, f"/api/device-queues/{dev}/items", csrf, {"work_ids": ids})
    assert [x["status"] for x in r.json()["results"]] == ["queued", "queued"]

    feed = etree.fromstring(client.get("/opds/queue", headers=opds).content)
    acq = {
        e.findtext(f"{ATOM}title"): [
            (link.get("type"), link.get("href"))
            for link in e.findall(f"{ATOM}link")
            if link.get("rel") == "http://opds-spec.org/acquisition"
        ]
        for e in feed.findall(f"{ATOM}entry")
    }
    assert [t for t, _ in acq["The Atlas of Small Rivers"]] == [PDF]
    assert [t for t, _ in acq["Chapter Three"]] == [CBZ]

    href = acq["Chapter Three"][0][1]
    d = client.get(href, headers=opds)
    assert d.status_code == 200 and d.headers["content-type"] == CBZ
    assert d.headers["content-disposition"].endswith("Chapter%20Three.cbz")
    feed = etree.fromstring(client.get("/opds/queue", headers=opds).content)
    assert [e.findtext(f"{ATOM}title") for e in feed.findall(f"{ATOM}entry")] == [
        "The Atlas of Small Rivers"
    ]  # the comic was delivered and left the queue


def test_koreader_and_web_share_pdf_page_progress(client, owner, settings):
    csrf = owner["csrf"]
    w = _pdf_work(settings, client)
    f = w["files"][0]
    doc = f["partial_md5"]
    key = api_post(client, "/api/devices", csrf, {"label": "Kindle"}).json()["sync_secret"]
    ko = ko_headers("owner", key)
    # KOReader sends PDF positions as page numbers (integers).
    r = client.put("/syncs/progress", headers=ko, json={"document": doc, "progress": 2,
                   "percentage": 0.66, "device": "Kindle", "device_id": "k1"})  # fmt: skip
    assert r.status_code == 200, r.text
    info = client.get(f"/api/files/{f['id']}/pages").json()
    assert info["format"] == "PDF" and info["url"] == f"/library/files/{f['id']}/view"
    assert (info["progress"]["page"], info["progress"]["device"]) == (2, "Kindle")

    # The web viewer turns to page 3; KOReader's next GET gets page 3.
    r = put_json(client, f"/api/files/{f['id']}/progress", csrf,
                 {"page": 3, "pages": 3, "device": "Firefox"})  # fmt: skip
    assert r.status_code == 200 and r.json()["page"] == 3
    got = client.get(f"/syncs/progress/{doc}", headers=ko).json()
    assert (got["progress"], got["percentage"], got["device"]) == ("3", 1.0, "Firefox")
    # It is in Continue reading, as KOReader-style progress on that file.
    item = client.get("/api/continue").json()["items"][0]
    assert item["id"] == w["id"] and item["progress"]["progress"] == "3"
    with session_factory()() as db:
        assert db.query(ReadingProgress).count() == 1  # one record, shared

    v = client.get(f"/library/files/{f['id']}/view")
    assert v.status_code == 200 and v.headers["content-type"] == PDF
    assert v.content[:5] == b"%PDF-"
    assert (
        client.get(
            f"/library/files/{f['id']}/view", headers={"if-none-match": v.headers["etag"]}
        ).status_code
        == 304
    )


def test_comic_pages_are_served_in_order_and_only_to_people_who_see_the_work(
    client, owner, settings
):
    csrf = owner["csrf"]
    make_cbz(settings.inbox_path / "saga.cbz", pages=11, comicinfo=SAGA_INFO)
    drain_inbox(settings)
    w = works(client)["Chapter Three"]
    fid = w["files"][0]["id"]
    info = client.get(f"/api/files/{fid}/pages").json()
    assert (info["pages"], info["rtl"], info["format"]) == (11, True, "CBZ")
    assert client.get(f"/api/files/{fid}/pages/1").content == png((0, 160, 60))
    p10 = client.get(f"/api/files/{fid}/pages/10")
    assert p10.headers["content-type"] == "image/png" and p10.content == png((200, 200, 200))
    assert client.get(f"/api/files/{fid}/pages/12").status_code == 404
    assert client.get(f"/api/files/{fid}/pages/0").status_code == 404
    assert (
        put_json(client, f"/api/files/{fid}/progress", csrf, {"page": 4, "pages": 11}).status_code
        == 200
    )

    rex = make_user(client, csrf, "rex", "reader")
    login(client, "rex", rex["password"])
    assert client.get(f"/api/files/{fid}/pages").status_code == 404
    assert client.get(f"/api/files/{fid}/pages/1").status_code == 404


def test_read_along_is_unavailable_without_an_epub(client, owner, settings):
    w = _pdf_work(settings, client)
    st = client.get(f"/api/works/{w['id']}/read-aloud").json()
    assert (st["status"], st["reason"], st["readalong"]) == ("unavailable", "needs_epub", False)
    assert st["message"] == "Read-along needs an EPUB edition."
    assert client.get(f"/library/works/{w['id']}/read-aloud.epub").status_code == 409
    assert client.get(f"/library/works/{w['id']}/book.epub").status_code == 404
    with session_factory()() as db:
        assert db.get(Work, w["id"]) is not None
        assert db.query(BookFile).filter_by(work_id=w["id"]).one().media_type == PDF
        from lectern.models import Sentence

        assert db.query(Sentence).filter_by(work_id=w["id"]).count() == 0


# --- metering and concurrency -------------------------------------------------------------------


def test_pdf_viewer_range_requests_count_as_one_download(client, owner, settings):
    """pdf.js reads a PDF in many Range requests; together they are one download, so a cap of 3
    still lets the viewer fetch eight ranges."""
    f = _pdf_work(settings, client)["files"][0]
    settings.download_cap_per_hour = 3
    size = None
    for i in range(8):
        r = client.get(f"/library/files/{f['id']}/view", headers={"range": f"bytes={i}-{i + 9}"})
        assert r.status_code == 206, (i, r.status_code, r.text)
        size = size or r.headers["content-range"].rsplit("/", 1)[1]
        assert r.headers["content-range"] == f"bytes {i}-{i + 9}/{size}" and len(r.content) == 10
    with session_factory()() as db:
        target = f"file:{f['id']}"
        assert db.query(DownloadEvent).filter_by(target=target, blocked=False).count() == 1
        assert db.query(DownloadEvent).filter_by(target=target, blocked=True).count() == 0
        assert db.query(AuditLog).filter_by(action="download", target=target).count() == 1


class _Gate:
    """Stands in for the PDF parser child: records how many run at once."""

    def __init__(self, real, hold: float = 0.0):
        self.real, self.hold, self.now, self.peak, self.calls = real, hold, 0, 0, 0
        self.lock, self.release = threading.Lock(), threading.Event()
        if hold:
            self.release.set()

    def __call__(self, path):
        with self.lock:
            self.now += 1
            self.calls += 1
            self.peak = max(self.peak, self.now)
        try:
            time.sleep(self.hold)
            self.release.wait(10)
            return self.real(path)
        finally:
            with self.lock:
                self.now -= 1


@pytest.mark.parametrize("into_import", [False, True])
def test_concurrent_finalizes_of_one_upload_parse_it_once(
    client, owner, settings, tmp_path, monkeypatch, into_import
):
    csrf = owner["csrf"]
    gate = _Gate(pdf_ingest._probe_pdf, hold=0.2)
    monkeypatch.setattr(pdf_ingest, "_probe_pdf", gate)
    iid = new_import(client, csrf) if into_import else None
    uid, digest = send(client, csrf, make_pdf(tmp_path / "rivers.pdf"), PDF, iid)

    def fin(_):
        return api_post(client, f"/api/uploads/{uid}/finalize", csrf, {"sha256": digest})

    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(fin, range(6)))
    assert [r.status_code for r in results] == [200] * 6, [r.text for r in results]
    assert gate.calls == 1 and gate.peak == 1
    assert {r.json()["status"] for r in results} == {"completed"}
    with session_factory()() as db:
        assert db.query(AuditLog).filter_by(action="upload.complete").count() == 1
    if into_import:
        assert len(client.get(f"/api/imports/{iid}").json()["files"]) == 1
    else:
        assert len(list(settings.inbox_path.glob("*.pdf"))) == 1


def test_pdf_parsers_are_bounded_and_a_busy_finalize_can_be_retried(
    client, owner, settings, tmp_path, monkeypatch
):
    csrf = owner["csrf"]
    path = make_pdf(tmp_path / "rivers.pdf")
    # Six parses at once: never more than MAX_CONCURRENT children.
    gate = _Gate(pdf_ingest._probe_pdf, hold=0.1)
    monkeypatch.setattr(pdf_ingest, "_probe_pdf", gate)
    with ThreadPoolExecutor(6) as pool:
        probes = list(pool.map(lambda _: formats.probe(path, PDF), range(6)))
    assert gate.peak == pdf_ingest.MAX_CONCURRENT == 2 and len(probes) == 6

    # Both slots held: a finalize waits BUSY_WAIT_S, then says 503 pdf_busy and keeps the upload.
    held = _Gate(pdf_ingest._probe_pdf)
    monkeypatch.setattr(pdf_ingest, "_probe_pdf", held)
    monkeypatch.setattr(pdf_ingest, "BUSY_WAIT_S", 0.05)
    iid = new_import(client, csrf)
    plain = send(client, csrf, path, PDF)
    staged = send(client, csrf, path, PDF, iid)
    with ThreadPoolExecutor(2) as pool:
        holders = [pool.submit(formats.probe, path, PDF) for _ in range(2)]
        while held.now < 2:
            time.sleep(0.01)
        with pytest.raises(formats.ProbeBusy):
            formats.probe(path, PDF)
        for uid, digest in (plain, staged):
            r = api_post(client, f"/api/uploads/{uid}/finalize", csrf, {"sha256": digest})
            assert (r.status_code, r.json()["detail"]) == (503, "pdf_busy"), r.text
        held.release.set()
        for h in holders:
            h.result()
    with session_factory()() as db:
        for uid, _ in (plain, staged):
            assert db.query(UploadSession).filter_by(public_id=uid).one().status.value == "active"
    assert client.get(f"/api/imports/{iid}").json()["files"] == []

    # Once a slot is free the same uploads finish.
    for uid, digest in (plain, staged):
        r = api_post(client, f"/api/uploads/{uid}/finalize", csrf, {"sha256": digest})
        assert r.status_code == 200, r.text
    assert len(client.get(f"/api/imports/{iid}").json()["files"]) == 1
    assert len(list(settings.inbox_path.glob("*.pdf"))) == 1
