"""Full-text search inside books: an SQLite FTS5 index, kept in step with the library.

Where it lives
    ``<data>/search.db``, a file of its own. It is derived data: backups leave it out, and
    deleting it (server stopped) rebuilds it from the books. Writing it never takes the main
    database's write lock, so indexing a long book does not hold up anyone else.

What is indexed
    A work's text, in chunks of about :data:`CHUNK_CHARS` characters cut at sentence ends
    (:func:`lectern.ingest.sentences.segment`): the primary EPUB (the one the reader opens,
    spine item by spine item, on the frozen text timeline of
    :func:`lectern.positions.normalize_spine_text`) and every PDF's canonical text document,
    section by section, once :mod:`lectern.pdftext.store` has read it (``pdf_texts.status``
    ``ready``; a scan joins once OCR has written its document). The indexer never opens a PDF
    itself. At most
    ``LECTERN_SEARCH_MAX_CHARS_PER_WORK`` characters per work. Comics have no text.

    A chunk's location is ``(spine_index, char)`` for an EPUB, which the web reader opens
    directly (``lib/textpos.ts``), or a page number for a PDF (each PDF chunk keeps where its
    pages start, from the document's words). EPUB text is indexed with a
    space between block elements even where the timeline has none (:func:`spine_text`); each
    chunk lists those spaces (``gaps``) so a match maps back to the exact timeline offset.

Size
    ``chunks`` holds each chunk zlib-compressed; the FTS5 table is an external-content index
    over the ``chunk_text`` view, which inflates a chunk only when a snippet or a delete needs
    it. On real books the file is about the size of the plain text (docs/search.md), where a
    plain FTS5 table would be well over twice that.

Keeping up (:func:`reconcile`)
    The indexer thread compares, every ``LECTERN_SEARCH_POLL_SECONDS``, each live work's
    *source key* (the files it would index and their SHA-256, and :data:`INDEX_VERSION`) with
    the key it indexed. A new or changed key is (re)indexed in one transaction, the old chunks
    replaced; a trashed, deleted, or textless work's chunks are removed. Running it again
    changes nothing: that is what makes reindexing idempotent, and also what picks up a book
    added, edited, trashed, or restored by any route (upload, inbox, editor, merge, restore
    from backup) without each of them having to tell the index.

Who sees what (:func:`search`)
    Every query is limited to the ids of :func:`lectern.access.visible_works`, the library's
    own rule, so trashed works and works a reader cannot see never come back, and nothing is
    counted: the response is only the visible hits. The full-text match reads the words'
    matches across the whole index for everyone, then keeps the visible ones; what the time a
    query takes can tell a reader is at most how common a word is in the whole library (and
    only for words in thousands of passages), never which book has it or what it says.
    Docs: docs/search.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import unicodedata
import zipfile
import zlib
from bisect import bisect_left, bisect_right
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from lectern.access import visible_works
from lectern.config import Settings
from lectern.models import BookFile, FileKind, User, Work

log = logging.getLogger("lectern.search")

# search.db's layout. Another value in its meta table (an older or newer Lectern) rebuilds it.
SCHEMA_VERSION = 1
# Text extraction and chunking; part of every source key, so changing either reindexes all.
INDEX_VERSION = 1
CHUNK_CHARS = 800
MAX_QUERY_CHARS = 200
MAX_TERMS = 12
MAX_RESULTS = 20
PER_WORK = 3
PREFIX_MIN = 4
# Characters of context kept before and after the first match in a snippet.
SNIPPET_BEFORE = 60
SNIPPET_AFTER = 140
# Indexing work per reconcile pass before the next look at what changed (a trash meanwhile
# is removed from the index at the next pass, not after the whole first build).
PASS_BUDGET_S = 30.0

EPUB_KIND, PDF_KIND = 0, 1
_KIND_NAME = {EPUB_KIND: "epub", PDF_KIND: "pdf"}
_OPEN, _CLOSE = "\x02", "\x03"
_TERM = re.compile(r"[^\W_]+")

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    work_id INTEGER NOT NULL,
    file_id INTEGER NOT NULL,
    kind INTEGER NOT NULL,       -- 0 EPUB, 1 PDF
    loc INTEGER NOT NULL,        -- EPUB spine index, or PDF document section
    char_start INTEGER NOT NULL, -- offset of the chunk in that spine item's / section's text
    z BLOB NOT NULL,             -- zlib-compressed UTF-8 text
    gaps TEXT,                   -- EPUB: spaces the timeline lacks ("12,40": spine_text)
    pages TEXT                   -- PDF: 1-based page from each offset on ("0:3,412:4")
);
CREATE INDEX chunks_work ON chunks (work_id);
CREATE VIEW chunk_text AS SELECT id, lectern_inflate(z) AS body FROM chunks;
CREATE VIRTUAL TABLE fts USING fts5(
    body, content='chunk_text', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE indexed (
    work_id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,        -- the source key this work's chunks were built from
    chunks INTEGER NOT NULL,
    chars INTEGER NOT NULL,
    error TEXT,                  -- a file that could not be read (it is skipped)
    indexed_at REAL NOT NULL
);
"""

_path: Path | None = None


class QueryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Transient(Exception):  # noqa: N818 - internal signal, not an error for callers
    """Try this work again later: a file mid-rename, every PDF parser slot busy."""


def _inflate(blob: bytes) -> str:
    return zlib.decompress(blob).decode("utf-8")


def connect(path: Path | None = None) -> sqlite3.Connection:
    """A connection to search.db with ``lectern_inflate`` registered (the ``chunk_text`` view,
    and so every snippet and delete, needs it). Autocommit; callers ``BEGIN`` themselves."""
    con = sqlite3.connect(
        str(path or _path), isolation_level=None, check_same_thread=False, timeout=5.0
    )
    con.create_function("lectern_inflate", 1, _inflate, deterministic=True)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init(settings: Settings) -> Path:
    """Open ``<data>/search.db``: create it, or rebuild it when it has another layout or is
    damaged (it is derived data; the indexer fills it again)."""
    global _path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.data_dir / "search.db"
    for attempt in range(2):
        try:
            con = connect(path)
            try:
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
                if not tables:
                    con.executescript(_SCHEMA)
                    con.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
                    break
                if "meta" in tables and con.execute(
                    "SELECT value FROM meta WHERE key = 'schema'"
                ).fetchone() == (str(SCHEMA_VERSION),):
                    break
            finally:
                con.close()
        except sqlite3.DatabaseError:
            pass
        if attempt:
            raise RuntimeError(f"could not create {path}")
        log.warning("rebuilding the search index (%s has another layout or is damaged)", path)
        for suffix in ("", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
    _path = path
    return path


# --- what each work should have indexed --------------------------------------------------------


@dataclass(frozen=True)
class Source:
    file_id: int
    kind: int
    sha256: str  # the file's
    rel_path: str
    text_sha256: str = ""  # a PDF's text document (lectern.pdftext)


@dataclass(frozen=True)
class Wanted:
    key: str
    dir_path: str
    sources: tuple[Source, ...]


def wanted(db: Session) -> dict[int, Wanted]:
    """Every live work with text: its primary EPUB (``works.primary_file_id`` when that is one
    of its EPUBs, else the first added, as :func:`lectern.ingest.sentences.primary_epub`) and
    its PDFs whose text document is ready and current. One query for the whole library."""
    from lectern.ingest.formats import EPUB, PDF
    from lectern.models import PdfText
    from lectern.pdftext.document import DOCUMENT_VERSION
    from lectern.pdftext.store import READY

    rows = db.execute(
        select(
            Work.id,
            Work.dir_path,
            Work.primary_file_id,
            BookFile.id,
            BookFile.media_type,
            BookFile.sha256,
            BookFile.rel_path,
            PdfText.text_sha256,
        )
        .join(BookFile, BookFile.work_id == Work.id)
        .outerjoin(
            PdfText,
            (PdfText.file_id == BookFile.id)
            & (PdfText.status == READY)
            & (PdfText.pdf_sha256 == BookFile.sha256)
            & (PdfText.version == DOCUMENT_VERSION),
        )
        .where(
            Work.trashed_at.is_(None),
            BookFile.kind == FileKind.EBOOK,
            BookFile.media_type.in_([EPUB, PDF]),
        )
        .order_by(Work.id, BookFile.added_at, BookFile.id)
    ).all()
    by_work: dict[int, list] = {}
    for r in rows:
        by_work.setdefault(r[0], []).append(r)
    out: dict[int, Wanted] = {}
    for work_id, files in by_work.items():
        epubs = [f for f in files if f[4] == EPUB]
        primary = next((f for f in epubs if f[3] == f[2]), epubs[0] if epubs else None)
        sources = [Source(primary[3], EPUB_KIND, primary[5], primary[6])] if primary else []
        sources += [Source(f[3], PDF_KIND, f[5], f[6], f[7]) for f in files if f[4] == PDF and f[7]]
        if not sources:
            continue
        parts = ";".join(f"{s.file_id}:{s.kind}:{s.sha256}:{s.text_sha256}" for s in sources)
        key = hashlib.sha256(f"{INDEX_VERSION}|{parts}".encode()).hexdigest()
        out[work_id] = Wanted(key, files[0][1], tuple(sources))
    return out


# --- text extraction --------------------------------------------------------------------------


def chunks_of(text: str) -> Iterator[tuple[int, str]]:
    """``(char_start, chunk)`` pieces of normalised ``text``: whole sentences, about
    :data:`CHUNK_CHARS` each (a longer sentence is its own chunk)."""
    from lectern.ingest.sentences import segment

    start = end = None
    for s, e in segment(text):
        if start is None:
            start, end = s, e
        elif e - start > CHUNK_CHARS:
            yield start, text[start:end]
            start, end = s, e
        else:
            end = e
    if start is not None:
        yield start, text[start:end]


_BLOCK = "\x00"  # never in parsed XML text


def spine_text(xhtml: bytes) -> tuple[str, list[int]]:
    """A spine item's text for the index, and where it differs from the reader's timeline.

    The timeline (:func:`lectern.positions.normalize_spine_text`, frozen) joins block elements
    without a space when the markup has none between them (``…spleen.</p><p>Whenever…``
    reads ``spleen.Whenever``), which would make one word of the last word of a paragraph and
    the first of the next. The index text has a space there instead; the second value lists
    those inserted spaces (positions in the index text), and removing them gives exactly the
    timeline text, so every match maps back to a reader location."""
    from lxml import etree

    from lectern.textmap import BLOCK_TAGS

    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=False)
    try:
        root = etree.fromstring(xhtml, parser)
    except (etree.LxmlError, ValueError):
        return "", []
    if root is None:
        return "", []
    body = root.find(".//{http://www.w3.org/1999/xhtml}body")
    if body is None:
        body = root.find(".//body")
    if body is None:
        body = root
    raw: list[str] = []

    def collect(node) -> None:  # positions._collect_text, with block boundaries marked
        if not isinstance(node.tag, str):
            return
        name = node.tag.rsplit("}", 1)[-1]
        if name in ("script", "style"):
            return
        block = name in BLOCK_TAGS
        if block:
            raw.append(_BLOCK)
        if node.text:
            raw.append(node.text)
        for child in node:
            collect(child)
            if child.tail:
                raw.append(child.tail)
        if block:
            raw.append(_BLOCK)

    collect(body)
    out: list[str] = []
    inserted: list[int] = []
    pos = 0
    for word in "".join(raw).split():
        pieces = [p for p in word.split(_BLOCK) if p]
        if not pieces:
            continue
        if out:
            out.append(" ")
            pos += 1
        for k, piece in enumerate(pieces):
            if k:
                out.append(" ")
                inserted.append(pos)
                pos += 1
            out.append(piece)
            pos += len(piece)
    return "".join(out), inserted


def _epub_texts(path: Path) -> Iterator[tuple[int, str, list[int], list[tuple[int, int]]]]:
    from lectern.ingest.epub import read_spine
    from lectern.ingest.sentences import MAX_SPINE_ITEM_BYTES

    spine = read_spine(path, strict=False)
    with zipfile.ZipFile(path) as zf:
        for spine_index, name in enumerate(spine):
            try:
                info = zf.getinfo(name)
            except KeyError:
                continue
            if info.file_size > MAX_SPINE_ITEM_BYTES:
                continue
            text, inserted = spine_text(zf.read(name))
            if text:
                yield spine_index, text, inserted, []


def _pdf_texts(path: Path) -> Iterator[tuple[int, str, list[int], list[tuple[int, int]]]]:
    """A PDF's text document (:mod:`lectern.pdftext.document`), section by section, with the
    offsets where a new (1-based) page starts, from its words."""
    doc = json.loads(path.read_bytes())
    for k, section in enumerate(doc.get("sections") or []):
        text = str(section.get("text") or "")
        if not text:
            continue
        marks: list[tuple[int, int]] = []
        for word in section.get("words") or []:
            page = int(word[2]) + 1
            if not marks or marks[-1][1] != page:
                marks.append((0 if not marks else int(word[0]), page))
        yield k, text, [], marks or [(0, int(section.get("page_start") or 0) + 1)]


def _chunk_pages(marks: list[tuple[int, int]], start: int, end: int) -> str | None:
    """``marks`` as they apply to the chunk ``[start, end)``, offsets relative to it."""
    if not marks:
        return None
    i = max(0, bisect_right([m[0] for m in marks], start) - 1)
    out = [(0, marks[i][1])]
    out += [(off - start, page) for off, page in marks[i + 1 :] if off < end]
    return ",".join(f"{off}:{page}" for off, page in out)


def _page_at(pages: str | None, offset: int) -> int | None:
    page = None
    for item in (pages or "").split(","):
        if not item:
            continue
        off, _, p = item.partition(":")
        if int(off) > offset:
            break
        page = int(p)
    return page


# --- the index --------------------------------------------------------------------------------


def _delete_work(con: sqlite3.Connection, work_id: int) -> None:
    con.execute(
        "INSERT INTO fts (fts, rowid, body) "
        "SELECT 'delete', id, lectern_inflate(z) FROM chunks WHERE work_id = ?",
        (work_id,),
    )
    con.execute("DELETE FROM chunks WHERE work_id = ?", (work_id,))
    con.execute("DELETE FROM indexed WHERE work_id = ?", (work_id,))


def remove_work(con: sqlite3.Connection, work_id: int) -> None:
    con.execute("BEGIN IMMEDIATE")
    try:
        _delete_work(con, work_id)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def index_work(con: sqlite3.Connection, settings: Settings, work_id: int, want: Wanted) -> dict:
    """Replace ``work_id``'s chunks with its files' text now, in one transaction.

    A file that cannot be read (not a zip, a damaged document) adds nothing and is noted in
    ``indexed.error``; a missing file (a folder being renamed, a PDF's document not written
    again yet after a restore) raises :class:`_Transient` and leaves the old chunks in place."""
    from lectern.ingest.epub import EpubError
    from lectern.pdftext.store import document_rel

    folder = settings.library_dir / want.dir_path
    errors: list[str] = []
    budget = settings.search_max_chars_per_work
    paths = {
        s.file_id: folder / (s.rel_path if s.kind == EPUB_KIND else document_rel(s.sha256))
        for s in want.sources
    }
    for s in want.sources:
        if not paths[s.file_id].is_file():
            raise _Transient(f"{paths[s.file_id].name} is missing")
    n = chars = 0
    con.execute("BEGIN IMMEDIATE")
    try:
        _delete_work(con, work_id)
        for s in want.sources:
            if chars >= budget:
                break
            path = paths[s.file_id]
            texts = _epub_texts(path) if s.kind == EPUB_KIND else _pdf_texts(path)
            try:
                for loc, text, inserted, marks in texts:
                    for start, chunk in chunks_of(text[: budget - chars]):
                        lo = bisect_left(inserted, start)
                        hi = bisect_left(inserted, start + len(chunk), lo)
                        gaps = ",".join(str(i - start) for i in inserted[lo:hi]) or None
                        pages = _chunk_pages(marks, start, start + len(chunk))
                        cur = con.execute(
                            "INSERT INTO chunks (work_id, file_id, kind, loc, char_start, z, gaps, pages) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (work_id, s.file_id, s.kind, loc, start - lo,
                             zlib.compress(chunk.encode("utf-8"), 6), gaps, pages),
                        )  # fmt: skip
                        con.execute(
                            "INSERT INTO fts (rowid, body) VALUES (?, ?)",
                            (cur.lastrowid, chunk),
                        )
                        n += 1
                    chars += min(len(text), budget - chars)
                    if chars >= budget:
                        break
            except FileNotFoundError:
                raise _Transient(f"{s.rel_path} is missing") from None
            except (EpubError, zipfile.BadZipFile, zlib.error, ValueError, OSError) as exc:
                errors.append(f"{s.rel_path}: {type(exc).__name__}")
        con.execute(
            "INSERT INTO indexed (work_id, source, chunks, chars, error, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (work_id, want.key, n, chars, "; ".join(errors)[:1000] or None, time.time()),
        )
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return {"chunks": n, "chars": chars, "errors": errors}


def reconcile(
    session_factory: sessionmaker,
    settings: Settings,
    *,
    stop: threading.Event | None = None,
    budget_s: float | None = PASS_BUDGET_S,
    retry: dict[int, tuple[float, int]] | None = None,
) -> dict:
    """One pass: remove what should not be indexed, then (re)index changed works, newest
    first, for up to ``budget_s`` seconds (``None``: all of them). Returns what it did and
    how many works still wait. ``retry`` (work id -> (monotonic time, failures)) backs off
    works that failed for a passing reason; the indexer thread keeps it between passes."""
    with session_factory() as db:
        want = wanted(db)
    retry = {} if retry is None else retry
    con = connect()
    try:
        have = dict(con.execute("SELECT work_id, source FROM indexed").fetchall())
        removed = 0
        for work_id in set(have) - set(want):
            remove_work(con, work_id)
            removed += 1
        now = time.monotonic()
        todo = sorted(
            (w for w, x in want.items() if have.get(w) != x.key and retry.get(w, (0,))[0] <= now),
            reverse=True,
        )
        started = time.monotonic()
        indexed = 0
        for work_id in todo:
            if stop is not None and stop.is_set():
                break
            if budget_s is not None and time.monotonic() - started > budget_s:
                break
            try:
                index_work(con, settings, work_id, want[work_id])
                retry.pop(work_id, None)
                indexed += 1
            except _Transient as exc:
                tries = retry.get(work_id, (0, 0))[1] + 1
                retry[work_id] = (time.monotonic() + min(3600.0, 30.0 * 2 ** (tries - 1)), tries)
                log.info("search index: work %d later (%s)", work_id, exc)
            except Exception:  # noqa: BLE001 - one bad book must not stop the rest
                retry[work_id] = (time.monotonic() + 3600.0, retry.get(work_id, (0, 0))[1] + 1)
                log.exception("search index: work %d failed", work_id)
        return {"removed": removed, "indexed": indexed, "pending": len(todo) - indexed}
    finally:
        con.close()


class SearchIndexer:
    """The background thread that keeps search.db in step (:func:`reconcile`)."""

    def __init__(self, session_factory: sessionmaker, settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self._stop = threading.Event()
        self._retry: dict[int, tuple[float, int]] = {}
        self._thread = threading.Thread(target=self._run, name="lectern-search", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def _run(self) -> None:
        while not self._stop.is_set():
            pending = 0
            try:
                done = reconcile(
                    self.session_factory, self.settings, stop=self._stop, retry=self._retry
                )
                pending = done["pending"]
                if done["indexed"] or done["removed"]:
                    log.info(
                        "search index: %d indexed, %d removed, %d waiting",
                        done["indexed"],
                        done["removed"],
                        pending,
                    )
            except Exception:  # noqa: BLE001
                log.exception("search index pass failed")
            self._stop.wait(0.05 if pending else self.settings.search_poll_seconds)


# --- searching --------------------------------------------------------------------------------


def query_terms(q: str) -> list[str]:
    """The words of a query, as the index tokenises them. Raises :class:`QueryError`."""
    if len(q) > MAX_QUERY_CHARS:
        raise QueryError("query_too_long", f"Searches are at most {MAX_QUERY_CHARS} characters.")
    # NFC first: a decomposed accent (e + U+0300, as macOS input can send) is a word
    # boundary to the pattern and would split "père" into "pe" and "re".
    terms = _TERM.findall(unicodedata.normalize("NFC", q))
    if len(terms) > MAX_TERMS:
        raise QueryError("too_many_words", f"Searches are at most {MAX_TERMS} words.")
    return terms


def fts_queries(terms: list[str], *, prefix: bool) -> list[str]:
    """The phrase (the words in order, together), then every word anywhere in the chunk.
    Each word is a quoted FTS5 string, so nothing in a query is FTS syntax; the last one
    matches as a prefix while it is still being typed, from :data:`PREFIX_MIN` letters (a
    shorter prefix, ``the*``, expands to so many words that it reads much of the index)."""
    if not terms:
        return []
    star = " *" if prefix and len(terms[-1]) >= PREFIX_MIN else ""
    phrase = '"' + " ".join(terms) + '"' + star
    if len(terms) == 1:
        return [phrase]
    return [phrase, " ".join(f'"{t}"' for t in terms) + star]


_RANKED = """
SELECT id, work_id, file_id, kind, loc, char_start, gaps, pages FROM (
    SELECT c.id, c.work_id, c.file_id, c.kind, c.loc, c.char_start, c.gaps, c.pages,
           fts.rank AS score,
           row_number() OVER (PARTITION BY c.work_id ORDER BY fts.rank) AS nth
    FROM fts JOIN chunks AS c ON c.id = fts.rowid
    WHERE fts MATCH ?1 AND c.work_id IN (SELECT value FROM json_each(?2))
) WHERE nth <= ?3 ORDER BY score LIMIT ?4
"""


def search(
    db: Session, user: User, settings: Settings, q: str, *, limit: int = 8, prefix: bool = True
) -> dict:
    """Passages matching ``q`` in works ``user`` may see: ``{"results": [...], "too_broad"}``.

    Each result names its work and where the first match is (``spine_index`` and ``char``
    for an EPUB, ``page`` for a PDF), with a short snippet in parts (``[text, is_match]``).
    """
    terms = query_terms(q)
    limit = max(1, min(limit, MAX_RESULTS))
    if _path is None or not terms or max(len(t) for t in terms) < 2:
        return {"results": [], "too_broad": False}
    visible = json.dumps(list(db.scalars(select(Work.id).where(visible_works(user)))))
    hits: list[tuple] = []  # (query, row)
    seen: set[int] = set()
    per_work: dict[int, int] = {}
    too_broad = False
    con = connect()
    try:
        deadline = time.monotonic() + settings.search_time_budget_ms / 1000
        con.set_progress_handler(lambda: int(time.monotonic() > deadline), 100)
        for fq in fts_queries(terms, prefix=prefix):
            if len(hits) >= limit:
                break
            try:
                rows = con.execute(_RANKED, (fq, visible, PER_WORK, limit)).fetchall()
            except sqlite3.OperationalError as exc:
                if "interrupt" not in str(exc):
                    raise
                too_broad = True
                break
            for row in rows:
                if row[0] in seen or per_work.get(row[1], 0) >= PER_WORK or len(hits) >= limit:
                    continue
                seen.add(row[0])
                per_work[row[1]] = per_work.get(row[1], 0) + 1
                hits.append((fq, row))
        con.set_progress_handler(None, 0)
        marked: dict[int, str] = {}
        for fq in {fq for fq, _ in hits}:
            ids = [row[0] for f, row in hits if f == fq]
            marked.update(
                con.execute(
                    f"SELECT rowid, highlight(fts, 0, char(2), char(3)) FROM fts "  # noqa: S608 - placeholders only
                    f"WHERE fts MATCH ? AND rowid IN ({','.join('?' * len(ids))})",
                    (fq, *ids),
                ).fetchall()
            )
    finally:
        con.close()
    if not hits:
        return {"results": [], "too_broad": too_broad}
    work_ids = {row[1] for _, row in hits}
    works = {
        w.id: w for w in db.scalars(select(Work).where(Work.id.in_(work_ids), visible_works(user)))
    }
    current = set(
        db.execute(
            select(BookFile.id, BookFile.work_id).where(
                BookFile.id.in_({row[2] for _, row in hits})
            )
        ).all()
    )
    results = []
    for _, (chunk_id, work_id, file_id, kind, loc, char_start, gaps, pages) in hits:
        work = works.get(work_id)
        text = marked.get(chunk_id)
        if work is None or (file_id, work_id) not in current or text is None:
            continue  # trashed or replaced since it was indexed
        snip = snippet(text)
        if snip is None:
            continue
        item = {
            "work_id": work_id,
            "title": work.title,
            "author": work.author,
            "format": _KIND_NAME[kind],
            "file_id": file_id,
            "match": snip["match"],
            "snippet": {"parts": snip["parts"], "before": snip["before"], "after": snip["after"]},
        }
        if kind == EPUB_KIND:
            # back to the reader's timeline: the spaces only the index has do not count
            inserted = [int(g) for g in gaps.split(",")] if gaps else []
            a, b = snip["start"], snip["start"] + snip["length"]
            a -= bisect_left(inserted, a)
            b -= bisect_left(inserted, b)
            item |= {"spine_index": loc, "char": char_start + a, "length": b - a}
        else:
            item["page"] = _page_at(pages, snip["start"])
        results.append(item)
    return {"results": results, "too_broad": too_broad}


def snippet(marked: str) -> dict | None:
    """A short window of ``marked`` (``highlight()`` output, matches between ``\\x02`` and
    ``\\x03``) around its first match: ``parts`` ``[[text, is_match], …]``, whether text was
    cut ``before`` and ``after``, and the first match's ``start``/``length`` in the plain text."""
    parts: list[list] = []
    inside = False
    for piece in re.split(f"([{_OPEN}{_CLOSE}])", marked):
        if piece == _OPEN:
            inside = True
        elif piece == _CLOSE:
            inside = False
        elif piece:
            parts.append([piece, inside])
    plain = "".join(p[0] for p in parts)
    pos = 0
    first = None
    for text, is_match in parts:
        if is_match:
            first = (pos, pos + len(text))
            break
        pos += len(text)
    if first is None:
        return None
    lo = max(0, first[0] - SNIPPET_BEFORE)
    if lo > 0:
        space = plain.find(" ", lo, first[0])
        lo = space + 1 if space != -1 else lo
    hi = min(len(plain), first[1] + SNIPPET_AFTER)
    if hi < len(plain):
        space = plain.rfind(" ", first[1], hi)
        hi = space if space != -1 else hi
    out: list[list] = []
    pos = 0
    for text, is_match in parts:
        a, b = max(lo, pos), min(hi, pos + len(text))
        if a < b:
            out.append([text[a - pos : b - pos], is_match])
        pos += len(text)
    return {
        "parts": out,
        "before": lo > 0,
        "after": hi < len(plain),
        "start": first[0],
        "length": first[1] - first[0],
        "match": plain[first[0] : first[1]],
    }
