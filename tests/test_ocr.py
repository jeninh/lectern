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
from pathlib import Path

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
from lectern.models import BookFile, Job, JobStatus, Work
from lectern.ocr import child, engine
from lectern.ocr import jobs as ocr
from lectern.ocr.words import FLAG_BREAK, FLAG_HYPHEN, from_tsv
from lectern.worker.runners import OcrRunner

HAVE_TESSERACT = engine.probe()[0] is not None and "eng" in engine.probe()[1]
if os.environ.get("TESSERACT_REQUIRED") == "1":
    assert HAVE_TESSERACT, "TESSERACT_REQUIRED=1 but Tesseract with English data is missing"
needs_tesseract = pytest.mark.skipif(not HAVE_TESSERACT, reason="tesseract not installed")

FONT = Path(__file__).parents[1] / "src/lectern/fonts/LecternCover-Text.ttf"
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
