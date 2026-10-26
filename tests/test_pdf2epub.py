"""PDF → EPUB conversion (issue #28): a reflowable EPUB 3 edition of a PDF work, with the
PDF's pagination kept as metadata.

Expected values come from the fixture source (tests/fixtures/pdf/expected.json, written by
make_fixtures.py from the same strings it typesets) or from EPUBCheck; never from the code
under test.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.conftest import api_delete, api_post, drain_inbox

from lectern.db import session_factory
from lectern.ingest.hashing import sha256_file
from lectern.jobs import run_pending
from lectern.models import BookFile, Job, JobStatus, Sentence, Work
from lectern.pdf2epub.build import (
    CONVERTED_NAME,
    ConversionError,
    build_epub,
    page_labels,
)
from lectern.pdftext import extract
from lectern.pdftext.document import build_document

FIX = Path(__file__).parent / "fixtures" / "pdf"
EXPECTED = json.loads((FIX / "expected.json").read_text())


def read(name: str) -> dict:
    return build_document(extract.extract_words(FIX / name), "0" * 64)


def convert(name: str, **kw) -> tuple[bytes, dict]:
    doc = read(name)
    return build_epub(
        doc,
        title=EXPECTED["title"],
        author="Ada Marlow",
        language="en",
        source_name=name,
        pdf_sha256=doc["pdf_sha256"],
        **kw,
    )


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


def spine_text(epub: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(epub)) as zf:  # noqa: SIM117 - one expression, one open
        return "\n".join(
            zf.read(n).decode()
            for n in zf.namelist()
            if n.startswith("OEBPS/") and n.endswith(".xhtml")
        )


# --- the builder on its own ---------------------------------------------------------------------


def test_page_labels_roman_numerals_and_offsets():
    doc = read("labels.pdf")
    assert page_labels(doc) == ["i", "ii", "5"]
    epub, report = convert("labels.pdf")
    with zipfile.ZipFile(io.BytesIO(epub)) as zf:
        nav = zf.read("OEBPS/nav.xhtml").decode()
    assert '<a href="s1.xhtml#page-1">i</a>' in nav
    assert '<a href="s1.xhtml#page-3">5</a></li>' in nav  # /St 5: the printed offset, not "3"
    assert report["labels_source"] == "page_labels"


def test_page_numbers_are_metadata_not_text():
    epub, _report = convert("plain.pdf")
    text = spine_text(epub)
    assert "Field Notes ·" not in text  # the printed footer stays out of the flow
    assert 'epub:type="pagebreak"' in text and 'epub:type="page-list"' in text
    with zipfile.ZipFile(io.BytesIO(epub)) as zf:
        opf = zf.read("OEBPS/content.opf").decode()
    assert '<meta property="pageBreakSource">urn:sha256:' in opf
    assert "<dc:source>plain.pdf</dc:source>" in opf


def test_a_paragraph_across_a_page_break_stays_one_paragraph():
    epub, _report = convert("plain.pdf")
    with zipfile.ZipFile(io.BytesIO(epub)) as zf:
        s = zf.read("OEBPS/s1.xhtml").decode()
    paras = re.findall(r"<p>(.*?)</p>", s, re.S)
    crossed = [p for p in paras if "pagebreak" in p]
    assert crossed, "no paragraph crosses a page break"
    # A marker may open a paragraph (the page broke between paragraphs), but a paragraph
    # that ran over the break must still be whole: text on both sides of its marker.
    whole = [p for p in crossed if re.split(r"<span[^>]*/>", p, maxsplit=1)[0].split()]
    assert whole and all(p.split() for p in crossed), "a pagebreak marker swallowed a paragraph"


def test_chapters_headings_and_the_first_page_of_every_page():
    epub, report = convert("two-column.pdf")
    chapters = EXPECTED["chapters"]
    # s1 is the title block (a section of its own), s2/s3 are the outline's chapters.
    with zipfile.ZipFile(io.BytesIO(epub)) as zf:
        names = [n for n in zf.namelist() if re.match(r"OEBPS/s\d+\.xhtml$", n)]
        assert len(names) == len(chapters) + 1
        for name, c in zip(names[1:], chapters, strict=True):
            heading, paragraphs = c["title"], c["paragraphs"]
            x = zf.read(name).decode()
            # the section's own first heading block is the title, as h2 (never printed twice)
            assert f">{heading}</h2>" in x
            for p in paragraphs:
                body = " ".join(x.split())
                assert " ".join(p.split()[:6]) in body
        nav = zf.read("OEBPS/nav.xhtml").decode()
    # every page has exactly one marker, and the page list anchors to all of them
    text = spine_text(epub)
    ids = re.findall(r'id="page-(\d+)"', text)
    assert sorted(set(ids)) == ["1", "2", "3"] and len(ids) == 3
    assert nav.count("<li><a href=") == len(names) + 3  # toc (all sections) + page list
    assert report["sections"] == len(names)


def test_the_conversion_is_deterministic_and_the_report_is_honest():
    doc = read("two-column.pdf")
    a, ra = build_epub(doc, title="T", author="A", language="en", source_name="x.pdf",
                       pdf_sha256=doc["pdf_sha256"])  # fmt: skip
    b, rb = build_epub(doc, title="T", author="A", language="en", source_name="x.pdf",
                       pdf_sha256=doc["pdf_sha256"])  # fmt: skip
    assert a == b and ra == rb
    assert ra["pdf_sha256"] == doc["pdf_sha256"]


def test_a_scanned_pdf_is_refused_until_ocr_exists():
    with pytest.raises(ConversionError):
        convert("scanned.pdf")


def test_cover_and_isbn_carry_into_the_edition():
    cover = bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909"
        "080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c31"
        "3434341f27393d38323c2e333432ffc0000b080001000101011100ffc400015f00"
    )  # a truncated JPEG is enough here: the builder only carries the bytes
    epub, _report = convert("plain.pdf", cover_jpeg=cover, isbn="9780000000002")
    with zipfile.ZipFile(io.BytesIO(epub)) as zf:
        opf = zf.read("OEBPS/content.opf").decode()
        assert 'properties="cover-image"' in opf
        assert zf.read("OEBPS/cover.jpg") == cover
        assert "urn:isbn:9780000000002" in opf


# --- EPUBCheck (CI pins and requires it; see .github/workflows/ci.yml) ---------------------------


@pytest.mark.parametrize("name", ["plain.pdf", "labels.pdf", "two-column.pdf"])
def test_the_converted_edition_is_a_valid_epub(name):
    epub, _report = convert(name)
    out = Path("/tmp") / f"lectern-{name}.epub"
    out.write_bytes(epub)
    epubcheck(out)


# --- the job, the routes, and the library --------------------------------------------------------


def _pdf_work(settings, name: str) -> tuple[int, int]:
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    (settings.inbox_path / name).write_bytes((FIX / name).read_bytes())
    drain_inbox(settings)
    from lectern.pdftext.store import sweep

    assert sweep(session_factory(), settings.library_dir) == 1
    with session_factory()() as db:
        book = db.scalar(
            select(BookFile).where(BookFile.rel_path.like("%.pdf")).order_by(BookFile.id.desc())
        )
        return book.id, book.work_id


def test_convert_publishes_an_edition_and_moves_the_works_text(client, owner, settings):
    _file_id, work_id = _pdf_work(settings, "labels.pdf")
    r = api_post(client, f"/api/works/{work_id}/pdf-epub", owner["csrf"])
    assert r.status_code == 202 and r.json()["status"] == "queued"
    again = api_post(client, f"/api/works/{work_id}/pdf-epub", owner["csrf"])
    assert again.json()["job_id"] == r.json()["job_id"]  # one live job per work

    assert run_pending(session_factory(), settings) == 1

    with session_factory()() as db:
        work = db.get(Work, work_id)
        book = db.scalar(
            select(BookFile).where(BookFile.work_id == work_id, BookFile.rel_path == CONVERTED_NAME)
        )
        assert book is not None and book.media_type == "application/epub+zip"
        path = settings.library_dir / work.dir_path / CONVERTED_NAME
        assert path.is_file() and book.sha256 == sha256_file(path)
        # the work's text is the EPUB now, with its own sentence list (the nav document is
        # not in the spine, so every sentence sits on the one section file)
        kinds = db.scalars(select(Sentence.spine_index).where(Sentence.work_id == work_id)).all()
        assert kinds and set(kinds) == {0}
    st = client.get(f"/api/works/{work_id}/pdf-epub").json()
    assert st["eligible"] is False and st["reason"] == "converted"
    assert st["edition"]["file"] == CONVERTED_NAME and st["job"]["status"] == "succeeded"
    assert st["report"]["labels_source"] == "page_labels"
    report_path = path.parent / "derived" / "pdf2epub-report.json"
    assert report_path.is_file()

    # the original PDF is untouched and still there
    with session_factory()() as db:
        pdfs = db.scalars(
            select(BookFile).where(BookFile.work_id == work_id, BookFile.rel_path.like("%.pdf"))
        ).all()
        assert len(pdfs) == 1

    # delete, then convert again (delete/regenerate anytime)
    r = api_delete(client, f"/api/works/{work_id}/pdf-epub", owner["csrf"])
    assert r.status_code == 200
    assert not path.exists() and not report_path.exists()
    st = client.get(f"/api/works/{work_id}/pdf-epub").json()
    assert st["eligible"] is True and st["edition"] is None
    r = api_post(client, f"/api/works/{work_id}/pdf-epub", owner["csrf"])
    assert r.status_code == 202
    assert run_pending(session_factory(), settings) == 1
    assert path.is_file()


def test_a_failed_job_is_reported_and_retryable(client, owner, settings):
    _file_id, work_id = _pdf_work(settings, "plain.pdf")
    api_post(client, f"/api/works/{work_id}/pdf-epub", owner["csrf"])

    with session_factory()() as db:
        job = db.scalar(select(Job).where(Job.kind == "pdf2epub").order_by(Job.id.desc()))
        job.payload = {
            "pdf_file_id": job.payload["pdf_file_id"],
            "pdf_sha256": "f" * 64,
            "text_sha256": job.payload["text_sha256"],
        }  # the PDF is gone: fail
        db.commit()
    assert run_pending(session_factory(), settings) == 1
    st = client.get(f"/api/works/{work_id}/pdf-epub").json()
    assert st["job"]["status"] == "failed" and st["job"]["error"] == "pdf_changed"
    assert st["edition"] is None

    r = api_post(client, f"/api/works/{work_id}/pdf-epub/retry", owner["csrf"])
    assert r.status_code == 202
    assert run_pending(session_factory(), settings) == 1
    st = client.get(f"/api/works/{work_id}/pdf-epub").json()
    assert st["job"]["status"] == "succeeded"
    with session_factory()() as db:
        assert (
            db.scalar(select(Job).where(Job.work_id == work_id, Job.status == JobStatus.FAILED))
            is not None
        )


def test_a_work_without_a_text_pdf_cannot_convert(client, owner, settings):
    st = client.get("/api/works/999999/pdf-epub")
    assert st.status_code == 404
