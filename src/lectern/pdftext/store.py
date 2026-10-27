"""Keep every PDF's canonical text current, and the work's text source in one place.

The housekeeping thread (:mod:`lectern.services`) calls :func:`sweep` with the library rescan
(every ``LECTERN_TRACK_RESCAN_SECONDS`` and at startup). For each PDF in the library it:

1. reads the text layer when the file is new, changed, or was read by an older extractor
   (:func:`refresh_file`: the sandboxed reader, then the layout), one PDF at a time;
2. writes the canonical text document next to the work
   (``derived/pdf-text-<sha256 prefix>.json``, replaced atomically, never in place) and its
   sentences with page and box maps (``pdf_sentences``);
3. marks a PDF whose pages mostly have no text ``needs_ocr``, until OCR has recognised it:
   then the document is built from the recognised words (``derived/ocr-<prefix>.json``,
   :mod:`lectern.ocr`) through the same layout step instead (:func:`read_ocr`), it is
   ``ready``, and everything below works on the scan unchanged. A new recognition (another
   language, a retry) or a deleted copy reads the PDF again (``pdf_texts.ocr_sha256``);
4. for a work whose text is an EPUB, projects the PDF's sentences onto the work's sentences
   (:mod:`lectern.pdftext.project`) whenever that list changes (an alignment, a narration);
5. for a work whose only text is a PDF, gives it the text-only sentence list a work with an
   EPUB gets at ingest, so positions, alignment, narration, and read-along all have
   coordinates.

:func:`work_text` is the one place that decides what a work's text is: its primary EPUB when
it has one, else its first PDF whose text is ready. Alignment, narration, and the worker
inputs ask it instead of looking for an EPUB.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from lectern.ingest import formats
from lectern.ingest.formats import FormatError, ProbeBusy
from lectern.models import BookFile, FileKind, PdfSentence, PdfText, Sentence, Work
from lectern.pdftext.document import DOCUMENT_VERSION, build_document, dumps, sha256_of
from lectern.pdftext.extract import RawDoc

log = logging.getLogger("lectern.pdftext")

PENDING, READY, NEEDS_OCR, FAILED = "pending", "ready", "needs_ocr", "failed"
MAX_ATTEMPTS = 3
DERIVED_DIR = "derived"


def document_rel(pdf_sha256: str) -> str:
    """Where a PDF's text document lives, relative to its work directory."""
    return f"{DERIVED_DIR}/pdf-text-{pdf_sha256[:16]}.json"


@dataclass(frozen=True)
class TextSource:
    """A work's text: ``kind`` ``epub`` (the EPUB itself) or ``pdf`` (the PDF's canonical text
    document). ``sha256`` is the digest of the bytes alignment and narration read (the EPUB,
    or the document), which is what jobs, sentence maps, and alignments record."""

    kind: str
    file: BookFile
    sha256: str


def work_text(db: Session, library: Path, work: Work) -> tuple[TextSource, Path] | None:
    """The work's text source and the path of the bytes to read (see the module docstring)."""
    from lectern.align.tracks import work_epub

    found = work_epub(db, library, work)
    if found is not None:
        return TextSource("epub", found[0], found[0].sha256), found[1]
    pair = text_pdf(db, work.id)
    if pair is None:
        return None
    book, row = pair
    root = library.resolve()
    path = (root / work.dir_path / document_rel(book.sha256)).resolve()
    if root not in path.parents or not path.is_file():
        return None
    return TextSource("pdf", book, row.text_sha256 or ""), path


def text_pdf(db: Session, work_id: int) -> tuple[BookFile, PdfText] | None:
    """The work's first PDF (in the order they were added) whose text is ready and current."""
    row = db.execute(
        select(BookFile, PdfText)
        .join(PdfText, PdfText.file_id == BookFile.id)
        .where(
            BookFile.work_id == work_id,
            BookFile.media_type == formats.PDF,
            PdfText.status == READY,
            PdfText.pdf_sha256 == BookFile.sha256,
            PdfText.version == DOCUMENT_VERSION,
        )
        .order_by(BookFile.added_at, BookFile.id)
        .limit(1)
    ).first()
    return (row[0], row[1]) if row else None


def pdf_text_of(db: Session, file_id: int) -> PdfText | None:
    return db.scalar(select(PdfText).where(PdfText.file_id == file_id))


@lru_cache(maxsize=8)
def _load(path: str, text_sha256: str) -> dict:  # text_sha256 keys the cache
    return json.loads(Path(path).read_bytes())


def load_document(library: Path, work: Work, book: BookFile, row: PdfText) -> dict | None:
    root = library.resolve()
    path = (root / work.dir_path / document_rel(book.sha256)).resolve()
    if root not in path.parents or not path.is_file() or not row.text_sha256:
        return None
    try:
        return _load(str(path), row.text_sha256)
    except (OSError, ValueError):
        return None


# --- reading a PDF -----------------------------------------------------------------------------


def stale(row: PdfText | None, pdf_sha256: str, ocr_sha256: str | None = None) -> bool:
    """Should this PDF be read (again)? New, changed, read by an older extractor, or its OCR
    words are not the ones it was read from (recognised, recognised again, or deleted:
    ``ocr_sha256`` is :func:`ocr_digest`'s); a failure is retried on later sweeps until
    ``MAX_ATTEMPTS`` (a new extractor version or recognition starts over)."""
    if row is None or row.pdf_sha256 != pdf_sha256 or row.version != DOCUMENT_VERSION:
        return True
    if row.ocr_sha256 != ocr_sha256:
        return True
    return row.status == PENDING or (row.status == FAILED and row.attempts < MAX_ATTEMPTS)


# --- recognised text (OCR) ---------------------------------------------------------------------


def ocr_digest(work_dir: Path, pdf_sha256: str) -> str | None:
    """The SHA-256 of the PDF's finished OCR words (``derived/ocr-<prefix>.json``), or None
    when it has none (never recognised, or the copy was deleted). Read from the checksum
    sidecar the OCR publish writes next to it; hashed when the sidecar is missing."""
    from lectern.ocr.jobs import words_rel

    path = work_dir / words_rel(pdf_sha256)
    if not path.is_file():
        return None
    try:
        digest = path.with_name(path.name + ".sha256").read_text("ascii").strip()
    except (OSError, UnicodeDecodeError):
        digest = ""
    if len(digest) == 64:
        return digest
    try:
        return sha256_of(path.read_bytes())
    except OSError:
        return None


def read_ocr(path: Path, pdf_sha256: str) -> tuple[RawDoc, dict]:
    """The OCR words file of a PDF as the text-layer reader's pages (``RawDoc``), and how
    they were recognised (``{"engine", "language"}``), for :func:`build_document`. Raises
    ``ValueError`` when the file is not that PDF's words."""
    from lectern.pdftext.extract import TOC_MAX, _page

    data = json.loads(path.read_bytes())
    if not isinstance(data, dict) or data.get("kind") != "ocr":
        raise ValueError("not an OCR words file")
    if data.get("pdf_sha256") != pdf_sha256:
        raise ValueError("the OCR words are another PDF's")
    pages = data.get("pages")
    if not isinstance(pages, list) or not all(isinstance(p, dict) for p in pages):
        raise ValueError("the OCR words have no pages")
    try:
        raw_pages = [_page(p) for p in pages]
        toc = [(int(lv), str(t), int(pi)) for lv, t, pi in (data.get("toc") or [])[:TOC_MAX]]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"malformed OCR words: {type(exc).__name__}") from exc
    if any(p.index != i for i, p in enumerate(raw_pages)):
        raise ValueError("the OCR words skip pages")
    info = {"engine": str(data.get("engine") or "ocr"), "language": str(data.get("language") or "")}
    return RawDoc(raw_pages, toc, bool(data.get("truncated"))), info


def document_sentences(doc: dict) -> list[dict]:
    """The document's sentences, segmented exactly as alignment segments a spine item, with
    their pages and highlight boxes."""
    from lectern.align.text import segment_text
    from lectern.pdftext.document import SectionText, sentence_boxes

    out: list[dict] = []
    for k, section in enumerate(doc.get("sections") or []):
        text = SectionText(section["text"], section["blocks"])
        starts = [w[0] for w in section["words"]]
        for start, end in segment_text(text.text, text.block_starts()):
            boxes = sentence_boxes(section, start, end, starts)
            pages = [b[0] for b in boxes] or [section["page_start"]]
            out.append(
                {
                    "n": len(out),
                    "section": k,
                    "char_start": start,
                    "char_end": end,
                    "page_start": min(pages),
                    "page_end": max(pages),
                    "boxes": boxes,
                }
            )
    return out


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def refresh_file(session_factory: sessionmaker, library: Path, file_id: int) -> str | None:
    """Read one PDF's text layer and store the result. Returns the new status, or ``None``
    when nothing was done (the file is gone, or every parser slot was busy: try later)."""
    from lectern.fsops import LIBRARY_LOCK

    with session_factory() as db:
        book = db.get(BookFile, file_id)
        work = db.get(Work, book.work_id) if book else None
        if book is None or work is None or book.media_type != formats.PDF:
            return None
        sha, work_id = book.sha256, work.id
        root = library.resolve()
        work_dir = (root / work.dir_path).resolve()
        path = (root / work.dir_path / book.rel_path).resolve()
        if root not in path.parents or not path.is_file() or root not in work_dir.parents:
            return None
    error = None
    doc = None
    # Recognised words, when OCR has run: they are the scan's text (its own text layer is
    # what made it a scan), laid out by the same step as a text PDF's.
    ocr = ocr_digest(work_dir, sha)
    try:
        from lectern.ocr.jobs import words_rel
        from lectern.pdftext.extract import extract_words

        if ocr is not None:
            words = work_dir / words_rel(sha)
            try:
                raw, info = read_ocr(words, sha)
            except (OSError, ValueError) as exc:
                raise FormatError("ocr_unreadable", str(exc)[:200]) from exc
            doc = build_document(raw, sha, ocr=info)
        else:
            doc = build_document(extract_words(path), sha)
    except ProbeBusy:
        return None
    except FormatError as exc:
        error = f"{exc.code}: {exc.detail}"[:500] if getattr(exc, "detail", "") else exc.code
    except Exception as exc:  # noqa: BLE001 - a layout bug must not stop the sweep
        log.exception("reading the text of file %d failed", file_id)
        error = f"internal: {type(exc).__name__}"
    # Serialising and boxing a long PDF takes seconds: done before the locks, so uploads and
    # other writers never wait on it.
    data = dumps(doc) if doc is not None and doc["status"] == READY else None
    sentences = document_sentences(doc) if data is not None else []
    with LIBRARY_LOCK, session_factory() as db:
        book = db.get(BookFile, file_id)
        work = db.get(Work, work_id)
        if book is None or work is None or book.sha256 != sha or book.work_id != work_id:
            return None  # replaced or moved meanwhile: the next sweep reads the new one
        if ocr_digest(library.resolve() / work.dir_path, sha) != ocr:
            return None  # recognised again (or deleted) meanwhile: the next sweep reads it
        if data is not None:
            # Written before any database write, so the SQLite write lock is not held over
            # the fsync of a large document.
            try:
                _write_atomic(library / work.dir_path / document_rel(sha), data)
            except OSError as exc:
                # An unwritable work folder fails this PDF (retried up to MAX_ATTEMPTS),
                # never the sweep: the other PDFs are still read.
                doc, error = None, f"write_failed: {exc.strerror or type(exc).__name__}"[:500]
        row = pdf_text_of(db, file_id)
        if row is None:
            row = PdfText(file_id=file_id, work_id=work_id, pdf_sha256=sha, attempts=0)
            db.add(row)
        if row.pdf_sha256 != sha or row.version != DOCUMENT_VERSION or row.ocr_sha256 != ocr:
            row.attempts = 0
        row.work_id, row.pdf_sha256, row.version = work_id, sha, DOCUMENT_VERSION
        row.ocr_sha256 = ocr
        row.attempts = (row.attempts or 0) + 1
        # Read again with the same result (a regenerated document): the work's list is still
        # this PDF's when it was before. An EPUB projection is redone (epub_n is rewritten).
        was_self = row.status == READY and row.text_sha256 and is_self(db, work, row)
        old_text = row.text_sha256
        row.projected_key = None
        db.execute(delete(PdfSentence).where(PdfSentence.file_id == file_id))
        if doc is None:
            row.status, row.error, row.text_sha256 = FAILED, error, None
            row.sentence_count = row.chars = 0
            db.commit()
            log.warning("could not read the text of %s: %s", book.rel_path, error)
            return FAILED
        stats = doc["stats"]
        row.pages, row.pages_with_text = stats["pages"], stats["pages_with_text"]
        row.chars, row.error = stats["chars"], None
        row.stats = {**stats, "ocr": doc["ocr"]} if "ocr" in doc else stats
        if doc["status"] != READY:
            row.status, row.text_sha256, row.sentence_count = NEEDS_OCR, None, 0
            row.sections = []
            db.commit()
            return NEEDS_OCR
        db.add_all(PdfSentence(file_id=file_id, **s) for s in sentences)
        row.status, row.text_sha256 = READY, sha256_of(data)
        row.attempts = 0  # a later failure gets its full MAX_ATTEMPTS again
        row.sentence_count = len(sentences)
        if was_self and old_text == row.text_sha256:
            db.flush()
            row.projected_key = self_key(db, work_id, row)
        row.sections = [
            {
                "title": s["title"],
                "page_start": s["page_start"],
                "page_end": s["page_end"],
                "from_outline": s["from_outline"],
                "sentences": sum(1 for x in sentences if x["section"] == k),
                "characters": len(s["text"]),
            }
            for k, s in enumerate(doc["sections"])
        ]
        db.commit()
        log.info(
            "read %d pages of %s: %d sentences in %d sections",
            row.pages, book.rel_path, row.sentence_count, len(row.sections),
        )  # fmt: skip
        return READY


# --- the work's sentences and the projection ----------------------------------------------------


def ensure_pdf_sentences(db: Session, library: Path, work: Work) -> int:
    """Keep a work's text-only sentence list on the work's text source (no alignment yet;
    an alignment's sentence map owns the list once there is one).

    - The text is a PDF and the list is empty, or is not that PDF's (the EPUB it came from
      was removed, or another PDF was the text): install the PDF's list (``matched =
      false``), exactly the list an alignment of it will produce, so ``n`` is the same in
      ``sentences`` and ``pdf_sentences``, and stamp the PDF as the list's source.
    - The text is an EPUB but the list is still a PDF's (the EPUB was added later): rebuild
      the EPUB's list, moving positions through the EPUB↔PDF projection.

    Positions move with the list. Returns the sentences written. Caller commits."""
    from lectern.ingest.sentences import epub_sentences, replace_sentences

    if _installed_alignment(db, work.id) is not None:
        return 0
    found = work_text(db, library, work)
    if found is None:
        return 0
    old_count = db.scalar(select(func.count(Sentence.id)).where(Sentence.work_id == work.id))
    before = sentences_key(db, work.id, "")
    source, path = found
    if source.kind == "pdf":
        row = pdf_text_of(db, source.file.id)
        if row is None or (old_count and is_self(db, work, row)):
            return 0
        rows = db.scalars(
            select(PdfSentence).where(PdfSentence.file_id == source.file.id).order_by(PdfSentence.n)
        ).all()
        entries = [
            {"n": r.n, "spine_index": r.section, "char_start": r.char_start, "char_end": r.char_end}
            for r in rows
        ]
        if not _still_ours(db, work.id, before):
            return 0
        if old_count:
            replace_sentences(db, work.id, entries, move=_proportional(old_count, len(entries)))
        else:
            replace_sentences(db, work.id, entries)
        for other in db.scalars(select(PdfText).where(PdfText.work_id == work.id)):
            other.projected_key = None
        row.projected_key = self_key(db, work.id, row)
        return len(entries)
    # The text is an EPUB: is the list still the PDF's it was before the EPUB came?
    if not old_count:
        return 0  # the EPUB's own list is built at ingest (ingest.sentences)
    was = next(
        (r for r in db.scalars(select(PdfText).where(PdfText.work_id == work.id))
         if is_self(db, work, r)),
        None,
    )  # fmt: skip
    if was is None:
        return 0
    try:
        entries = epub_sentences(path)
    except Exception:  # noqa: BLE001 - an unreadable EPUB keeps the list it has
        log.warning("could not segment the EPUB of work %d", work.id)
        return 0
    move = _proportional(old_count, len(entries))
    book = db.get(BookFile, was.file_id)
    doc = load_document(library, work, book, was) if book else None
    if doc is not None and entries:
        from lectern.align.text import iter_spine_items
        from lectern.pdftext.project import project

        items = {item.index: item.text.text for item in iter_spine_items(path)}
        work_texts = [items.get(e["spine_index"], "")[e["char_start"] : e["char_end"]]
                      for e in entries]  # fmt: skip
        pdf_rows = db.scalars(
            select(PdfSentence).where(PdfSentence.file_id == was.file_id).order_by(PdfSentence.n)
        ).all()
        sections = doc["sections"]
        epub_n = project(
            work_texts, [sections[r.section]["text"][r.char_start : r.char_end] for r in pdf_rows]
        )
        move = _through(epub_n, move)
    if not _still_ours(db, work.id, before):
        return 0
    replace_sentences(db, work.id, entries, move=move)
    was.projected_key = None
    return len(entries)


def self_key(db: Session, work_id: int, row: PdfText) -> str:
    """The stamp a PDF's text row carries while the work's sentence list is the one installed
    from it (``projected_key`` holds it: a PDF that is the work's text has no projection)."""
    return hashlib.sha256(
        f"self:{sentences_key(db, work_id, row.text_sha256 or '')}".encode()
    ).hexdigest()


def is_self(db: Session, work: Work, row: PdfText) -> bool:
    """Whether the work's sentences are this PDF's: installed from it (and not replaced
    since), or the sentence map of an alignment of its text. A count is not enough: an EPUB's
    list can happen to have as many sentences."""
    alignment = _installed_alignment(db, work.id)
    if alignment is not None:
        return bool(row.text_sha256) and alignment.epub_sha256 == row.text_sha256
    return row.projected_key is not None and row.projected_key == self_key(db, work.id, row)


def _still_ours(db: Session, work_id: int, before: str) -> bool:
    """Take the database's write lock, then check nothing replaced the sentence list while the
    new one was computed (an alignment or a narration publishing its result): from here until
    the caller commits, nobody else can. When something did, back off and leave its list."""
    from sqlalchemy import text

    # A write starts the transaction (pysqlite begins one lazily, before the first write), so
    # the reads below see everything committed until now, and nothing commits after them.
    db.execute(text("UPDATE works SET id = id WHERE id = :id"), {"id": work_id})
    if _installed_alignment(db, work_id) is None and sentences_key(db, work_id, "") == before:
        return True
    db.rollback()
    return False


def _installed_alignment(db: Session, work_id: int):
    """The work's alignment when its result is what the sentence list holds (succeeded, or
    succeeded and since gone stale). A queued, running, or failed one has installed nothing:
    its ``epub_sha256`` is only what it was asked to read."""
    from lectern.models import Alignment, AlignmentStatus

    return db.scalar(
        select(Alignment).where(
            Alignment.work_id == work_id,
            Alignment.status.in_([AlignmentStatus.SUCCEEDED, AlignmentStatus.STALE]),
        )
    )


def _proportional(old: int, new: int):
    return lambda n: round(n * new / old) if old else 0


def _through(epub_n: list[int | None], fallback):
    """Old (PDF) sentence ``n`` to the EPUB sentence it was projected onto, or the nearest
    earlier one that was; past the projection, ``fallback``."""

    def move(n: int) -> int:
        for k in range(min(n, len(epub_n) - 1), -1, -1):
            if epub_n[k] is not None:
                return epub_n[k]
        return fallback(n)

    return move


def sentences_key(db: Session, work_id: int, source_sha: str) -> str:
    """A digest of the work's sentence list (every sentence's span) and its text source, so it
    changes whenever the boundaries do. Row ids are not enough: SQLite reuses them after the
    list is deleted and inserted again, so a same-size list would keep its old key."""
    h = hashlib.sha256(f"{source_sha}:".encode())
    for n, si, a, b in db.execute(
        select(Sentence.n, Sentence.spine_index, Sentence.char_start, Sentence.char_end)
        .where(Sentence.work_id == work_id)
        .order_by(Sentence.n)
    ):
        h.update(f"{n},{si},{a},{b};".encode())
    return h.hexdigest()


def refresh_projection(
    db: Session, library: Path, work: Work, book: BookFile, row: PdfText
) -> bool:
    """Point the PDF's sentences at the work's (EPUB) sentences, if that list changed since the
    last projection. A PDF that is itself the work's text has ``epub_n`` NULL everywhere: its
    ``n`` already is the work's. Returns True when anything was written. Caller commits."""
    from lectern.align.text import iter_spine_items
    from lectern.pdftext.project import project

    found = work_text(db, library, work)
    if found is None or found[0].kind != "epub":
        # No EPUB: no projection. A self stamp (ensure_pdf_sentences) stays as it is.
        if row.projected_key is None or is_self(db, work, row):
            return False
        db.execute(
            PdfSentence.__table__.update().where(PdfSentence.file_id == book.id).values(epub_n=None)
        )
        row.projected_key = None
        return True
    key = sentences_key(db, work.id, found[0].sha256)
    if row.projected_key == key:
        return False
    doc = load_document(library, work, book, row)
    if doc is None:
        return False
    spans = db.execute(
        select(Sentence.spine_index, Sentence.char_start, Sentence.char_end)
        .where(Sentence.work_id == work.id)
        .order_by(Sentence.n)
    ).all()
    if not spans:
        return False
    items = {item.index: item.text.text for item in iter_spine_items(found[1])}
    work_texts = [items.get(si, "")[a:b] for si, a, b in spans]
    pdf_rows = db.scalars(
        select(PdfSentence).where(PdfSentence.file_id == book.id).order_by(PdfSentence.n)
    ).all()
    sections = doc["sections"]
    pdf_texts = [sections[r.section]["text"][r.char_start : r.char_end] for r in pdf_rows]
    for r, n in zip(pdf_rows, project(work_texts, pdf_texts), strict=True):
        r.epub_n = n
    row.projected_key = key
    return True


def sweep(session_factory: sessionmaker, library: Path, *, limit: int = 20) -> int:
    """One pass over the library's PDFs (see the module docstring). Returns PDFs read."""
    with session_factory() as db:
        books = db.execute(
            select(BookFile.id, BookFile.sha256, PdfText, Work.dir_path)
            .join(Work, Work.id == BookFile.work_id)
            .outerjoin(PdfText, PdfText.file_id == BookFile.id)
            .where(
                BookFile.media_type == formats.PDF,
                BookFile.kind == FileKind.EBOOK,
                Work.trashed_at.is_(None),
            )
            .order_by(BookFile.id)
        ).all()
        root = library.resolve()
        # A READY row whose document is gone (a library restored from a backup, which leaves
        # out derived/) is read again: the same PDF gives the same document and sentences.
        todo = [
            fid for fid, sha, row, d in books
            if stale(row, sha, ocr_digest(root / d, sha))
            or (row.status == READY and not (root / d / document_rel(sha)).is_file())
        ]  # fmt: skip
        ready = [fid for fid, _sha, row, _d in books if row is not None and row.status == READY]
    read = 0
    for file_id in todo[:limit]:
        try:
            if refresh_file(session_factory, library, file_id) is not None:
                read += 1
                ready.append(file_id)
        except Exception:  # noqa: BLE001 - one PDF must not stop the others
            log.exception("reading the text of file %d failed", file_id)
    # Database only (the projection reads the EPUB): no library lock, so uploads never wait.
    for file_id in dict.fromkeys(ready):
        with session_factory() as db:
            try:
                book = db.get(BookFile, file_id)
                row = pdf_text_of(db, file_id)
                work = db.get(Work, book.work_id) if book else None
                if book is None or row is None or work is None or row.status != READY:
                    continue
                # Commit the repair (it holds the write lock) before the projection, which
                # parses the EPUB and matches the whole book: other writers must not wait.
                ensure_pdf_sentences(db, library, work)
                db.commit()
                refresh_projection(db, library, work, book, row)
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
                log.exception("updating the sentences of file %d failed", file_id)
    return read


def status_json(row: PdfText | None, book: BookFile) -> dict:
    """What the app shows about a PDF's text (``GET /api/files/{id}/text`` and the work)."""
    if row is None or row.pdf_sha256 != book.sha256 or row.version != DOCUMENT_VERSION:
        return {"status": PENDING, "pages": None, "pages_with_text": None, "sections": [],
                "sentences": 0, "error": None, "source": None, "ocr": None}  # fmt: skip
    status = row.status
    if status == FAILED and row.attempts < MAX_ATTEMPTS:
        status = PENDING  # retried on the next sweep
    ocr = (row.stats or {}).get("ocr") if row.ocr_sha256 else None
    return {
        "status": status,
        "pages": row.pages,
        "pages_with_text": row.pages_with_text,
        "sections": row.sections or [],
        "sentences": row.sentence_count,
        "error": row.error if status == FAILED else None,
        # where the text came from: the PDF's text layer, or OCR ({"engine", "language"})
        "source": "ocr" if row.ocr_sha256 else "text_layer",
        "ocr": ocr,
    }
