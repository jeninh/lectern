"""Text recognition for scanned PDFs (issue #17, docs/ocr.md).

Tesseract is real (CI installs it with English data); everything runs against the real app.
The scanned fixture is drawn here: pages of rendered text saved as images in a PDF, so it has
no text layer, exactly like a scan.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pypdfium2 as pdfium
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, RectangleObject
from sqlalchemy import select
from tests.conftest import api_delete, api_post, drain_inbox

from lectern.align.worker import RemoteWorker
from lectern.db import session_factory
from lectern.ingest.hashing import sha256_file
from lectern.jobs import run_pending
from lectern.models import (
    Alignment,
    AlignmentStatus,
    BookFile,
    Job,
    JobStatus,
    PdfSentence,
    PdfText,
    Sentence,
    Work,
)
from lectern.ocr import child, engine
from lectern.ocr import jobs as ocr
from lectern.ocr.words import FLAG_BREAK, FLAG_HYPHEN, from_tsv
from lectern.pdf2epub.build import CONVERTED_NAME
from lectern.pdftext import extract
from lectern.pdftext.document import build_document
from lectern.pdftext.store import document_rel, work_text
from lectern.pdftext.store import sweep as pdf_sweep
from lectern.worker.runners import OcrRunner

HAVE_TESSERACT = engine.probe()[0] is not None and "eng" in engine.probe()[1]
if os.environ.get("TESSERACT_REQUIRED") == "1":
    assert HAVE_TESSERACT, "TESSERACT_REQUIRED=1 but Tesseract with English data is missing"
needs_tesseract = pytest.mark.skipif(not HAVE_TESSERACT, reason="tesseract not installed")

FONT = Path(__file__).parents[1] / "src/lectern/fonts/LecternCover-Text.ttf"
EXCERPT = Path(__file__).parent / "fixtures" / "magi" / "excerpt.mp3"
PAGES = [
    ["Chapter One", "The lighthouse keeper climbed the stairs every night.",
     "He counted the steps and listened to the sea below."],
    ["The ships came rarely now, and the town had forgotten",
     "the old tower on the rocks. Still he climbed, and still",
     "the lamp turned over the dark water until morning."],
]  # fmt: skip


def page_image(lines: list[str]) -> Image.Image:
    font = ImageFont.truetype(str(FONT), 40)
    im = Image.new("L", (1700, 2200), 255)
    d = ImageDraw.Draw(im)
    for i, line in enumerate(lines):
        d.text((150, 200 + 90 * i), line, font=font, fill=0)
    return im.convert("RGB")


def make_scan(path: Path, title: str = "The Lighthouse Keeper") -> Path:
    images = [page_image(lines) for lines in PAGES]
    raw = io.BytesIO()
    images[0].save(raw, "PDF", save_all=True, append_images=images[1:], resolution=200)
    w = PdfWriter(clone_from=io.BytesIO(raw.getvalue()))
    w.add_metadata({"/Title": title, "/Author": "Ada Lovelace"})
    w.write(path)
    return path


def make_text_pdf(path: Path, title: str = "Born Digital") -> Path:
    """A born-digital PDF: real text in Helvetica on every page."""
    w = PdfWriter()
    for lines in PAGES:
        page = w.add_blank_page(612, 792)
        font = w._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica")}))  # fmt: skip
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        body = "".join(
            f"BT /F1 12 Tf 72 {700 - 20 * i} Td ({t}) Tj ET\n" for i, t in enumerate(lines)
        )
        s = DecodedStreamObject()
        s.set_data(body.encode())
        page[NameObject("/Contents")] = w._add_object(s)
    w.add_metadata({"/Title": title, "/Author": "Grace Hopper"})
    w.write(path)
    return path


def pdf_file(title: str) -> tuple[BookFile, Work]:
    with session_factory()() as db:
        work = db.scalar(select(Work).where(Work.title == title))
        book = db.scalar(select(BookFile).where(BookFile.work_id == work.id))
        db.expunge_all()
        return book, work


def text_of(path: Path) -> str:
    doc = pdfium.PdfDocument(str(path))
    try:
        out = []
        for i in range(len(doc)):
            page = doc[i]
            tp = page.get_textpage()
            out.append(tp.get_text_range())
            tp.close()
            page.close()
        return "\n".join(out)
    finally:
        doc.close()


@pytest.fixture
def scanned(client, owner, settings):
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    make_scan(settings.inbox_path / "keeper.pdf")
    drain_inbox(settings)
    return pdf_file("The Lighthouse Keeper")


# --- detection and languages -------------------------------------------------------------------


def test_scans_are_found_and_text_pdfs_are_not(tmp_path):
    scan = child.scan(make_scan(tmp_path / "scan.pdf"))
    assert (len(scan.pages), scan.pages_with_text, scan.needs_ocr) == (2, 0, True)
    assert scan.pages[0]["w"] == pytest.approx(612, abs=1)
    text = child.scan(make_text_pdf(tmp_path / "text.pdf"))
    assert (text.pages_with_text, text.needs_ocr) == (2, False)


@pytest.mark.parametrize(
    "metadata, code",
    [(None, "eng"), ("", "eng"), ("en-GB", "eng"), ("fr", "fra"), ("fre", "fra"),
     ("de-AT", "deu"), ("zh-Hant-TW", "chi_tra"), ("zh", "chi_sim"), ("sr-Latn", "srp_latn"),
     ("xx", "eng"), ("nld", "nld"), ("osd", "eng")],
)  # fmt: skip
def test_the_work_language_picks_tesseract_data(metadata, code):
    assert engine.language_for(metadata) == code


@pytest.mark.parametrize(
    "spec", ["", "eng+", "en", "eng;rm", "../eng", "eng+eng", "a+b+c+d", "eng\n+fra", " eng"]
)
def test_language_specs_that_could_not_be_tesseract_codes_are_refused(spec):
    with pytest.raises(ValueError):
        engine.parse_spec(spec)


def test_words_keep_reading_order_line_breaks_and_hyphens():
    tsv = "\n".join([
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
        "4\t1\t1\t1\t1\t0\t100\t100\t400\t40\t-1\t",
        "5\t1\t1\t1\t1\t1\t100\t105\t150\t30\t96\tlight-",
        "4\t1\t1\t1\t2\t0\t100\t160\t400\t40\t-1\t",
        "5\t1\t1\t1\t2\t1\t100\t165\t120\t30\t95\thouse",
        "5\t1\t1\t1\t2\t2\t240\t165\t100\t30\t3\t~",  # noise: below the confidence floor
    ])  # fmt: skip
    # 2 px per point, origin flipped: a 1000 px tall page
    words = from_tsv(tsv, lambda x, y: (x / 2, (1000 - y) / 2), 0.5)
    assert [w[0] for w in words] == ["light-", "house"]
    assert words[0][6] == FLAG_HYPHEN | FLAG_BREAK and words[1][6] == FLAG_BREAK
    # the word's box spans its line, in points with y up
    assert words[0][1:5] == [50.0, 430.0, 125.0, 450.0]


@needs_tesseract
def test_boxes_land_on_the_words_on_rotated_and_offset_pages(tmp_path):
    """Recognised boxes are in PDF user space (before /Rotate, crop box offset included), and
    the text layer puts every word where it is on screen."""
    upright = page_image(PAGES[0])
    w = PdfWriter()
    for rot in (0, 90, 180, 270):
        buf = io.BytesIO()
        upright.rotate(rot, expand=True).save(buf, "PDF", resolution=200)
        page = PdfReader(buf).pages[0]
        if rot:
            page.rotate(rot)
        else:
            page.mediabox = page.cropbox = RectangleObject([-50, -30, 562, 762])
        w.add_page(page)
    w.write(tmp_path / "turned.pdf")
    path = tmp_path / "turned.pdf"
    pages = [child.ocr_page(path, i, tesseract="tesseract", language="eng", dpi=200,
                            memory_mb=1536) for i in range(4)]  # fmt: skip
    child.assemble(path, pages, tmp_path / "out.pdf", memory_mb=1536)
    doc = pdfium.PdfDocument(str(tmp_path / "out.pdf"))
    try:
        for i, page in enumerate(pages):
            assert " ".join(w[0] for w in page["words"]).startswith("Chapter One The lighthouse")
            the = page["words"][2]
            tp = doc[i].get_textpage()
            # PDFium reads the invisible text in order, and finds it inside the word's box
            assert tp.get_text_range().startswith("Chapter One\r\nThe lighthouse keeper")
            inside = tp.get_text_bounded(the[1] - 1, the[2] - 1, the[3] + 1, the[4] + 1)
            assert inside.strip() == "The", (i, inside)
            tp.close()
    finally:
        doc.close()


# --- the job lifecycle --------------------------------------------------------------------------


@needs_tesseract
def test_a_scan_is_recognised_into_a_searchable_copy_and_the_original_is_untouched(
    client, owner, settings, scanned
):
    book, work = scanned
    original = settings.library_dir / work.dir_path / book.rel_path
    before = sha256_file(original)
    st = client.get(f"/api/files/{book.id}/ocr").json()
    assert (st["status"], st["language"]) == ("checking", "eng")

    assert ocr.sweep(session_factory(), settings) == 1
    assert ocr.sweep(session_factory(), settings) == 0  # one job, not one per sweep
    st = client.get(f"/api/files/{book.id}/ocr").json()
    assert (st["status"], st["pages"], st["pages_with_text"]) == ("queued", 2, 0)

    assert ocr.OcrRunner(session_factory(), settings).run_once()
    st = client.get(f"/api/files/{book.id}/ocr").json()
    assert st["status"] == "done", st
    assert st["result"]["pages"] == 2 and st["result"]["words"] > 30
    assert sha256_file(original) == before == book.sha256

    searchable = ocr.pdf_path(settings.library_dir, work, book.sha256)
    text = text_of(searchable)
    assert "lighthouse keeper climbed the stairs" in text and "until morning" in text
    assert text_of(original).strip() == ""
    # the searchable copy is a text PDF now: never recognised again
    assert not child.scan(searchable).needs_ocr

    # the words document: the text-layer reader's page shape, ready for the layout step
    doc = json.loads(ocr.words_path(settings.library_dir, work, book.sha256).read_text())
    assert (doc["kind"], doc["pdf_sha256"], doc["language"]) == ("ocr", book.sha256, "eng")
    page = doc["pages"][0]
    assert set(page) >= {"index", "w", "h", "x0", "y0", "rotation", "label", "chars",
                         "map_errors", "words"}  # fmt: skip
    first = page["words"][0]
    assert first[0] == "Chapter" and 0 < first[1] < first[3] < page["w"]
    assert 0 < first[2] < first[4] < page["h"] and first[2] > page["h"] / 2  # near the top
    # ...which the PDF text layout step turns into the canonical text document unchanged
    raw = extract.RawDoc([extract._page(p) for p in doc["pages"]], [tuple(t) for t in doc["toc"]])
    canonical = build_document(raw, book.sha256)
    assert canonical["status"] == "ready"
    section = canonical["sections"][0]
    assert "lighthouse keeper climbed the stairs" in section["text"]
    assert "until morning" in section["text"]
    assert section["words"][0][:3] == [0, 7, 0]  # "Chapter", boxed on the first page
    # ...and the library sweep makes it the scan's text (issue #62)
    pdf_sweep(session_factory(), settings.library_dir)
    st = client.get(f"/api/files/{book.id}/text").json()
    assert (st["status"], st["source"], st["map"]) == ("ready", "ocr", "self"), st
    assert st["ocr"]["language"] == "eng" and st["ocr"]["engine"].startswith("tesseract")
    assert st["sentences"] > 3

    r = client.get(f"/api/files/{book.id}/ocr/pdf")
    assert r.status_code == 200 and r.content == searchable.read_bytes()
    assert "searchable" in r.headers["content-disposition"]

    # deleting it removes the derived files only
    st = api_delete(client, f"/api/files/{book.id}/ocr", owner["csrf"]).json()
    assert st["status"] == "needs_ocr" and not searchable.exists() and original.is_file()
    assert ocr.sweep(session_factory(), settings) == 0  # a deleted copy is not remade unasked
    assert client.get(f"/api/files/{book.id}/ocr/pdf").status_code == 404


def test_born_digital_pdfs_are_never_recognised(client, owner, settings):
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    make_text_pdf(settings.inbox_path / "digital.pdf")
    drain_inbox(settings)
    book, _work = pdf_file("Born Digital")
    assert ocr.sweep(session_factory(), settings) == 0
    assert client.get(f"/api/files/{book.id}/ocr").json()["status"] == "not_needed"
    r = api_post(client, f"/api/files/{book.id}/ocr", owner["csrf"])
    assert (r.status_code, r.json()["detail"]) == (409, "has_text")
    with session_factory()() as db:
        assert db.scalar(select(Job).where(Job.kind == "ocr")) is None


def test_without_an_engine_or_worker_it_says_so_and_queues_nothing(
    client, owner, settings, scanned
):
    settings.tesseract_path = "no-such-tesseract"
    book, _work = scanned
    assert ocr.sweep(session_factory(), settings) == 0
    st = client.get(f"/api/files/{book.id}/ocr").json()
    assert st["status"] == "unavailable" and "Tesseract" in st["message"]
    r = api_post(client, f"/api/files/{book.id}/ocr", owner["csrf"])
    assert (r.status_code, r.json()["detail"]) == (409, "no_engine")
    # a worker that has OCR is enough: the job waits for it
    key = api_post(client, "/api/worker-credentials", owner["csrf"], {"label": "pi"}).json()
    wc = TestClient(client.app, headers={"Authorization": f"Bearer {key['secret']}"})
    assert wc.post("/api/worker/jobs/claim", json={"worker": "w", "capabilities": ["ocr"]})
    assert ocr.sweep(session_factory(), settings) == 1


@needs_tesseract
def test_a_failure_is_visible_and_retryable_and_nothing_stays_running(
    client, owner, settings, scanned
):
    book, _work = scanned
    r = api_post(client, f"/api/files/{book.id}/ocr", owner["csrf"], {"language": "zzz"})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    assert ocr.OcrRunner(session_factory(), settings).run_once()
    st = client.get(f"/api/files/{book.id}/ocr").json()
    assert st["status"] == "failed" and "zzz" in st["error"], st
    assert not ocr.OcrRunner(session_factory(), settings).run_once()  # not retried by itself

    r = api_post(client, f"/api/files/{book.id}/ocr", owner["csrf"], {"language": "eng"})
    assert r.json()["status"] == "queued"
    # the server stops mid-run: the lease is handed back at the next start, not left running
    with session_factory()() as db:
        job = db.scalar(select(Job).where(Job.kind == "ocr", Job.status == JobStatus.QUEUED))
        job.status, job.lease_token_hash, job.attempts = JobStatus.LEASED, "x" * 64, 1
        db.commit()
        assert ocr.recover_local(db) == 1
        db.refresh(job)
        assert job.status == JobStatus.QUEUED and job.error.startswith("the server restarted")
        job.run_after = job.created_at
        db.commit()
    assert ocr.OcrRunner(session_factory(), settings).run_once()
    assert client.get(f"/api/files/{book.id}/ocr").json()["status"] == "done"


def test_members_start_ocr_readers_cannot(client, owner, settings, scanned):
    from tests.conftest import login, make_user

    book, _work = scanned
    make_user(client, owner["csrf"], "rita", "reader")
    r = api_post(client, f"/api/files/{book.id}/ocr", owner["csrf"], {"language": "en;rm -rf"})
    assert (r.status_code, r.json()["detail"]) == (422, "invalid_language")
    client.cookies.clear()
    csrf = login(client, "rita", "rita-correct horse")
    assert client.get(f"/api/files/{book.id}/ocr").status_code == 404  # not shared with her
    assert api_post(client, f"/api/files/{book.id}/ocr", csrf).status_code in (403, 404)


@needs_tesseract
def test_a_worker_recognises_pages_and_the_server_builds_the_pdf(
    client, owner, settings, scanned, app
):
    book, work = scanned
    key = api_post(client, "/api/worker-credentials", owner["csrf"], {"label": "mac"}).json()
    assert api_post(client, f"/api/files/{book.id}/ocr", owner["csrf"]).status_code == 200
    http = TestClient(app, headers={"Authorization": f"Bearer {key['secret']}"})
    worker = RemoteWorker("http://testserver", key["secret"], client=http, runners=[OcrRunner()])
    assert worker.run_once()
    with session_factory()() as db:
        job = db.scalar(select(Job).where(Job.kind == "ocr"))
        assert job.status == JobStatus.SUCCEEDED, job.error
        assert job.leased_by_id is not None and job.result["words"] > 30
    assert "the old tower on the rocks" in text_of(
        ocr.pdf_path(settings.library_dir, work, book.sha256)
    )


@needs_tesseract
def test_a_worker_cannot_publish_pages_the_pdf_does_not_have(client, owner, settings, scanned, app):
    book, _work = scanned
    key = api_post(client, "/api/worker-credentials", owner["csrf"], {"label": "mac"}).json()
    api_post(client, f"/api/files/{book.id}/ocr", owner["csrf"])
    wc = TestClient(app, headers={"Authorization": f"Bearer {key['secret']}"})
    r = wc.post("/api/worker/jobs/claim", json={"worker": "w", "capabilities": ["ocr"]})
    assert r.status_code == 200 and set(r.json()["inputs"]) == {"pdf"}
    job_id, token = r.json()["job"]["id"], {"X-Lease-Token": r.json()["lease"]["token"]}
    bad = json.dumps({"first": 0, "pages": [{"index": 7, "words": []}]}).encode()
    digest = hashlib.sha256(bad).hexdigest()
    up = wc.put(f"/api/worker/jobs/{job_id}/artifacts/pages-00000.json", content=bad,
                headers={**token, "X-Content-SHA256": digest})  # fmt: skip
    assert up.status_code == 201, up.text
    body = {"engine": "tesseract 5", "language": "eng", "batches": [
        {"first": 0, "artifact": "pages-00000.json",
         "artifact_sha256": digest}]}  # fmt: skip
    r = wc.put(f"/api/worker/jobs/{job_id}/result", json=body, headers=token)
    assert r.status_code == 422 and "invalid_pages" in r.text, r.text


def test_orphaned_derived_files_are_removed(client, owner, settings, scanned):
    book, work = scanned
    derived = settings.library_dir / work.dir_path / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    stale = derived / "ocr-0123456789abcdef.pdf"
    stale.write_bytes(b"%PDF-")
    mine = ocr.pdf_path(settings.library_dir, work, book.sha256)
    mine.write_bytes(b"%PDF-")
    settings.ocr_auto = False
    ocr.sweep(session_factory(), settings)
    assert not stale.exists() and mine.exists()


def test_a_merge_takes_the_searchable_copy_along(client, owner, settings, scanned):
    book, work = scanned
    make_text_pdf(settings.inbox_path / "other.pdf", title="Another Edition")
    drain_inbox(settings)
    _other_book, target = pdf_file("Another Edition")
    copy = ocr.pdf_path(settings.library_dir, work, book.sha256)
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(b"%PDF-searchable")
    ocr.words_path(settings.library_dir, work, book.sha256).write_text("{}")
    r = api_post(
        client, f"/api/works/{work.id}/merge", owner["csrf"], {"target_work_id": target.id}
    )
    assert r.status_code == 200, r.text
    with session_factory()() as db:
        target = db.get(Work, target.id)
        assert ocr.has_output(settings.library_dir, target, book.sha256)
        assert ocr.pdf_path(settings.library_dir, target, book.sha256).read_bytes() == (
            b"%PDF-searchable"
        )


def _queue(client, owner, book) -> int:
    r = api_post(client, f"/api/files/{book.id}/ocr", owner["csrf"])
    assert r.status_code == 200 and r.json()["status"] == "queued", r.text
    return r.json()["job"]["id"]


@needs_tesseract
def test_a_pdf_changed_mid_run_fails_the_job_instead_of_leaving_it_leased(
    client, owner, settings, scanned, monkeypatch
):
    book, _work = scanned
    job_id = _queue(client, owner, book)
    real = ocr.recognise_batch

    def then_replace_the_file(*a, **kw):
        out = real(*a, **kw)
        with session_factory()() as db:  # an edit swapped the PDF meanwhile
            db.get(BookFile, book.id).sha256 = "f" * 64
            db.commit()
        return out

    monkeypatch.setattr(ocr, "recognise_batch", then_replace_the_file)
    assert ocr.OcrRunner(session_factory(), settings).run_once()
    with session_factory()() as db:
        job = db.get(Job, job_id)
        assert job.status == JobStatus.FAILED and "changed" in job.error
        assert ocr.active_job(db, job.work_id) is None


@needs_tesseract
def test_a_lease_lost_before_publishing_is_handed_back(
    client, owner, settings, scanned, monkeypatch
):
    book, work = scanned
    job_id = _queue(client, owner, book)
    monkeypatch.setattr(ocr, "_extend_lease", lambda *a: False)
    assert ocr.OcrRunner(session_factory(), settings).run_once()
    with session_factory()() as db:
        job = db.get(Job, job_id)
        assert job.status == JobStatus.QUEUED and "lease" in job.error  # retried, not stuck
    assert not ocr.has_output(settings.library_dir, work, book.sha256)


@needs_tesseract
def test_a_retry_resumes_from_stored_batches(client, owner, settings, scanned, monkeypatch):
    book, work = scanned
    job_id = _queue(client, owner, book)
    store = ocr.queue.artifacts_dir(settings.data_dir, job_id)
    store.mkdir(parents=True)
    done = {"first": 0, "pages": [
        {"index": i, "words": [["Zebra", 100, 600, 160, 620, 16, 2]], "conf": 90, "dpi": 300}
        for i in range(2)]}  # fmt: skip
    (store / ocr.batch_name(0)).write_text(json.dumps(done))

    def never(*a, **kw):
        raise AssertionError("a stored batch was recognised again")

    monkeypatch.setattr(ocr, "recognise_batch", never)
    monkeypatch.setattr(ocr, "local_engine", lambda _s: ("5.3.0", ("eng",)))
    assert ocr.OcrRunner(session_factory(), settings).run_once()
    words = json.loads(ocr.words_path(settings.library_dir, work, book.sha256).read_text())
    assert [p["words"][0][0] for p in words["pages"]] == ["Zebra", "Zebra"]
    assert "Zebra" in text_of(ocr.pdf_path(settings.library_dir, work, book.sha256))


def test_books_past_the_word_cap_are_truncated(client, owner, settings, scanned, monkeypatch):
    book, work = scanned
    src = ocr.Source(book, work, Path("unused"), ocr.load_scan(settings.data_dir, book.sha256)
                     or ocr.scan_file(settings, book, work))  # fmt: skip
    path = settings.data_dir / "batch.json"
    word = ["word", 100, 600, 160, 620, 16, 0]
    path.write_text(json.dumps({"first": 0, "pages": [
        {"index": i, "words": [word] * 3} for i in range(2)]}))  # fmt: skip
    monkeypatch.setattr(ocr, "MAX_WORDS", 4)
    pages, truncated = ocr.read_batches(src, {0: path})
    assert truncated and [len(p["words"]) for p in pages] == [3, 0]
    monkeypatch.setattr(ocr, "BATCH_MAX_BYTES", 64)
    with pytest.raises(ValueError, match="larger than"):
        ocr.read_batches(src, {0: path})


@needs_tesseract
def test_a_worker_cannot_make_the_server_parse_a_huge_batch(
    client, owner, settings, scanned, app, monkeypatch
):
    book, _work = scanned
    monkeypatch.setattr(ocr, "BATCH_MAX_BYTES", 256)
    key = api_post(client, "/api/worker-credentials", owner["csrf"], {"label": "mac"}).json()
    _queue(client, owner, book)
    wc = TestClient(app, headers={"Authorization": f"Bearer {key['secret']}"})
    r = wc.post("/api/worker/jobs/claim", json={"worker": "w", "capabilities": ["ocr"]})
    job_id, token = r.json()["job"]["id"], {"X-Lease-Token": r.json()["lease"]["token"]}
    big = json.dumps({"first": 0, "pages": [{"index": 0, "words": []}], "pad": "x" * 1000}).encode()
    digest = hashlib.sha256(big).hexdigest()
    up = wc.put(f"/api/worker/jobs/{job_id}/artifacts/pages-00000.json", content=big,
                headers={**token, "X-Content-SHA256": digest})  # fmt: skip
    assert up.status_code == 201, up.text
    body = {"engine": "tesseract 5", "language": "eng", "batches": [
        {"first": 0, "artifact": "pages-00000.json", "artifact_sha256": digest}]}  # fmt: skip
    r = wc.put(f"/api/worker/jobs/{job_id}/result", json=body, headers=token)
    assert r.status_code == 422 and "too large" in r.text, r.text


def test_a_page_timeout_kills_a_running_tesseract_too(tmp_path):
    """The page child runs in its own process group: on a timeout the engine it started is
    killed with it (a stand-in that never finishes plays a stuck Tesseract)."""
    import threading
    import time

    path = make_scan(tmp_path / "scan.pdf")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stuck = bindir / "tesseract"
    stuck.write_text("#!/bin/sh\nexec sleep 120\n")
    stuck.chmod(0o755)
    seen: list[int] = []
    stop = threading.Event()

    def sleepers() -> list[int]:
        out = []
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                cmd = Path(f"/proc/{d}/cmdline").read_bytes().split(b"\0")
                state = Path(f"/proc/{d}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except OSError:
                continue
            if cmd[:2] == [b"sleep", b"120"] and state != "Z":
                out.append(int(d))
        return out

    def watch() -> None:
        while not stop.is_set():
            seen.extend(p for p in sleepers() if p not in seen)
            time.sleep(0.05)

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    try:
        with pytest.raises(child.OcrError) as err:
            child._run(["page", str(path), "0", str(tmp_path / "o.json"), str(stuck), "eng",
                        "150", "1536"], str(tmp_path), 8)  # fmt: skip
    finally:
        stop.set()
        t.join()
    assert err.value.code == "timed_out"
    assert seen, "the stand-in engine never started"
    deadline = time.monotonic() + 10  # the killed grandchild is reaped by init, not by us
    while set(seen) & set(sleepers()) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not set(seen) & set(sleepers()), "the engine outlived its page's timeout"


def test_the_caps_apply_while_pages_are_read(client, owner, settings, scanned, monkeypatch):
    """Pages past the book-wide cap are emptied as they are read, so a worker sending many
    batches never makes the server hold more than the cap."""
    book, work = scanned
    src = ocr.Source(book, work, Path("unused"), ocr.load_scan(settings.data_dir, book.sha256)
                     or ocr.scan_file(settings, book, work))  # fmt: skip
    src.scan = {**src.scan, "pages": [src.scan["pages"][0]] * 4}
    word = ["word", 100, 600, 160, 620, 16, 0]
    files = {}
    for first in (0, 2):
        files[first] = settings.data_dir / f"b{first}.json"
        files[first].write_text(json.dumps({"first": first, "pages": [
            {"index": i, "words": [word] * 3} for i in (first, first + 1)]}))  # fmt: skip
    kept: list[dict] = []
    held: list[int] = []
    real = ocr.validated_page

    def spy(raw, geometry):
        held.append(sum(len(p["words"]) for p in kept))  # what is held when the next is read
        page = real(raw, geometry)
        kept.append(page)
        return page

    monkeypatch.setattr(ocr, "validated_page", spy)
    monkeypatch.setattr(ocr, "MAX_WORDS", 4)
    pages, truncated = ocr.read_batches(src, files)
    assert truncated and [len(p["words"]) for p in pages] == [3, 0, 0, 0]
    assert max(held) <= 4


@needs_tesseract
def test_a_worker_without_the_language_fails_retryably_and_never_bounces(
    client, owner, settings, scanned, app, monkeypatch
):
    from lectern.ocr import engine as eng

    book, _work = scanned
    key = api_post(client, "/api/worker-credentials", owner["csrf"], {"label": "mac"}).json()
    api_post(client, f"/api/files/{book.id}/ocr", owner["csrf"], {"language": "eng"})
    real = eng.probe
    monkeypatch.setattr(eng, "probe", lambda *a: (real(*a)[0], ("fra",)))
    http = TestClient(app, headers={"Authorization": f"Bearer {key['secret']}"})
    worker = RemoteWorker("http://testserver", key["secret"], client=http, runners=[OcrRunner()])
    assert worker.run_once()
    with session_factory()() as db:
        job = db.scalar(select(Job).where(Job.kind == "ocr"))
        # an attempt is spent and the reason recorded: with no one able to run it, the job
        # ends failed after max_attempts instead of being handed back forever
        assert job.status == JobStatus.QUEUED and job.attempts == 1
        assert "no data for eng" in job.error
    assert "ocr" in worker.capabilities  # the worker keeps offering OCR for other jobs


# --- the recognised text is the scan's text (issue #62) -----------------------------------------
#
# These write what a finished OCR job leaves (docs/ocr.md, "What is kept") without Tesseract:
# the words of the same pages typeset as a text PDF, read by the text-layer reader, in the
# words file's shape. The end-to-end test above checks the real recognition does the same.


def fake_ocr(settings, book: BookFile, work: Work, language: str = "eng") -> str:
    """Publish an OCR words file for ``book`` (and its checksum sidecar); returns its digest."""
    typeset = make_text_pdf(settings.data_dir / f"typeset-{language}.pdf")
    pages = [vars(p) for p in extract.extract_words(typeset).pages]
    src = SimpleNamespace(book=book, scan={"toc": []})
    doc = ocr.build_document(src, pages, engine_label="tesseract 5.3.0", language=language,
                             dpi=300)  # fmt: skip
    data = ocr.dumps(doc)
    dest = ocr.words_path(settings.library_dir, work, book.sha256)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    dest.with_name(dest.name + ".sha256").write_text(digest)
    return digest


def _text(client, book) -> dict:
    return client.get(f"/api/files/{book.id}/text").json()


def test_recognised_words_become_the_scans_ready_text_and_read_along_runs_on_it(
    client, owner, settings, scanned
):
    book, work = scanned
    assert pdf_sweep(session_factory(), settings.library_dir) == 1
    assert _text(client, book)["status"] == "needs_ocr"
    assert pdf_sweep(session_factory(), settings.library_dir) == 0  # nothing new: not read again

    digest = fake_ocr(settings, book, work)
    assert pdf_sweep(session_factory(), settings.library_dir) == 1  # recognised: read again
    assert pdf_sweep(session_factory(), settings.library_dir) == 0
    st = _text(client, book)
    assert (st["status"], st["source"], st["ocr"]) == (
        "ready", "ocr", {"engine": "tesseract 5.3.0", "language": "eng"})  # fmt: skip
    assert (st["text_source"], st["map"], st["pages"], st["pages_with_text"]) == (
        "pdf",
        "self",
        2,
        2,
    )
    assert st["sections"] and st["sentences"] > 3

    doc_path = settings.library_dir / work.dir_path / document_rel(book.sha256)
    doc = json.loads(doc_path.read_bytes())
    assert doc["ocr"] == {"engine": "tesseract 5.3.0", "language": "eng"}
    text = " ".join(s["text"] for s in doc["sections"])
    assert "lighthouse keeper climbed the stairs every night" in text
    assert "the old tower on the rocks" in text
    # every word has a box on its page: the overlay highlights recognised text
    assert {w[2] for s in doc["sections"] for w in s["words"]} == {0, 1}
    with session_factory()() as db:
        row = db.scalar(select(PdfText).where(PdfText.file_id == book.id))
        assert row.ocr_sha256 == digest and row.text_sha256 == hashlib.sha256(
            doc_path.read_bytes()).hexdigest()  # fmt: skip
        n_work = db.query(Sentence).filter_by(work_id=work.id).count()
        assert n_work == db.query(PdfSentence).filter_by(file_id=book.id).count() == st["sentences"]
    r = client.get(f"/api/files/{book.id}/sentences?from=0&to=3").json()
    assert r["map"] == "self" and [s["work_n"] for s in r["sentences"]] == [0, 1, 2]
    assert all(s["boxes"] for s in r["sentences"])

    # Alignment reads the recognised text, and narration has a text to narrate.
    shutil.copy(EXCERPT, settings.library_dir / work.dir_path / "01 reading.mp3")
    r = api_post(client, f"/api/works/{work.id}/align", owner["csrf"], {})
    assert r.status_code == 202, r.text
    assert r.json()["capability"] == "align-pdf"
    with session_factory()() as db:
        job = db.scalar(select(Job).where(Job.kind == "align"))
        assert job.payload["epub_sha256"] == row.text_sha256
    assert client.get(f"/api/works/{work.id}/audiobook").json()["reason"] != "pdf_needs_ocr"


def test_a_sparse_scan_is_ready_from_what_ocr_recognised():
    """A picture book: text on one page in three is not a text PDF by the text-layer rule,
    but it is a recognised text."""
    words = [["Once", 72.0, 700.0, 110.0, 712.0, 12.0, 0], ["upon", 114.0, 700.0, 150.0, 712.0,
             12.0, 0], ["a", 154.0, 700.0, 160.0, 712.0, 12.0, 0], ["time.", 164.0, 700.0, 200.0,
             712.0, 12.0, 2]]  # fmt: skip
    pages = [extract.RawPage(i, 612.0, 792.0, 0.0, 0.0, 0, None, 0, 0, words if i == 1 else [])
             for i in range(3)]  # fmt: skip
    raw = extract.RawDoc(pages, [])
    assert build_document(raw, "0" * 64)["status"] == "needs_ocr"
    doc = build_document(raw, "0" * 64, ocr={"engine": "tesseract 5", "language": "eng"})
    assert doc["status"] == "ready" and doc["sections"][0]["text"] == "Once upon a time."
    assert doc["sections"][0]["words"][0][2] == 1  # boxed on the second page
    # nothing recognised at all is still no text
    empty = extract.RawDoc([extract.RawPage(0, 612.0, 792.0, 0.0, 0.0, 0, None, 0, 0, [])], [])
    assert build_document(empty, "0" * 64, ocr={"engine": "t", "language": "eng"})["status"] == (
        "needs_ocr"
    )


def test_recognising_again_in_another_language_makes_older_alignments_stale(
    client, owner, settings, scanned
):
    from lectern.align.jobs import DEFAULT_MODEL, align_payload, refresh_work

    book, work = scanned
    shutil.copy(EXCERPT, settings.library_dir / work.dir_path / "01 reading.mp3")
    fake_ocr(settings, book, work, "eng")
    pdf_sweep(session_factory(), settings.library_dir)
    with session_factory()() as db:
        w = db.get(Work, work.id)
        refresh_work(db, settings.library_dir, w, auto_enqueue=False)
        payload = align_payload(db, settings.library_dir, w, DEFAULT_MODEL)
        first = db.scalar(select(PdfText).where(PdfText.file_id == book.id)).text_sha256
        assert payload["epub_sha256"] == first and payload["text_source"] == "pdf"
        db.add(Alignment(work_id=w.id, model="m", status=AlignmentStatus.SUCCEEDED,
                         epub_sha256=first, audio_fingerprint=payload["audio_fingerprint"]))  # fmt: skip
        db.commit()
        assert refresh_work(db, settings.library_dir, w) is None  # current: nothing to do
        db.commit()

    fake_ocr(settings, book, work, "fra")  # the same words, recognised as French
    assert pdf_sweep(session_factory(), settings.library_dir) == 1
    st = _text(client, book)
    assert st["status"] == "ready" and st["ocr"]["language"] == "fra"
    with session_factory()() as db:
        w = db.get(Work, work.id)
        second = db.scalar(select(PdfText).where(PdfText.file_id == book.id)).text_sha256
        assert second != first  # a new recognition is a new text
        job = refresh_work(db, settings.library_dir, w)
        db.commit()
        a = db.scalar(select(Alignment).where(Alignment.work_id == w.id))
        assert a.status == AlignmentStatus.STALE
        assert job is not None and job.payload["epub_sha256"] == second


def test_deleting_the_copy_takes_the_recognised_text_back_out(client, owner, settings, scanned):
    book, work = scanned
    fake_ocr(settings, book, work)
    ocr.pdf_path(settings.library_dir, work, book.sha256).write_bytes(b"%PDF-")  # the copy
    pdf_sweep(session_factory(), settings.library_dir)
    assert _text(client, book)["status"] == "ready"
    assert api_delete(client, f"/api/files/{book.id}/ocr", owner["csrf"]).status_code == 200
    assert pdf_sweep(session_factory(), settings.library_dir) == 1
    st = _text(client, book)
    assert (st["status"], st["source"], st["sentences"]) == ("needs_ocr", "text_layer", 0)
    with session_factory()() as db:
        assert db.query(PdfSentence).filter_by(file_id=book.id).count() == 0


def test_unusable_words_fail_that_pdf_and_stop_retrying(client, owner, settings, scanned):
    book, work = scanned
    dest = ocr.words_path(settings.library_dir, work, book.sha256)
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"kind": "ocr", "pdf_sha256": "f" * 64, "pages": []}).encode()
    dest.write_bytes(data)  # no sidecar: hashed instead
    for _ in range(3):
        assert pdf_sweep(session_factory(), settings.library_dir) == 1
    assert pdf_sweep(session_factory(), settings.library_dir) == 0  # MAX_ATTEMPTS, then left
    st = _text(client, book)
    assert st["status"] == "failed" and st["error"].startswith("ocr_unreadable")
    # a new recognition starts over
    fake_ocr(settings, book, work)
    assert pdf_sweep(session_factory(), settings.library_dir) == 1
    assert _text(client, book)["status"] == "ready"


# --- the reflowed EPUB of a scan (issue #62, phase 2 of #17) ------------------------------------


def _opf(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read("OEBPS/content.opf").decode()


def test_an_ocrd_scan_converts_to_an_epub_labelled_as_ocr_that_takes_part_in_read_along(
    client, owner, settings, scanned
):
    book, work = scanned
    pdf_sweep(session_factory(), settings.library_dir)
    st = client.get(f"/api/works/{work.id}/pdf-epub").json()
    assert (st["eligible"], st["reason"]) == (False, "no_text_pdf")  # a scan: not yet

    fake_ocr(settings, book, work)
    pdf_sweep(session_factory(), settings.library_dir)
    st = client.get(f"/api/works/{work.id}/pdf-epub").json()
    assert (st["eligible"], st["source"], st["outdated"]) == (True, "ocr", False)
    assert api_post(client, f"/api/works/{work.id}/pdf-epub", owner["csrf"]).status_code == 202
    assert run_pending(session_factory(), settings) == 1

    st = client.get(f"/api/works/{work.id}/pdf-epub").json()
    assert st["job"]["status"] == "succeeded", st["job"]
    assert (st["reason"], st["outdated"], st["report"]["source"]) == ("converted", False, "ocr")
    assert st["report"]["ocr"] == {"engine": "tesseract 5.3.0", "language": "eng"}
    assert any("OCR" in w and "recognition errors" in w for w in st["report"]["warnings"])
    path = settings.library_dir / work.dir_path / CONVERTED_NAME
    opf = _opf(path)
    assert '<meta name="lectern:source" content="ocr"/>' in opf
    assert "text recognition (OCR: tesseract 5.3.0, language eng)" in opf
    with zipfile.ZipFile(path) as z:
        assert "recognition errors" in z.read("OEBPS/nav.xhtml").decode()
        body = " ".join(z.read(n).decode() for n in z.namelist() if re.match(r"OEBPS/s\d+", n))
    assert "lighthouse keeper climbed the stairs every night" in body

    # The edition is the work's text now (sentences, alignment, read-along), and the scan's
    # pages follow it through the PDF↔EPUB projection.
    with session_factory()() as db:
        w = db.get(Work, work.id)
        found = work_text(db, settings.library_dir, w)
        assert (
            found is not None
            and found[0].kind == "epub"
            and found[0].file.rel_path == (CONVERTED_NAME)
        )
        assert db.query(Sentence).filter_by(work_id=work.id).count() > 3
    pdf_sweep(session_factory(), settings.library_dir)
    st = _text(client, book)
    assert (st["text_source"], st["map"]) == ("epub", "epub")
    shutil.copy(EXCERPT, settings.library_dir / work.dir_path / "01 reading.mp3")
    r = api_post(client, f"/api/works/{work.id}/align", owner["csrf"], {})
    assert r.status_code == 202 and r.json()["capability"] == "align", r.text

    # Recognised again: the edition is out of date, and converting again replaces it.
    old = sha256_file(path)
    fake_ocr(settings, book, work, "fra")
    pdf_sweep(session_factory(), settings.library_dir)
    st = client.get(f"/api/works/{work.id}/pdf-epub").json()
    assert (st["eligible"], st["outdated"]) == (True, True)
    assert api_post(client, f"/api/works/{work.id}/pdf-epub", owner["csrf"]).status_code == 202
    assert run_pending(session_factory(), settings) >= 1
    st = client.get(f"/api/works/{work.id}/pdf-epub").json()
    assert (st["reason"], st["outdated"]) == ("converted", False)
    assert st["report"]["ocr"]["language"] == "fra" and sha256_file(path) != old
    with session_factory()() as db:
        assert db.query(BookFile).filter_by(work_id=work.id, rel_path=CONVERTED_NAME).count() == 1

    # ...and it can be deleted like any converted edition (the scan and its OCR stay).
    assert api_delete(client, f"/api/works/{work.id}/pdf-epub", owner["csrf"]).status_code == 200
    assert not path.exists() and ocr.words_path(settings.library_dir, work, book.sha256).is_file()
