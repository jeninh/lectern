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
from lectern.pdf2epub import probe, structure
from lectern.pdf2epub.build import (
    CONVERTED_NAME,
    ConversionError,
    build_epub,
    page_labels,
    preview,
    regions,
)
from lectern.pdf2epub.rules import ChapterRule, PageNumbers, Rules, defaults
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


# --- structure (issue #63): figures, tables, footnotes, maths, verse, styles, page images -------

S = EXPECTED["structured"]


def analysed(name: str = "structured.pdf") -> tuple[dict, dict]:
    doc = read(name)
    return doc, structure.analyse(doc, probe.scan(FIX / name))


def convert_structured(rules: Rules | None = None, tmp: Path | None = None) -> tuple[bytes, dict]:
    doc, st = analysed()
    rules = rules if rules is not None else defaults(st)
    out = Path(tmp or "/tmp") / "lectern-structured-images"
    out.mkdir(parents=True, exist_ok=True)
    names = probe.render(FIX / "structured.pdf", regions(doc, st, rules), out)
    images = {n: (out / n).read_bytes() for n in names}
    return build_epub(doc, title="T", author="A", language="en", source_name="structured.pdf",
                      pdf_sha256=doc["pdf_sha256"], structure=st, rules=rules,
                      images=images)  # fmt: skip


def files(epub: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(epub)) as zf:
        return {n[len("OEBPS/") :]: zf.read(n).decode("utf-8", "replace")
                for n in zf.namelist() if n.startswith("OEBPS/")}  # fmt: skip


def flat(html: str) -> str:
    """Text with tags removed and spaces collapsed."""
    return " ".join(re.sub(r"<[^>]+>", "", html).split())


def test_styles_drop_caps_lists_quotes_and_verse(tmp_path):
    epub, report = convert_structured(tmp=tmp_path)
    f = files(epub)
    s1, s2, s3 = f["s1.xhtml"], f["s2.xhtml"], f["s3.xhtml"]
    # the drop cap rejoins its word, and the paragraph reads as printed
    para = next(p for p in re.findall(r"<p>(.*?)</p>", s1, re.S) if "dropcap" in p)
    assert flat(para) == S["drop_paragraph"]
    for phrase in S["italic"]:
        assert f"<i>{phrase}</i>" in s1
    for phrase in S["bold"]:
        assert f"<b>{phrase}</b>" in s1
    assert "<b>Chapter" not in s1  # a heading is not bold twice
    items = re.findall(r"<li>(.*?)</li>", s3)
    assert "<ul>" in s3 and [flat(i) for i in items] == S["list"]
    quote = re.search(r"<blockquote><p>(.*?)</p></blockquote>", s3, re.S)
    assert quote and flat(quote.group(1)) == S["quote"]
    verse = re.search(r'<p class="verse">(.*?)</p>', s2, re.S)
    assert verse and [flat(x) for x in verse.group(1).split("<br/>")] == S["poem"]
    # nothing else became verse, a list, or a quote
    assert report["converted"]["verse"] == 1 and report["converted"]["lists"] == 1
    assert report["converted"]["quotes"] == 1


def test_figures_tables_maths_and_footnotes(tmp_path):
    epub, report = convert_structured(tmp=tmp_path)
    f = files(epub)
    s1, s2, s3 = f["s1.xhtml"], f["s2.xhtml"], f["s3.xhtml"]
    # a photo with its caption, placed between the text above and below it
    fig = re.search(r'<figure id="[^"]+"><img src="images/([^"]+)" alt="([^"]*)"/>'
                    r"<figcaption>(.*?)</figcaption></figure>", s1)  # fmt: skip
    assert fig and flat(fig.group(3)) == S["captions"][0] and fig.group(1).endswith(".jpg")
    assert s1.index("missing one was the") < fig.start() < s1.index("The fog lifted")
    assert f[f"images/{fig.group(1)}"]
    # a ruled grid is an HTML table, header row included, its caption the table's
    table = re.search(r"<table[^>]*>(.*?)</table>", s2, re.S).group(1)
    rows = [[flat(c) for c in re.findall(r"<t[hd]>(.*?)</t[hd]>", r)]
            for r in re.findall(r"<tr>(.*?)</tr>", table)]  # fmt: skip
    assert rows == S["table"] and "<th>" in table
    assert flat(re.search(r"<caption>(.*?)</caption>", table).group(1)) == S["captions"][1]
    assert "Boat Heron" not in flat(s2)  # the cells left the text flow
    # footnote markers link to asides at the chapter's end; the notes left the body text
    for text, mark, note in S["footnotes"]:
        ref = re.search(
            rf'<sup><a epub:type="noteref" href="#([^"]+)" id="[^"]+">{mark}</a></sup>', s2
        )
        assert ref, mark
        aside = re.search(rf'<aside epub:type="footnote" id="{ref.group(1)}">(.*?)</aside>', s2)
        assert aside and flat(aside.group(1)) == f"{mark} {note}"
        assert flat(s2).index(text) < flat(s2).index(f"{mark} {note}")
    assert s2.rindex("</aside>") > s2.index('class="verse"')  # at the end of the chapter
    # maths: an image of the region, its text as alt
    math = re.search(r'<div class="math"[^>]*><img src="images/([^"]+)" alt="([^"]*)"/>', s3)
    assert math and math.group(2).startswith(S["equation"])
    # a vector drawing is a figure too, its labels as alt
    assert re.search(r'<img src="images/[^"]+\.png" alt="N"/><figcaption><i>'
                     + re.escape(S["captions"][2]), s3)  # fmt: skip
    assert report["converted"]["figures"] == 2 and report["converted"]["footnotes"] == 2


def test_a_low_confidence_page_is_kept_as_an_image_and_can_be_reflowed(tmp_path):
    doc, st = analysed()
    plate = S["plate_page"]
    flagged = [p["page"] for p in st["pages"] if p["flagged"]]
    assert flagged == [plate]  # the picture page, not the pages of text
    assert defaults(st).page_images == [plate]
    epub, report = convert_structured(tmp=tmp_path)
    s3 = files(epub)["s3.xhtml"]
    page = re.search(rf'<figure class="page-image"[^>]*><span epub:type="pagebreak" '
                     rf'id="page-{plate}"[^>]*/><img src="images/page-{plate}.jpg"', s3)  # fmt: skip
    assert page and report["page_images"] == [plate]
    # the person decides otherwise: the page is reflowed, its caption as text
    epub, report = convert_structured(Rules(page_images=[]), tmp=tmp_path)
    s3 = files(epub)["s3.xhtml"]
    assert "page-image" not in s3 and 'id="page-4"' in s3 and report["page_images"] == []
    assert any("low-confidence page(s) [4]" in w for w in report["warnings"])


def test_rules_change_the_conversion(tmp_path):
    doc, st = analysed()
    # footnotes right after their paragraph, tables as images, no page numbers
    rules = Rules(footnotes="popup", tables="image", maths="text",
                  page_numbers=PageNumbers(keep=False), page_images=[])  # fmt: skip
    epub, report = convert_structured(rules, tmp=tmp_path)
    f = files(epub)
    s2, s3 = f["s2.xhtml"], f["s3.xhtml"]
    assert s2.index("<aside") < s2.index("<figure")  # notes before the table that follows
    assert "<table" not in s2 and S["captions"][1] in flat(s2)
    assert 'class="math"' not in s3 and "α + β = γ" in s3
    assert "pagebreak" not in "".join(f.values()) and "pageBreakSource" not in f["content.opf"]
    # footnotes left inline: no links, the notes stay text
    epub, _ = convert_structured(Rules(footnotes="inline"), tmp=tmp_path)
    s2 = files(epub)["s2.xhtml"]
    assert "noteref" not in s2 and S["footnotes"][0][2] in flat(s2)
    # rename and merge chapters
    rules = Rules(
        chapters=[ChapterRule(index=0, title="Harbour"), ChapterRule(index=2, merge=True)]
    )
    epub, report = convert_structured(rules, tmp=tmp_path)
    f = files(epub)
    assert report["sections"] == 2 and "s3.xhtml" not in f
    assert "<h2>Harbour</h2>" in f["s1.xhtml"] and "Chapter Three" in f["s2.xhtml"]
    assert ">Harbour</a>" in f["nav.xhtml"]


def test_the_preview_shows_each_pages_converted_text():
    doc, st = analysed()
    pages = preview(doc, st, defaults(st), [2, 4])
    assert [p["page"] for p in pages] == [2, 4]
    two, four = pages
    assert S["poem"][0] in two["html"] and "<table" in two["html"]
    assert four["as_image"] and four["confidence"] < 0.6 and 'class="ph"' in four["html"]
    assert "<img" not in two["html"] + four["html"]  # the preview never loads images


def test_text_pdfs_without_structure_convert_as_before():
    doc, st = analysed("plain.pdf")
    assert not st["zones"] and not st["notes"] and not any(p["flagged"] for p in st["pages"])
    a, _ = convert("plain.pdf")
    b, _ = build_epub(doc, title=EXPECTED["title"], author="Ada Marlow", language="en",
                      source_name="plain.pdf", pdf_sha256=doc["pdf_sha256"], structure=st,
                      rules=defaults(st))  # fmt: skip
    assert flat(spine_text(a)) == flat(spine_text(b))


# --- EPUBCheck (CI pins and requires it; see .github/workflows/ci.yml) ---------------------------


@pytest.mark.parametrize("name", ["plain.pdf", "labels.pdf", "two-column.pdf"])
def test_the_converted_edition_is_a_valid_epub(name):
    epub, _report = convert(name)
    out = Path("/tmp") / f"lectern-{name}.epub"
    out.write_bytes(epub)
    epubcheck(out)


@pytest.mark.parametrize(
    "rules",
    [
        None,
        Rules(footnotes="popup", tables="image", page_images=[]),
        Rules(page_images=[1, 2, 3, 4]),
    ],
    ids=["defaults", "popup-notes-table-images", "every-page-an-image"],
)
def test_the_structured_edition_is_a_valid_epub(rules, tmp_path):
    epub, _report = convert_structured(rules, tmp=tmp_path)
    out = tmp_path / "structured.epub"
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


def test_analyse_review_preview_then_convert_with_rules(client, owner, settings):
    _file_id, work_id = _pdf_work(settings, "structured.pdf")
    base = f"/api/works/{work_id}/pdf-epub"
    assert client.get(f"{base}/analysis").status_code == 409  # not analysed yet
    r = api_post(client, f"{base}/analyse", owner["csrf"])
    assert r.status_code == 202
    assert run_pending(session_factory(), settings) == 1
    st = client.get(base).json()
    assert st["analysed"] and st["job"]["mode"] == "analyse" and st["edition"] is None
    a = client.get(f"{base}/analysis").json()
    assert a["summary"]["footnotes"] == 2 and a["summary"]["flagged_pages"] == [S["plate_page"]]
    assert [c["title"] for c in a["chapters"]] == ["Chapter One", "Chapter Two", "Chapter Three"]
    assert a["rules"]["page_images"] == [S["plate_page"]] and a["rules"] == a["defaults"]
    assert S["plate_page"] in a["preview_pages"]
    img = client.get(f"{base}/preview/{S['plate_page']}.jpg")
    assert img.status_code == 200 and img.content[:2] == b"\xff\xd8"
    assert client.get(f"{base}/preview/999.jpg").status_code == 404
    p = api_post(client, f"{base}/preview", owner["csrf"],
                 json={"pages": [2], "rules": {"footnotes": "inline"}}).json()  # fmt: skip
    assert "noteref" not in p["pages"][0]["html"] and p["pages"][0]["image"] is True

    rules = {"footnotes": "popup", "page_images": []}
    r = api_post(client, base, owner["csrf"], json={"rules": rules})
    assert r.status_code == 202
    assert run_pending(session_factory(), settings) == 1
    st = client.get(base).json()
    assert st["job"]["status"] == "succeeded" and st["edition"] is not None
    assert st["report"]["rules"]["footnotes"] == "popup" and st["report"]["page_images"] == []
    # the rules are saved with the edition: the review sheet and a re-run start from them
    assert client.get(f"{base}/analysis").json()["rules"]["footnotes"] == "popup"
    assert api_post(client, base, owner["csrf"]).status_code == 409  # already converted
    r = api_post(client, base, owner["csrf"], json={"reconvert": True})
    assert r.status_code == 202 and run_pending(session_factory(), settings) == 1
    st = client.get(base).json()
    assert st["report"]["rules"]["footnotes"] == "popup"
    with session_factory()() as db:
        work = db.get(Work, work_id)
        epub = (settings.library_dir / work.dir_path / CONVERTED_NAME).read_bytes()
    assert any(n.startswith("OEBPS/images/") for n in zipfile.ZipFile(io.BytesIO(epub)).namelist())
    # removing the edition takes the saved rules and the analysis with it
    assert api_delete(client, base, owner["csrf"]).status_code == 200
    assert client.get(base).json()["analysed"] is False


def test_a_server_without_ffmpeg_still_converts(client, owner, settings, monkeypatch):
    """Data saver leaves transcodes to a worker when ffmpeg is missing; conversions need no
    ffmpeg and must still run here."""
    monkeypatch.setattr(settings, "ffmpeg_path", "/nonexistent/ffmpeg")
    _file_id, work_id = _pdf_work(settings, "labels.pdf")
    assert api_post(client, f"/api/works/{work_id}/pdf-epub", owner["csrf"]).status_code == 202
    assert run_pending(session_factory(), settings) == 1


# --- many works at once (issue #61) --------------------------------------------------------------


def test_bulk_convert_queues_eligible_works_and_reports_the_rest(
    client, owner, settings, monkeypatch
):
    from tests.conftest import make_epub, make_mp3

    _f, text_a = _pdf_work(settings, "labels.pdf")
    _f, text_b = _pdf_work(settings, "plain.pdf")
    (settings.inbox_path / "scanned.pdf").write_bytes((FIX / "scanned.pdf").read_bytes())
    make_epub(settings.inbox_path / "novel.epub", title="Already An Epub")
    make_mp3(settings.inbox_path / "talk.mp3", title="Only Audio", album="Only Audio")
    drain_inbox(settings)
    from lectern.pdftext.store import sweep

    sweep(session_factory(), settings.library_dir)
    with session_factory()() as db:
        scanned = db.scalar(
            select(BookFile.work_id).where(
                BookFile.media_type == "application/pdf",
                BookFile.work_id.not_in([text_a, text_b]),
            )
        )
        epub = db.scalar(
            select(BookFile.work_id).where(BookFile.media_type == "application/epub+zip")
        )
        audio = db.scalar(select(Work.id).where(Work.id.not_in([text_a, text_b, scanned, epub])))
    assert None not in (scanned, epub, audio)

    # one work's enqueue blows up: reported, and the rest still go through
    from lectern.pdf2epub import jobs as pj

    real = pj.enqueue

    def flaky(db, settings_, work, user_id=None, **kw):
        if work.id == text_b:
            raise RuntimeError("boom")
        return real(db, settings_, work, user_id, **kw)

    monkeypatch.setattr(pj, "enqueue", flaky)
    ids = [text_a, epub, 999999, text_b, scanned, audio]
    r = api_post(client, "/api/works/pdf-epub", owner["csrf"], json={"work_ids": ids})
    assert r.status_code == 200
    got = {x["work_id"]: (x["ok"], x["status"], x.get("error")) for x in r.json()["results"]}
    assert [x["work_id"] for x in r.json()["results"]] == ids  # request order
    assert got == {
        text_a: (True, "queued", None),
        epub: (False, "skipped", "already_epub"),
        999999: (False, "failed", "not_found"),
        text_b: (False, "failed", "internal_error"),
        scanned: (False, "skipped", "no_text_pdf"),
        audio: (False, "skipped", "unsupported"),
    }
    job_a = r.json()["results"][0]["job_id"]
    # the job queue page shows what this request queued as one batch row
    (batch,) = client.get("/api/job-queue").json()["batches"]
    assert batch["id"] == r.json()["batch_id"]
    assert [(j["id"], j["status"]) for j in batch["jobs"]] == [(job_a, "queued")]
    monkeypatch.setattr(pj, "enqueue", real)

    # asking again: the live job is reported, not duplicated; the failed work queues now
    again = api_post(
        client, "/api/works/pdf-epub", owner["csrf"], json={"work_ids": [text_a, text_b]}
    )
    a, b = again.json()["results"]
    assert (a["status"], a["job_id"]) == ("already_queued", job_a)
    assert b["status"] == "queued"

    st = client.get(f"/api/pdf-epub/jobs?ids={job_a},{b['job_id']},424242").json()["jobs"]
    assert [(j["id"], j["work_id"], j["status"]) for j in st] == [
        (job_a, text_a, "queued"),
        (b["job_id"], text_b, "queued"),
    ]
    assert run_pending(session_factory(), settings) == 2  # one at a time, through the queue
    st = client.get(f"/api/pdf-epub/jobs?ids={job_a},{b['job_id']}").json()["jobs"]
    assert {j["status"] for j in st} == {"succeeded"}

    # converted now: an EPUB edition exists, so a third request skips both
    third = api_post(
        client, "/api/works/pdf-epub", owner["csrf"], json={"work_ids": [text_a, text_b]}
    )
    assert [x["error"] for x in third.json()["results"]] == ["already_epub", "already_epub"]


def test_bulk_convert_is_capped_rate_limited_and_hides_others_jobs(client, owner, settings):
    from tests.conftest import login, make_user

    too_many = list(range(1, 202))
    r = api_post(client, "/api/works/pdf-epub", owner["csrf"], json={"work_ids": too_many})
    assert r.status_code == 422
    for _ in range(10):  # 10 per minute per user (the refused body above never got that far)
        assert (
            api_post(
                client, "/api/works/pdf-epub", owner["csrf"], json={"work_ids": [1]}
            ).status_code
            == 200
        )
    assert (
        api_post(client, "/api/works/pdf-epub", owner["csrf"], json={"work_ids": [1]}).status_code
        == 429
    )

    _f, work_id = _pdf_work(settings, "plain.pdf")
    job_id = api_post(client, f"/api/works/{work_id}/pdf-epub", owner["csrf"]).json()["job_id"]
    assert client.get(f"/api/pdf-epub/jobs?ids={job_id}").json()["jobs"][0]["id"] == job_id
    assert client.get("/api/pdf-epub/jobs?ids=1,x").status_code == 422
    # readers manage no jobs and see none
    reader = make_user(client, owner["csrf"], "reader", "reader")
    client.cookies.clear()
    login(client, "reader", reader["password"])
    assert client.get(f"/api/pdf-epub/jobs?ids={job_id}").status_code == 403


def test_bulk_convert_never_reports_an_analysis_as_a_queued_conversion(client, owner, settings):
    """An analysis (the review step) is a pdf2epub job too, but no conversion: bulk convert
    must not hand its id back as "already_queued" (it would finish succeeded, no edition)."""
    _f, work_id = _pdf_work(settings, "structured.pdf")
    base = f"/api/works/{work_id}/pdf-epub"
    analyse = api_post(client, f"{base}/analyse", owner["csrf"]).json()["job_id"]
    r = api_post(client, "/api/works/pdf-epub", owner["csrf"], json={"work_ids": [work_id]})
    (item,) = r.json()["results"]
    assert (item["ok"], item["status"], item["error"]) == (False, "skipped", "analysing")
    assert "job_id" not in item
    assert client.get(f"/api/pdf-epub/jobs?ids={analyse}").json()["jobs"] == []
    # nor does a single convert with rules quietly drop them while a job runs
    assert api_post(client, base, owner["csrf"], json={"rules": {}}).status_code == 409

    assert run_pending(session_factory(), settings) == 1  # the analysis
    r = api_post(client, "/api/works/pdf-epub", owner["csrf"], json={"work_ids": [work_id]})
    (item,) = r.json()["results"]
    assert (item["ok"], item["status"]) == (True, "queued")
    assert api_post(client, base, owner["csrf"], json={"rules": {}}).status_code == 409
    assert run_pending(session_factory(), settings) == 1
    jobs = client.get(f"/api/pdf-epub/jobs?ids={analyse},{item['job_id']}").json()["jobs"]
    assert [(j["id"], j["status"]) for j in jobs] == [(item["job_id"], "succeeded")]
    assert client.get(base).json()["edition"] is not None
