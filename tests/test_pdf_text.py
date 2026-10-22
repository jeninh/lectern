"""PDF text layers and read-along on PDFs (docs/pdf-read-along.md).

Expected values come from the fixture source (tests/fixtures/pdf/expected.json, written by
make_fixtures.py from the same strings it typesets), from PDFium's own text-in-a-rectangle
query (an independent check of every highlight box), or are worked out by hand in the test;
never from the code under test.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from sqlalchemy import func, select
from tests.conftest import (
    api_post,
    drain_inbox,
    ko_headers,
    login,
    make_user,
    needs_ffmpeg,
    put_json,
)

from lectern.align.pipeline import TrackInput, align_work
from lectern.align.tracks import probe
from lectern.align.transcribe import Word
from lectern.db import session_factory
from lectern.ingest.formats import FormatError
from lectern.ingest.hashing import sha256_file
from lectern.models import (
    Alignment,
    AudioTrack,
    BookFile,
    Job,
    JobStatus,
    PdfSentence,
    PdfText,
    Sentence,
    Work,
    WorkProgress,
)
from lectern.pdftext import extract, layout
from lectern.pdftext.document import build_document, sentence_boxes
from lectern.pdftext.extract import FLAG_HYPHEN, RawPage, extract_words
from lectern.pdftext.store import document_sentences, sweep

FIX = Path(__file__).parent / "fixtures" / "pdf"
EXPECTED = json.loads((FIX / "expected.json").read_text())
EXCERPT = Path(__file__).parent / "fixtures" / "magi" / "excerpt.mp3"


def words_only(s: str) -> list[str]:
    return re.findall(r"[\w’']+", s)


def read(name: str) -> dict:
    return build_document(extract_words(FIX / name), "0" * 64)


# --- the layout steps on their own ---------------------------------------------------------------


def test_unicode_ligatures_and_invisible_characters_are_normalised_but_print_stays():
    n = layout.normalize_word
    assert n("ﬁnd") == "find" and n("ﬂow") == "flow" and n("oﬃce") == "office"
    assert n("ｆｕｌｌ") == "full"  # fullwidth forms
    assert n("cafe\u0301") == "café" and len(n("cafe\u0301")) == 4  # NFC
    assert n("co\u00adop") == "coop"  # a soft hyphen inside a word is invisible
    assert n("a\u200bb\ufeff") == "ab"
    assert n("\ue000x") == "x"  # private use (symbol fonts) carries no text
    assert n("“Yes…”") == "“Yes…”" and n("—") == "—"  # quotes, ellipsis, dashes as printed


def W(text, x0, y, x1=None, size=10.0, flags=0, page=0):  # noqa: N802 - a word, briefly
    return layout.Word(text, x0, y, x1 if x1 is not None else x0 + 6 * len(text), y + 10,
                       size, flags, page)  # fmt: skip


def test_two_columns_on_one_baseline_grid_are_read_column_by_column():
    """Both columns share baselines and even have a paragraph gap at the same height (the
    trap for a cut that tries full-width gaps first): left column first, then right."""
    title = [W("Title", 150, 700, size=16)]
    left = [W(f"L{i}", 40, 600 - 13 * i) for i in range(8)]
    right = [W(f"R{i}", 220, 600 - 13 * i) for i in range(8)]
    # a paragraph gap at the same place in both columns
    left += [W(f"L{8 + i}", 40, 600 - 13 * 8 - 10 - 13 * i) for i in range(3)]
    right += [W(f"R{8 + i}", 220, 600 - 13 * 8 - 10 - 13 * i) for i in range(3)]
    regions = layout.xy_cut(title + right + left)
    order = [w.text for r in regions for ln in layout.group_lines(r) for w in ln.words]
    assert order == ["Title", *[f"L{i}" for i in range(11)], *[f"R{i}" for i in range(11)]]


def test_line_end_hyphens_join_words_but_keep_real_hyphens():
    lines = [
        [W("the", 40, 600), W("light-", 70, 600, flags=FLAG_HYPHEN)],
        [W("house", 40, 587), W("had", 80, 587), W("ninety-", 110, 587, flags=FLAG_HYPHEN)],
        [W("one", 40, 574), W("steps;", 70, 574), W("a", 110, 574), W("well-", 120, 574)],
        [W("known", 40, 561), W("Anglo-", 80, 561)],
        [W("Saxon", 40, 548), W("word—", 80, 548)],
        [W("then", 40, 535), W("a", 70, 535), W("well-known", 80, 535), W("end.", 150, 535)],
    ]
    para = layout.Paragraph([layout.Line(ws) for ws in lines])
    vocab = layout.vocabulary(para.lines)
    block = layout.paragraph_block(para, vocab)
    assert block.text == (
        "the lighthouse had ninety-one steps; a well-known Anglo-Saxon word—then a well-known end."
    )
    # every piece of a joined word keeps its own box (two lines, two rectangles)
    light = [f for f in block.fragments if block.text[f.start : f.end] in ("light", "house")]
    assert [(f.box[1]) for f in light] == [600, 587]


def test_running_headers_and_page_numbers_go_but_a_title_in_header_words_stays():
    pages = []
    for i in range(4):
        words = [W("CHAPTER", 150, 570, size=7, page=i), W(f"{i + 3}", 300, 570, size=7, page=i)]
        if i == 0:
            words = [W("CHAPTER", 120, 560, size=18, page=0), W("3", 250, 560, size=18, page=0)]
        words += [W(f"body{i}x{k}", 40, 400 - 13 * k, page=i) for k in range(5)]
        words += [W(f"- {i + 1} -", 200, 20, size=8, page=i)]
        pages.append((RawPage(i, 420, 595, 0, 0, 0, None, 100, 0, []), words))
    kept, removed = layout.strip_margins(pages)
    assert removed == 3 + 4  # three running heads, four page numbers
    assert [w.text for w in kept[0]][:2] == ["CHAPTER", "3"]  # the big chapter title stays
    assert all(not w.text.startswith("CHAPTER") for page in kept[1:] for w in page)


# --- real PDFs through the sandboxed reader ---------------------------------------------------


def _oracle_text(pdf: Path, boxes: list[list[float]]) -> str:
    """What PDFium itself finds inside the boxes (shrunk by half a point, so a neighbour's
    edge is not counted)."""
    doc = pdfium.PdfDocument(str(pdf))
    try:
        out = []
        for page, x0, y0, x1, y1 in boxes:
            tp = doc[page].get_textpage()
            out.append(tp.get_text_bounded(x0 + 0.5, y0 + 0.5, x1 - 0.5, y1 - 0.5))
        return " ".join(out).replace("\ufffe", "")
    finally:
        doc.close()


def test_two_column_text_pdf_reads_in_order_without_running_heads_page_numbers_or_hyphens():
    doc = read("two-column.pdf")
    assert doc["status"] == "ready"
    titles = [s["title"] for s in doc["sections"]]
    assert titles == ["THE KEEPER OF SMALL LIGHTS", "Chapter One", "Chapter Two"]
    assert [s["from_outline"] for s in doc["sections"]] == [True, True, True]
    assert [(s["page_start"], s["page_end"]) for s in doc["sections"]] == [(0, 0), (0, 1), (2, 2)]
    assert doc["sections"][0]["text"] == "THE KEEPER OF SMALL LIGHTS by Ada Marlow"
    for section, chapter in zip(doc["sections"][1:], EXPECTED["chapters"], strict=True):
        # exactly the typeset paragraphs, in reading order, across columns and pages
        assert section["text"] == " ".join([chapter["title"], *chapter["paragraphs"]])
    assert EXPECTED["facts"]["two-column.pdf"]["hyphenated_lines"] >= 3  # the fixture's trap
    assert doc["stats"]["columns"] >= 2 and doc["stats"]["margin_lines_removed"] == 5


def test_every_sentence_box_holds_exactly_that_sentence():
    for name in ("two-column.pdf", "plain.pdf"):
        doc = read(name)
        sentences = document_sentences(doc)
        assert len(sentences) > 20 if name == "two-column.pdf" else len(sentences) > 8
        for s in sentences:
            text = doc["sections"][s["section"]]["text"][s["char_start"] : s["char_end"]]
            got = _oracle_text(FIX / name, s["boxes"])
            assert "".join(words_only(got)) == "".join(words_only(text)), (name, text, got)


def test_a_sentence_over_a_page_break_has_boxes_on_both_pages():
    doc = read("plain.pdf")
    assert [s["title"] for s in doc["sections"]] == ["Pages 1–3"]  # no outline: page ranges
    assert doc["sections"][0]["text"] == " ".join(EXPECTED["field_notes"])
    text = doc["sections"][0]["text"]
    spanning = {
        text[s["char_start"] : s["char_end"]][:19]: s
        for s in document_sentences(doc)
        if s["page_start"] != s["page_end"]
    }
    # where make_fixtures.py broke the pages: inside these two sentences
    assert set(spanning) == {"On the twelfth day ", "If you are not look"}
    assert {b[0] for b in spanning["On the twelfth day "]["boxes"]} == {0, 1}


def test_scans_and_page_numbers_alone_need_ocr():
    for name in ("scanned.pdf", "low-text.pdf"):
        doc = read(name)
        assert doc["status"] == "needs_ocr" and doc["sections"] == []
        assert doc["stats"]["pages_with_text"] == 0


def test_reading_keeps_the_ingest_parser_limits(monkeypatch, tmp_path):
    from lectern.ingest import pdf as pdf_ingest

    # Batches stitch together exactly: one page per sandboxed child gives the same document.
    whole = read("two-column.pdf")
    monkeypatch.setattr(extract, "BATCH_PAGES", 1)
    assert read("two-column.pdf") == whole
    # The server keeps what the children read: a character budget stops reading, whatever
    # the words look like (one long word per line costs few words but many characters).
    monkeypatch.setattr(extract, "MAX_CHARS", 1)
    capped = extract_words(FIX / "two-column.pdf")
    assert capped.truncated and [p.index for p in capped.pages] == [0]
    monkeypatch.setattr(extract, "MAX_CHARS", 10**9)
    monkeypatch.setattr(pdf_ingest, "TIMEOUT_S", 0.001)  # the same wall clock as ingest
    with pytest.raises(FormatError) as e:
        extract_words(FIX / "plain.pdf")
    assert e.value.code == "pdf_too_complex"
    monkeypatch.undo()
    (tmp_path / "fake.pdf").write_bytes(b"%PDF-1.7\nnot really")
    with pytest.raises(FormatError) as e:
        extract_words(tmp_path / "fake.pdf")
    assert e.value.code == "not_pdf"
    from pypdf import PdfReader, PdfWriter

    w = PdfWriter(clone_from=PdfReader(str(FIX / "plain.pdf")))
    w.encrypt("secret")
    w.write(tmp_path / "locked.pdf")
    with pytest.raises(FormatError) as e:
        extract_words(tmp_path / "locked.pdf")
    assert e.value.code == "pdf_encrypted"


def test_sentence_boxes_split_a_word_shared_by_two_sentences():
    section = {"words": [[0, 10, 0, 100.0, 10.0, 200.0, 20.0]]}  # one 10-character "word"
    assert sentence_boxes(section, 0, 4) == [[0, 100.0, 10.0, 140.0, 20.0]]
    assert sentence_boxes(section, 4, 10) == [[0, 140.0, 10.0, 200.0, 20.0]]


# --- in the library ----------------------------------------------------------------------------


def _file(settings, name: str) -> int:
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIX / name, settings.inbox_path / name)
    drain_inbox(settings)
    with session_factory()() as db:
        return db.scalar(select(BookFile.id).where(BookFile.rel_path.like("%.pdf")).order_by(
            BookFile.id.desc()))  # fmt: skip


def _work_of(file_id: int) -> Work:
    with session_factory()() as db:
        return db.get(Work, db.get(BookFile, file_id).work_id)


def test_a_text_pdf_gets_sentences_with_page_maps_and_overlay_boxes(client, owner, settings):
    fid = _file(settings, "two-column.pdf")
    assert client.get(f"/api/files/{fid}/text").json()["status"] == "pending"
    assert sweep(session_factory(), settings.library_dir) == 1
    assert sweep(session_factory(), settings.library_dir) == 0  # read once
    st = client.get(f"/api/files/{fid}/text").json()
    assert (st["status"], st["text_source"], st["map"], st["readalong"]) == (
        "ready", "pdf", "self", False)  # fmt: skip
    assert [s["title"] for s in st["sections"]] == [
        "THE KEEPER OF SMALL LIGHTS", "Chapter One", "Chapter Two"]  # fmt: skip
    work = _work_of(fid)
    with session_factory()() as db:
        n_work = db.query(Sentence).filter_by(work_id=work.id).count()
        n_pdf = db.query(PdfSentence).filter_by(file_id=fid).count()
        assert n_work == n_pdf == st["sentences"] > 20
        doc_path = settings.library_dir / work.dir_path / "derived"
        assert len(list(doc_path.glob("pdf-text-*.json"))) == 1
    r = client.get(f"/api/files/{fid}/sentences?from=0&to=5").json()
    assert r["count"] == st["sentences"] and r["map"] == "self"
    assert [s["work_n"] for s in r["sentences"]] == [0, 1, 2, 3, 4]
    assert all(b[0] == s["page"] for s in r["sentences"] for b in s["boxes"][:1])
    # The PDF is the work's text: the read-along text view gets its sections as blocks.
    t = client.get(f"/api/works/{work.id}/text/1").json()
    assert t["blocks"][0] == {"kind": "h2", "start": 0, "text": "Chapter One"}
    # someone who cannot see the work learns nothing
    rex = make_user(client, owner["csrf"], "rex", "reader")
    login(client, "rex", rex["password"])
    assert client.get(f"/api/files/{fid}/text").status_code == 404
    assert client.get(f"/api/files/{fid}/sentences").status_code == 404


def test_a_scanned_pdf_is_flagged_for_ocr_and_offers_nothing_text_based(client, owner, settings):
    fid = _file(settings, "scanned.pdf")
    sweep(session_factory(), settings.library_dir)
    work = _work_of(fid)
    st = client.get(f"/api/files/{fid}/text").json()
    assert (st["status"], st["pages"], st["pages_with_text"], st["map"]) == (
        "needs_ocr",
        2,
        0,
        None,
    )
    assert client.get(f"/api/files/{fid}/sentences").status_code == 409
    assert client.get(f"/api/works/{work.id}/audiobook").json()["reason"] == "pdf_needs_ocr"
    ra = client.get(f"/api/works/{work.id}/read-aloud").json()
    assert ra["text_source"] is None and ra["pdfs"][0]["status"] == "needs_ocr"
    assert ra["reason"] == "needs_epub" and ra["readalong"] is False
    with session_factory()() as db:
        assert db.query(Sentence).filter_by(work_id=work.id).count() == 0
        assert db.scalars(select(PdfText)).one().text_sha256 is None


def _fake_transcript(doc_path: Path, duration: float):
    """A "narrator" who reads the chapters' words evenly over the track (front matter not)."""
    doc = json.loads(doc_path.read_bytes())
    words = [w for s in doc["sections"][1:] for w in s["text"].split()]
    step = (duration - 2.0) / len(words)

    def transcribe(_tracks, _language, progress):
        progress(1.0)
        return [Word(w, 1.0 + i * step, 1.0 + i * step + step * 0.8) for i, w in
                enumerate(words)], "en"  # fmt: skip

    return transcribe, step


def test_pdf_alignment_goes_only_to_workers_that_can_read_pdf_text(client, owner, settings, app):
    from fastapi.testclient import TestClient

    fid = _file(settings, "two-column.pdf")
    work = _work_of(fid)
    shutil.copy(EXCERPT, settings.library_dir / work.dir_path / "01 reading.mp3")
    sweep(session_factory(), settings.library_dir)
    r = api_post(client, f"/api/works/{work.id}/align", owner["csrf"], {})
    assert r.status_code == 202, r.text
    assert r.json()["capability"] == "align-pdf"
    key = api_post(client, "/api/worker-credentials", owner["csrf"], {"label": "pi"}).json()
    wc = TestClient(app, headers={"Authorization": f"Bearer {key['secret']}"})
    # A worker from before PDF text (it only knows "align") never gets the job.
    old = wc.post("/api/worker/jobs/claim", json={"kinds": ["align"], "worker": "old"})
    assert old.status_code == 204
    c = wc.post(
        "/api/worker/jobs/claim", json={"worker": "mac", "capabilities": ["align", "align-pdf"]}
    )
    assert c.status_code == 200, c.text
    claimed = c.json()
    payload = claimed["job"]["payload"]
    assert payload["text_source"] == "pdf" and set(claimed["inputs"]) == {"text", "tracks"}
    lease = {"X-Lease-Token": claimed["lease"]["token"]}
    text = wc.get(claimed["inputs"]["text"], headers=lease)
    assert text.status_code == 200 and text.headers["content-type"].startswith("application/json")
    assert text.headers["x-content-sha256"] == payload["epub_sha256"]
    doc_path = settings.library_dir / "doc.json"
    doc_path.write_bytes(text.content)
    duration = probe(EXCERPT)[0]
    transcribe, step = _fake_transcript(doc_path, duration)
    out = align_work(
        work_id=work.id,
        epub_path=doc_path,
        epub_sha256=payload["epub_sha256"],
        tracks=[TrackInput(0, EXCERPT, sha256_file(EXCERPT), duration, 0.0)],
        model_label="recorded-small",
        transcribe=transcribe,
    )
    res = wc.put(claimed["result_url"], headers=lease, content=out.sentence_map.model_dump_json())
    assert res.status_code == 200, res.text
    with session_factory()() as db:
        a = db.scalar(select(Alignment).where(Alignment.work_id == work.id))
        assert a.status.value == "succeeded" and a.epub_sha256 == payload["epub_sha256"]
        rows = db.scalars(
            select(Sentence).where(Sentence.work_id == work.id).order_by(Sentence.n)
        ).all()
        # Chapter One's first story sentence starts at its first word's time: the narrator
        # reached it after "Chapter One" (2 words).
        first = next(s for s in rows if s.spine_index == 1 and s.char_start > 0)
        assert first.matched and first.audio_start_s == pytest.approx(1.0 + 2 * step, abs=0.2)
        assert not rows[0].matched  # the title page was not read
    st = client.get(f"/api/files/{fid}/text").json()
    assert st["readalong"] and st["map"] == "self"
    ra = client.get(f"/api/works/{work.id}/read-aloud").json()
    assert ra["readalong"] and ra["reason"] == "export_needs_epub"  # read along on the pages
    assert "EPUB edition" in ra["message"]


def test_a_credential_allowed_to_align_may_also_align_pdf_text(client, owner, settings, app):
    from lectern import queue

    class Cred:
        capabilities = ["align", "tts-kokoro"]

    assert queue.claimable(["align", "align-pdf", "transcode"], Cred()) == ["align", "align-pdf"]


@needs_ffmpeg
def test_a_generated_audiobook_narrates_the_pdf_by_outline_chapter(client, owner, settings):
    from tests.test_tts import FakeSynth, _piper_voice, _runner

    fid = _file(settings, "two-column.pdf")
    work = _work_of(fid)
    sweep(session_factory(), settings.library_dir)
    _piper_voice(settings)
    state = client.get(f"/api/works/{work.id}/audiobook").json()
    assert state["eligible"], state
    assert [(c["title"], c["pages"]) for c in state["chapters"]] == [
        ("THE KEEPER OF SMALL LIGHTS", [1, 1]), ("Chapter One", [1, 2]), ("Chapter Two", [3, 3])]  # fmt: skip
    r = api_post(client, f"/api/works/{work.id}/audiobook", owner["csrf"],
                 {"engine": "piper", "voice": "en_US-test-low", "chapters": [1, 2]})  # fmt: skip
    assert r.status_code == 202, r.text
    synth = FakeSynth()
    assert _runner(settings, {"piper"}, synth).run_once()
    with session_factory()() as db:
        assert db.scalars(select(Job).where(Job.kind == "tts")).one().status == JobStatus.SUCCEEDED
        tracks = db.scalars(select(AudioTrack).where(AudioTrack.work_id == work.id)
                            .order_by(AudioTrack.position)).all()  # fmt: skip
        assert [t.title for t in tracks] == ["Chapter One", "Chapter Two"]
        sent = db.scalars(
            select(Sentence).where(Sentence.work_id == work.id).order_by(Sentence.n)
        ).all()
        assert [s.matched for s in sent if s.spine_index == 0] == [False, False]
        assert all(s.matched for s in sent if s.spine_index > 0)
    # what was spoken is the PDF's sentences, in order, nothing from the running heads
    spoken = " ".join(synth.calls)
    assert spoken.startswith("Chapter One Every evening at dusk")
    assert "THE KEEPER" not in spoken and " 2 " not in f" {spoken} "
    st = client.get(f"/api/files/{fid}/text").json()
    assert st["readalong"] and st["map"] == "self"


def _epub_of_the_story(path: Path) -> Path:
    import zipfile

    chapters = EXPECTED["chapters"]
    opf = f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">urn:uuid:keeper</dc:identifier>
    <dc:title>{EXPECTED["title"]}</dc:title><dc:creator>Ada Marlow</dc:creator><dc:language>en</dc:language></metadata>
  <manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        zf.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>
<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""")  # fmt: skip
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/nav.xhtml", "<html xmlns='http://www.w3.org/1999/xhtml' xmlns:epub="
                    "'http://www.idpf.org/2007/ops'><body><nav epub:type='toc'><ol><li><a href='c1.xhtml'>"
                    "One</a></li></ol></nav></body></html>")  # fmt: skip
        for i, ch in enumerate(chapters):
            body = f"<h1>{ch['title']}</h1>\n" + "\n".join(f"<p>{p}</p>" for p in ch["paragraphs"])
            zf.writestr(f"OEBPS/c{i + 1}.xhtml",
                        f"<html xmlns='http://www.w3.org/1999/xhtml'><body>{body}</body></html>")  # fmt: skip
    return path


def _epub_and_pdf_work(settings) -> tuple[int, int]:
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    _epub_of_the_story(settings.inbox_path / "keeper.epub")
    drain_inbox(settings)
    fid = _file(settings, "two-column.pdf")
    with session_factory()() as db:
        book = db.get(BookFile, fid)
        assert db.query(BookFile).filter_by(work_id=book.work_id).count() == 2  # one work
        return book.work_id, fid


def test_an_epub_and_a_pdf_of_one_book_point_at_the_same_sentences(client, owner, settings):
    from lectern.ingest.sentences import replace_sentences

    work_id, fid = _epub_and_pdf_work(settings)
    sweep(session_factory(), settings.library_dir)
    st = client.get(f"/api/files/{fid}/text").json()
    assert (st["text_source"], st["map"]) == ("epub", "epub")
    epub_texts = _work_sentence_texts(settings, work_id)
    r = client.get(f"/api/files/{fid}/sentences").json()
    doc = read("two-column.pdf")
    assert len(document_sentences(doc)) == len(r["sentences"])
    by_n = {s["n"]: s for s in r["sentences"]}
    sentences = document_sentences(doc)
    matched = 0
    for s in sentences:
        text = doc["sections"][s["section"]]["text"][s["char_start"] : s["char_end"]]
        work_n = by_n[s["n"]]["work_n"]
        if s["section"] == 0:
            assert work_n is None, text  # the PDF's title page is not in the EPUB
            continue
        assert work_n is not None, text
        # Editions cut sentences differently (here the EPUB's list keeps a heading with the
        # paragraph after it): the PDF sentence is a run of words of the one it points at.
        assert _run_of(words_only(text), words_only(epub_texts[work_n])), (text, epub_texts[work_n])
        matched += 1
    assert matched == len(sentences) - 2

    # A new sentence list (an alignment re-segments) makes the old projection unusable until
    # the next sweep projects again.
    with session_factory()() as db:
        rows = db.scalars(
            select(Sentence).where(Sentence.work_id == work_id).order_by(Sentence.n)
        ).all()
        merged = [{"n": i, "spine_index": a.spine_index, "char_start": a.char_start,
                   "char_end": (b.char_end if b and b.spine_index == a.spine_index else a.char_end)}
                  for i, (a, b) in enumerate(zip(rows[::2], [*rows[1::2], None], strict=False))]  # fmt: skip
        replace_sentences(db, work_id, merged)
        db.commit()
    assert client.get(f"/api/files/{fid}/sentences").status_code == 409
    sweep(session_factory(), settings.library_dir)
    again = client.get(f"/api/files/{fid}/sentences").json()["sentences"]
    assert max(s["work_n"] or 0 for s in again) <= len(merged) - 1
    assert again[-1]["work_n"] == len(merged) - 1


def _run_of(part: list[str], whole: list[str]) -> bool:
    return any(whole[i : i + len(part)] == part for i in range(len(whole) - len(part) + 1))


def _work_sentence_texts(settings, work_id: int) -> list[str]:
    from lectern.align.text import iter_spine_items
    from lectern.ingest.sentences import primary_epub

    with session_factory()() as db:
        work = db.get(Work, work_id)
        epub = settings.library_dir / work.dir_path / primary_epub(db, work_id).rel_path
        rows = db.scalars(
            select(Sentence).where(Sentence.work_id == work_id).order_by(Sentence.n)
        ).all()
        items = {i.index: i.text.text for i in iter_spine_items(epub)}
        return [items[s.spine_index][s.char_start : s.char_end] for s in rows]


def test_pdf_pages_and_the_canonical_position_follow_each_other(client, owner, settings):
    csrf = owner["csrf"]
    fid = _file(settings, "two-column.pdf")
    work = _work_of(fid)
    sweep(session_factory(), settings.library_dir)
    with session_factory()() as db:
        book = db.get(BookFile, fid)
        doc_sentences = db.scalars(select(PdfSentence).where(PdfSentence.file_id == fid)
                                   .order_by(PdfSentence.n)).all()  # fmt: skip
        chapter_two = next(s.n for s in doc_sentences if s.section == 2)  # starts page 3
    # The web viewer turns to page 3: the work's position is Chapter Two's first sentence.
    r = put_json(client, f"/api/files/{fid}/progress", csrf, {"page": 3, "pages": 3})
    assert r.status_code == 200
    with session_factory()() as db:
        wp = db.scalar(select(WorkProgress).where(WorkProgress.work_id == work.id))
        assert (wp.sentence_n, wp.raw) == (chapter_two, "page:3")
    # A later position from elsewhere (sentence 3, on page 1) is offered as page 1.
    r = put_json(client, f"/api/works/{work.id}/progress", csrf,
                 {"source": "web", "sentence_n": 3, "device": "Phone"})  # fmt: skip
    assert r.status_code == 200, r.text
    info = client.get(f"/api/files/{fid}/pages").json()
    assert info["text"]["status"] == "ready"
    assert info["position"]["page"] == 1 and info["position"]["device"] == "Phone"
    # KOReader syncs page 3 of the same file: the canonical position follows it ...
    key = api_post(client, "/api/devices", csrf, {"label": "Kobo"}).json()["sync_secret"]
    ko = ko_headers("owner", key)
    put = client.put("/syncs/progress", headers=ko, json={"document": book.partial_md5,
                     "progress": 3, "percentage": 1.0, "device": "Kobo", "device_id": "k"})  # fmt: skip
    assert put.status_code == 200
    with session_factory()() as db:
        wp = db.scalar(select(WorkProgress).where(WorkProgress.work_id == work.id))
        assert (wp.sentence_n, wp.source.value) == (chapter_two, "koreader")
    # ... and a newer web position comes back to KOReader as its page.
    put_json(client, f"/api/works/{work.id}/progress", csrf,
             {"source": "audio", "sentence_n": 3, "device": "Phone"})  # fmt: skip
    got = client.get(f"/syncs/progress/{book.partial_md5}", headers=ko).json()
    assert (got["progress"], got["device"]) == ("1", "Phone")


def test_the_sentence_list_follows_the_text_when_an_epub_comes_and_goes(client, owner, settings):
    """A PDF-only work's sentences are the PDF's; an EPUB added later becomes the text, and the
    list (and the reader's place in it) moves to the EPUB, and back when it goes again."""
    csrf = owner["csrf"]
    fid = _file(settings, "two-column.pdf")
    work_id = _work_of(fid).id
    sweep(session_factory(), settings.library_dir)
    assert client.get(f"/api/files/{fid}/text").json()["map"] == "self"
    with session_factory()() as db:
        pdf_count = db.scalar(select(PdfText.sentence_count).where(PdfText.file_id == fid))
    assert (
        put_json(client, f"/api/files/{fid}/progress", csrf, {"page": 3, "pages": 3}).status_code
        == 200
    )

    def place() -> str:  # the text of the work sentence the reader is at
        with session_factory()() as db:
            n = db.scalar(select(WorkProgress.sentence_n).where(WorkProgress.work_id == work_id))
        return _work_sentence_texts(settings, work_id)[n]

    _epub_of_the_story(settings.inbox_path / "keeper.epub")
    drain_inbox(settings)
    with session_factory()() as db:
        epub = db.scalar(select(BookFile).where(BookFile.work_id == work_id,
                                                BookFile.media_type == "application/epub+zip"))  # fmt: skip
        assert epub is not None, "the EPUB joined the PDF's work"
    sweep(session_factory(), settings.library_dir)
    st = client.get(f"/api/files/{fid}/text").json()
    assert (st["text_source"], st["map"]) == ("epub", "epub")
    # The list is the EPUB's (two chapters, no title page), and the place is still Chapter Two.
    with session_factory()() as db:
        spines = set(db.scalars(select(Sentence.spine_index).where(Sentence.work_id == work_id)))
    assert spines == {0, 1}
    assert place().startswith("Chapter Two") or "Wren read the letter" in place(), place()

    # The EPUB goes: the PDF is the text again, with its own list (a count alone proves nothing).
    with session_factory()() as db:
        db.delete(db.get(BookFile, epub.id))
        db.commit()
    assert client.get(f"/api/files/{fid}/sentences").status_code == 409
    sweep(session_factory(), settings.library_dir)
    st = client.get(f"/api/files/{fid}/text").json()
    assert (st["text_source"], st["map"]) == ("pdf", "self")
    with session_factory()() as db:
        assert (
            db.scalar(select(func.count(Sentence.id)).where(Sentence.work_id == work_id))
            == pdf_count
        )


def test_an_older_offline_page_turn_does_not_rewind_a_newer_audio_position(client, owner, settings):
    from datetime import UTC, datetime, timedelta

    csrf = owner["csrf"]
    fid = _file(settings, "two-column.pdf")
    work = _work_of(fid)
    sweep(session_factory(), settings.library_dir)
    iso = lambda d: d.isoformat()  # noqa: E731
    turned = datetime.now(UTC) - timedelta(minutes=10)  # page 3, offline, ten minutes ago
    # Meanwhile the audiobook moved on, to sentence 3 (page 1), on another device.
    r = put_json(client, f"/api/works/{work.id}/progress", csrf,
                 {"source": "audio", "sentence_n": 3, "device": "Phone"})  # fmt: skip
    assert r.status_code == 200, r.text
    r = put_json(client, f"/api/files/{fid}/progress", csrf, {"page": 3, "pages": 3, "offline": {
        "recorded_at": iso(turned), "sent_at": iso(turned + timedelta(seconds=1))}})  # fmt: skip
    # Neither record moves: the device is told about the newer place, as a page.
    assert r.status_code == 409, r.text
    assert r.json()["server"]["page"] == 1, r.json()
    with session_factory()() as db:
        wp = db.scalar(select(WorkProgress).where(WorkProgress.work_id == work.id))
        assert (wp.sentence_n, wp.source.value) == (3, "audio")
    info = client.get(f"/api/files/{fid}/pages").json()
    assert info["progress"] is None and info["position"]["page"] == 1
    key = api_post(client, "/api/devices", csrf, {"label": "Kobo"}).json()["sync_secret"]
    with session_factory()() as db:
        md5 = db.get(BookFile, fid).partial_md5
    got = client.get(f"/syncs/progress/{md5}", headers=ko_headers("owner", key)).json()
    assert got["progress"] == "1", got
    # A page turned now (online) still moves the work there.
    assert (
        put_json(client, f"/api/files/{fid}/progress", csrf, {"page": 3, "pages": 3}).status_code
        == 200
    )
    with session_factory()() as db:
        wp = db.scalar(select(WorkProgress).where(WorkProgress.work_id == work.id))
        assert wp.raw == "page:3"


def test_a_same_size_sentence_list_with_new_boundaries_needs_a_new_projection(
    client, owner, settings
):
    from lectern.ingest.sentences import replace_sentences

    work_id, fid = _epub_and_pdf_work(settings)
    sweep(session_factory(), settings.library_dir)
    assert client.get(f"/api/files/{fid}/sentences").status_code == 200
    with session_factory()() as db:
        rows = db.scalars(
            select(Sentence).where(Sentence.work_id == work_id).order_by(Sentence.n)
        ).all()
        entries = [{"n": r.n, "spine_index": r.spine_index, "char_start": r.char_start, "char_end": r.char_end}
                   for r in rows]  # fmt: skip
        # Move one boundary: same count, so (in this one-work database) the same row ids.
        i = next(
            k
            for k in range(len(entries) - 1)
            if entries[k]["spine_index"] == entries[k + 1]["spine_index"]
        )
        entries[i]["char_end"] -= 1
        entries[i + 1]["char_start"] -= 1
        replace_sentences(db, work_id, entries)
        db.commit()
    assert client.get(f"/api/files/{fid}/sentences").status_code == 409
    sweep(session_factory(), settings.library_dir)
    assert client.get(f"/api/files/{fid}/sentences").status_code == 200


def test_a_queued_alignment_does_not_make_a_stale_list_the_pdfs(client, owner, settings):
    from lectern.models import Alignment, AlignmentStatus

    work_id, fid = _epub_and_pdf_work(settings)
    sweep(session_factory(), settings.library_dir)  # the list is the EPUB's
    with session_factory()() as db:
        db.delete(db.scalar(select(BookFile).where(BookFile.work_id == work_id,
                                                   BookFile.media_type == "application/epub+zip")))  # fmt: skip
        text_sha = db.scalar(select(PdfText.text_sha256).where(PdfText.file_id == fid))
        # An alignment of the PDF's text is asked for before the sweep repairs the list.
        db.add(Alignment(work_id=work_id, model="m", status=AlignmentStatus.PENDING,
                         epub_sha256=text_sha, audio_fingerprint="x"))  # fmt: skip
        db.commit()
    assert client.get(f"/api/files/{fid}/sentences").status_code == 409
    sweep(session_factory(), settings.library_dir)
    st = client.get(f"/api/files/{fid}/text").json()
    assert (st["text_source"], st["map"]) == ("pdf", "self")
    with session_factory()() as db:
        assert set(db.scalars(select(Sentence.spine_index).where(Sentence.work_id == work_id))) == {
            0,
            1,
            2,
        }


def test_a_repair_never_overwrites_an_alignment_published_while_it_ran(
    client, owner, settings, monkeypatch
):
    """The sweep moves a PDF-only work's list to an EPUB added later; if an alignment of that
    EPUB publishes its timed sentences meanwhile, the alignment's list stays."""
    from lectern.ingest import sentences as ingest_sentences
    from lectern.ingest.sentences import replace_sentences
    from lectern.models import Alignment, AlignmentStatus

    fid = _file(settings, "two-column.pdf")
    work_id = _work_of(fid).id
    sweep(session_factory(), settings.library_dir)
    _epub_of_the_story(settings.inbox_path / "keeper.epub")
    drain_inbox(settings)
    real = ingest_sentences.epub_sentences

    def published_meanwhile(path):
        entries = real(path)
        with session_factory()() as other:  # the alignment worker, in its own session
            replace_sentences(other, work_id, [
                {**e, "audio_start_s": float(i), "audio_end_s": i + 1.0, "matched": True}
                for i, e in enumerate(entries)])  # fmt: skip
            sha = other.scalar(select(BookFile.sha256).where(
                BookFile.work_id == work_id, BookFile.media_type == "application/epub+zip"))  # fmt: skip
            other.add(Alignment(work_id=work_id, model="m", status=AlignmentStatus.SUCCEEDED,
                                epub_sha256=sha, audio_fingerprint="x"))  # fmt: skip
            other.commit()
        return entries

    monkeypatch.setattr(ingest_sentences, "epub_sentences", published_meanwhile)
    sweep(session_factory(), settings.library_dir)
    with session_factory()() as db:
        rows = db.scalars(select(Sentence).where(Sentence.work_id == work_id)).all()
    assert rows and all(r.matched and r.audio_start_s is not None for r in rows)
