"""KOReader highlights and notes (issue #65, docs/koreader-plugin.md#highlights-and-notes).

Pull
    The plugin sends, per document, the device's current list of highlights for it (a
    snapshot), with the device's sync key (``PUT /koplugin/annotations``, kosync auth). Each
    annotation's stable id (:func:`annotation_key`) is a digest of the document, KOReader's
    creation time, and the start position, so the same highlight synced twice, or from two
    devices that share a book's sidecar, is one row. A newer ``datetime_updated`` (a note edited
    on the device) updates the row. ``sources`` remembers which devices' latest snapshot has
    the highlight: one missing from a device's snapshot loses that device, and the row goes
    when no live (unrevoked) device has it any more (deleted on the device). A device never
    deletes a highlight it did not hold.

Place
    A highlight is placed on the work's text as the web reader opens it, the primary EPUB
    (:func:`locate`): by matching its text against the book's normalised text, ignoring
    whitespace, case, and compatibility forms, nearest to where the device says it was (the
    exact xpointer for the same EPUB, the spine item for another EPUB, the page share for a
    PDF). So a highlight from the EPUB or the PDF of a book lands on the same passage. When the work has a sentence list, the sentence is stored too. A placement is
    redone when the primary EPUB changes.

Search
    ``annotations_fts`` (migration 0026) is an SQLite FTS5 index over each highlight's text and
    note, folded for case and accents like the search inside books (:mod:`lectern.search`).
    Triggers keep it in step with every create, edit, and delete in the same transaction;
    :func:`reindex` rebuilds it. :func:`find` matches the caller's own rows only, then keeps
    those :func:`visible` still shows them.

Privacy
    Highlights are private-source data: only their owner sees them, only for works they can
    still see (:func:`lectern.access.visible_works`), never other household members, OPDS, or
    MCP. Nothing here logs their text or notes.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from array import array
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sqlalchemy import column, func, select
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from lectern import search
from lectern.access import visible_works
from lectern.models import Annotation, BookFile, FileKind, SyncKey, User, Work, utcnow
from lectern.positions import TextPos, xpointer_spine_index, xpointer_to_text

MAX_PER_DOCUMENT = 5000
MAX_TEXT = 20000
_IGNORED = {"\u00ad", "\u200b", "\u200c", "\u200d", "\ufeff"}  # soft hyphen, zero widths
_PREFIX = 64  # key characters matched when the whole text is not found
_PDF_POS = re.compile(r"^(\d+)(?::|$)")


def annotation_key(document: str, created: str, pos0: str) -> str:
    return hashlib.sha256(f"{document}\n{created}\n{pos0}".encode()).hexdigest()[:32]


# --- placing a highlight on the work's text -----------------------------------------------------


def _fold(text: str, base: int = 0, out: list[str] | None = None, idx: array | None = None):
    """``text`` without whitespace, NFKC-folded and case-folded, and for each kept character
    the offset in ``text`` (plus ``base``) it came from."""
    out = [] if out is None else out
    idx = array("l") if idx is None else idx
    for i, ch in enumerate(text):
        if ch < "\x80":
            if not ch.isspace():
                out.append(ch.lower())
                idx.append(base + i)
            continue
        if ch.isspace() or ch in _IGNORED:
            continue
        for c in unicodedata.normalize("NFKC", ch).casefold():
            if not c.isspace():
                out.append(c)
                idx.append(base + i)
    return out, idx


@dataclass(frozen=True)
class BookText:
    """The primary EPUB's normalised text, folded for matching."""

    starts: list[int]  # global offset where each spine item's text starts
    lengths: list[int]
    folded: str
    origin: array  # global offset of each folded character

    def to_text(self, glob: int) -> TextPos:
        i = max(0, bisect_right(self.starts, glob) - 1)
        return TextPos(i, glob - self.starts[i])

    def to_global(self, pos: TextPos) -> int:
        return self.starts[pos.spine_index] + pos.char

    @property
    def total(self) -> int:
        return self.starts[-1] + self.lengths[-1] if self.starts else 0


@lru_cache(maxsize=2)
def book_text(path: str, sha256: str) -> BookText:  # sha256 keys the cache
    from lectern.align.text import iter_spine_items

    starts, lengths, out, idx, total = [], [], [], array("l"), 0
    for item in iter_spine_items(Path(path)):
        text = item.text.text
        starts.append(total)
        lengths.append(len(text))
        _fold(text, total, out, idx)
        total += len(text)
    return BookText(starts, lengths, "".join(out), idx)


@dataclass(frozen=True)
class Place:
    spine: int
    char: int
    length: int


def _match(book: BookText, text: str, hint: int | None) -> Place | None:
    """Where ``text`` is in the book, the occurrence nearest the global offset ``hint``."""
    key = "".join(_fold(text)[0])
    if not key:
        return None
    for needle, whole in ((key, True), (key[:_PREFIX], False)):
        if not whole and len(key) <= _PREFIX:
            break
        hits, at = [], book.folded.find(needle)
        while at != -1 and len(hits) < 500:
            hits.append(at)
            at = book.folded.find(needle, at + 1)
        if not hits:
            continue
        best = min(hits, key=lambda h: abs(book.origin[h] - hint)) if hint is not None else hits[0]
        start = book.origin[best]
        end_key = best + len(key) - 1
        end = book.origin[end_key] + 1 if whole else start + len(text.strip())
        pos = book.to_text(start)
        # One spine item: the reader marks a range inside one section.
        length = max(1, min(end - start, book.lengths[pos.spine_index] - pos.char))
        return Place(pos.spine_index, pos.char, length)
    return None


def _hint(book: BookText, epub: Path, same: bool, a: Annotation, pages: int | None) -> int | None:
    spine = xpointer_spine_index(a.pos0)
    if spine is not None and 0 <= spine < len(book.starts):
        if same:
            from lectern.align.text import read_spine_item

            try:
                return book.to_global(xpointer_to_text(a.pos0, read_spine_item(epub, spine)))
            except Exception:  # noqa: BLE001, S110 - a damaged item: the spine item is hint enough
                pass
        return book.starts[spine]
    if a.page and pages:
        return int(book.total * min(1.0, (a.page - 1) / pages))
    return None


def _source_book(db: Session, document: str) -> BookFile | None:
    return db.scalar(
        select(BookFile)
        .where(BookFile.partial_md5 == document, BookFile.kind == FileKind.EBOOK)
        .order_by(BookFile.id)
        .limit(1)
    )


def locate(db: Session, library: Path, rows: list[Annotation]) -> None:
    """Place ``rows`` (all of one document) on their work's text; see the module docstring.
    Sets ``loc_*`` and ``sentence_n``; caller commits."""
    from lectern.align.tracks import work_epub
    from lectern.progress import sentence_index

    if not rows:
        return
    source = _source_book(db, rows[0].document)
    work = db.get(Work, source.work_id) if source else None
    found = work_epub(db, library, work) if work else None
    if found is None:
        for a in rows:
            a.loc_sha, a.loc_spine, a.loc_char, a.loc_len, a.sentence_n = (
                None,
                None,
                None,
                None,
                None,
            )
        return
    epub_file, path = found
    try:
        book = book_text(str(path), epub_file.sha256)
    except Exception:  # noqa: BLE001 - an unreadable EPUB leaves highlights unplaced
        book = None
    try:
        idx = sentence_index(db, work.id)
    except ValueError:
        idx = None
    same = source.id == epub_file.id
    for a in rows:
        place = None
        if book is not None and book.starts:
            place = _match(book, a.text, _hint(book, path, same, a, source.pages))
        a.loc_sha = epub_file.sha256
        if place is None:
            a.loc_spine, a.loc_char, a.loc_len, a.sentence_n = None, None, None, None
            continue
        a.loc_spine, a.loc_char, a.loc_len = place.spine, place.char, place.length
        sp = idx.at_text(TextPos(place.spine, place.char)) if idx is not None and len(idx) else None
        a.sentence_n = sp.n if sp else None


# --- sync from a device --------------------------------------------------------------------------


@dataclass
class Incoming:
    created: str
    updated: str
    text: str
    note: str | None
    chapter: str | None
    color: str | None
    drawer: str | None
    pos0: str
    pos1: str | None
    page: int | None
    pageno: int | None


def _page_of(pos0: str, page: int | None) -> int | None:
    if page is not None:
        return page
    m = _PDF_POS.match(pos0)
    return int(m.group(1)) if m else None


def sync_document(
    db: Session,
    library: Path,
    user: User,
    sync_key_id: int,
    document: str,
    device: str,
    items: list[Incoming],
) -> dict:
    """Apply one device's snapshot of ``document``'s highlights. Caller commits.

    ``sources`` only ever counts the user's live sync keys. A highlight missing from this
    snapshot is deleted only if this device held it and no other live device still does; one
    it never held (a revoked device's, another device's) is kept. A re-paired plugin sends
    every book again, so a replacement device holds what it has before it deletes anything."""
    live = set(
        db.scalars(
            select(SyncKey.id).where(SyncKey.user_id == user.id, SyncKey.revoked_at.is_(None))
        )
    )
    existing = {
        a.key: a
        for a in db.scalars(
            select(Annotation).where(Annotation.user_id == user.id, Annotation.document == document)
        )
    }
    seen: set[str] = set()
    changed: list[Annotation] = []
    added = updated = 0
    for it in items:
        key = annotation_key(document, it.created, it.pos0)
        if key in seen:
            continue
        seen.add(key)
        row = existing.get(key)
        if row is None:
            row = Annotation(user_id=user.id, document=document, key=key, created=it.created,
                             pos0=it.pos0, sources=[])  # fmt: skip
            db.add(row)
            existing[key] = row
            added += 1
        elif it.updated < row.updated:
            # An older copy (a device that has not seen the edit): only note who has it.
            if sync_key_id not in row.sources:
                row.sources = [*row.sources, sync_key_id]
            continue
        else:
            same = (row.text, row.note, row.chapter, row.color, row.updated) == (
                it.text, it.note, it.chapter, it.color, it.updated,
            )  # fmt: skip
            if same and sync_key_id in row.sources:
                continue
            updated += 0 if same else 1
        text_changed = row.text != it.text
        row.text, row.note, row.chapter = it.text, it.note, it.chapter
        row.color, row.drawer, row.pos1 = it.color, it.drawer, it.pos1
        row.page, row.pageno = _page_of(it.pos0, it.page), it.pageno
        row.updated, row.device = it.updated, device
        if sync_key_id not in (row.sources or []):
            row.sources = [*(row.sources or []), sync_key_id]
        row.synced_at = utcnow()
        if text_changed:
            changed.append(row)
    deleted = 0
    for key, row in existing.items():
        held = [s for s in (row.sources or []) if s in live]
        if key not in seen and sync_key_id in held:
            # Deleted on this device: it lets go; the row goes when no live device holds it.
            held = [s for s in held if s != sync_key_id]
            if not held:
                db.delete(row)
                deleted += 1
                continue
        # A snapshot only deletes what this device held: a highlight it never had (one only a
        # revoked device synced, or another device's) stays, even with no live source left.
        if held != (row.sources or []):
            row.sources = held
    db.flush()
    locate(db, library, changed)
    return {"document": document, "added": added, "updated": updated, "deleted": deleted,
            "total": len(seen)}  # fmt: skip


# --- reading them back ---------------------------------------------------------------------------


def visible(
    db: Session, library: Path, user: User, work_id: int | None = None, ids: list[int] | None = None
):
    """``[(work, [(annotation, file), ...]), ...]``: the user's own highlights on works they can
    see, with the file each was made in, each work's in reading order, works by title; only
    those in ``ids`` when given. Placements made against another primary EPUB than the work's
    current one are redone (and flushed)."""
    q = (
        select(Annotation, Work, BookFile)
        .join(
            BookFile,
            (BookFile.partial_md5 == Annotation.document) & (BookFile.kind == FileKind.EBOOK),
        )
        .join(Work, Work.id == BookFile.work_id)
        .where(Annotation.user_id == user.id, visible_works(user))
        .order_by(BookFile.id)
    )
    if work_id is not None:
        q = q.where(Work.id == work_id)
    if ids is not None:
        listed = select(column("value")).select_from(func.json_each(json.dumps(ids)))
        q = q.where(Annotation.id.in_(listed))
    by_work: dict[int, tuple[Work, dict[int, tuple[Annotation, BookFile]]]] = {}
    for a, w, f in db.execute(q):
        by_work.setdefault(w.id, (w, {}))[1].setdefault(a.id, (a, f))
    out = []
    for w, rows in by_work.values():
        pairs = list(rows.values())
        _refresh(db, library, w, [a for a, _ in pairs])
        pairs.sort(key=lambda p: _order(p[0]))
        out.append((w, pairs))
    out.sort(key=lambda p: (p[0].title.casefold(), p[0].id))
    return out


def _refresh(db: Session, library: Path, work: Work, rows: list[Annotation]) -> None:
    from lectern.ingest.sentences import primary_epub

    epub = primary_epub(db, work.id)
    want = epub.sha256 if epub is not None else None
    stale: dict[str, list[Annotation]] = {}
    for a in rows:
        if a.loc_sha != want:
            stale.setdefault(a.document, []).append(a)
    for group in stale.values():
        locate(db, library, group)
    if stale:
        db.flush()


def _order(a: Annotation):
    if a.loc_spine is not None:
        return (0, a.loc_spine, a.loc_char or 0, a.created)
    return (1, a.page or a.pageno or 0, 0, a.created)


def as_json(a: Annotation, f: BookFile) -> dict:
    located = a.loc_spine is not None
    return {
        "id": a.id,
        "key": a.key,
        "text": a.text,
        "note": a.note,
        "chapter": a.chapter,
        "color": a.color,
        "page": a.page,
        "pageno": a.pageno,
        "created": a.created,
        "updated": a.updated,
        "device": a.device,
        "file": {"id": f.id, "media_type": f.media_type},
        "location": (
            {"spine_index": a.loc_spine, "char": a.loc_char, "length": a.loc_len}
            if located
            else None
        ),
        "sentence_n": a.sentence_n,
    }


# --- searching (issue #72) ----------------------------------------------------------------------

# "note: word" searches notes only, "highlight: word" the highlighted text only.
_FIELD = re.compile(r"^\s*(highlight|note)\s*:\s*", re.IGNORECASE)
_COLUMN = {"highlight": "text", "note": "note"}
_OWN_MATCHES = (
    "SELECT rowid FROM annotations_fts WHERE annotations_fts MATCH :q "
    "AND rowid IN (SELECT id FROM annotations WHERE user_id = :user) ORDER BY rank"
)


@dataclass(frozen=True)
class Filters:
    """The highlights page's filters. ``since``/``until``: ``YYYY-MM-DD``, inclusive, on
    KOReader's creation date."""

    work_id: int | None = None
    author: str | None = None
    tag: str | None = None
    since: str | None = None
    until: str | None = None

    def keep(self, work: Work, a: Annotation) -> bool:
        day = a.created[:10]
        return (
            (self.author is None or work.author == self.author)
            and (self.tag is None or self.tag in (work.tags or []))
            and (self.since is None or day >= self.since)
            and (self.until is None or day <= self.until)
        )


@dataclass
class Found:
    groups: list  # as visible(): [(work, [(annotation, file), ...])], filtered
    rank: dict[int, int] | None  # annotation id -> relevance order, when there was a query
    mark: str | None  # the FTS query every hit matches, to mark the words with

    def hits(self) -> list[tuple[Work, Annotation, BookFile]]:
        """Best first when there was a query, else in :func:`visible`'s order."""
        out = [(w, a, f) for w, pairs in self.groups for a, f in pairs]
        if self.rank is not None:
            out.sort(key=lambda h: self.rank[h[1].id])
        return out


def _match_queries(q: str, prefix: bool) -> list[str]:
    """The FTS queries for ``q``, as the search inside books builds them (phrase, then every
    word, the last word a prefix while typed), limited to one field after ``note:`` /
    ``highlight:``. Raises :class:`lectern.search.QueryError`."""
    m = _FIELD.match(q)
    terms = search.query_terms(q[m.end() :] if m else q)
    if not terms or max(len(t) for t in terms) < 2:
        return []
    out = search.fts_queries(terms, prefix=prefix)
    return [f"{{{_COLUMN[m.group(1).lower()]}}} : ({fq})" for fq in out] if m else out


def find(
    db: Session, library: Path, user: User, q: str, filters: Filters, *, prefix: bool = True
) -> Found:
    """The user's own highlights matching ``q`` (every one when blank) and ``filters``, on works
    they can see: the full-text match reads only their own rows, then :func:`visible` keeps
    what they may still see. Raises :class:`lectern.search.QueryError`."""
    q = q.strip()
    if not q:
        return Found(_filtered(db, library, user, filters, None), None, None)
    queries = _match_queries(q, prefix)
    rank: dict[int, int] = {}
    for fq in queries:
        for rowid in db.scalars(sql(_OWN_MATCHES), {"q": fq, "user": user.id}):
            rank.setdefault(rowid, len(rank))
    if not rank:
        return Found([], {}, None)
    # Every phrase hit also has every word, so the last (every-word) query marks them all.
    return Found(_filtered(db, library, user, filters, list(rank)), rank, queries[-1])


def _filtered(db, library, user, filters: Filters, ids: list[int] | None) -> list:
    out = []
    for w, pairs in visible(db, library, user, filters.work_id, ids):
        kept = [(a, f) for a, f in pairs if filters.keep(w, a)]
        if kept:
            out.append((w, kept))
    return out


def marks(db: Session, mark: str | None, ids: list[int]) -> dict[int, tuple[str, str | None]]:
    """``{id: (text, note)}`` with the words ``mark`` matches between ``\\x02`` and ``\\x03``
    (as :func:`lectern.search.snippet` reads them). Only ``ids``, which the caller found."""
    if not mark or not ids:
        return {}
    rows = db.execute(
        sql(
            "SELECT rowid, highlight(annotations_fts, 0, char(2), char(3)), "
            "highlight(annotations_fts, 1, char(2), char(3)) FROM annotations_fts "
            "WHERE annotations_fts MATCH :q AND rowid IN (SELECT value FROM json_each(:ids))"
        ),
        {"q": mark, "ids": json.dumps(ids)},
    )
    return {r[0]: (r[1], r[2]) for r in rows}


def parts(marked: str | None) -> list[list] | None:
    """``marked`` (from :func:`marks`) as ``[[text, is_match], …]``."""
    if marked is None:
        return None
    out, inside = [], False
    for piece in re.split("([\x02\x03])", marked):
        if piece in ("\x02", "\x03"):
            inside = piece == "\x02"
        elif piece:
            out.append([piece, inside])
    return out


def reindex(db: Session) -> int:
    """Rebuild ``annotations_fts`` from ``annotations`` (``lectern highlights-reindex``), and
    return how many highlights it holds. Caller commits."""
    db.execute(sql("INSERT INTO annotations_fts (annotations_fts) VALUES ('rebuild')"))
    return db.scalar(select(func.count()).select_from(Annotation))


# --- Markdown export -----------------------------------------------------------------------------


def where(a: Annotation) -> str:
    """ "Page 12 · 2026-10-01 09:30", as the web app shows it under each highlight."""
    page = a.page or a.pageno
    when = a.created[:16]
    return f"Page {page} · {when}" if page else when


_BLOCK_START = re.compile(r"^(\s*\d*)([#>*+\-.)])")


def _line(text: str) -> str:
    """A line of highlight or note text that Markdown keeps as text (no heading, list, quote)."""
    return _BLOCK_START.sub(lambda m: m.group(1) + "\\" + m.group(2), text)


def _quote(text: str) -> str:
    return "\n".join(
        "> " + _line(line.rstrip()) if line.strip() else ">" for line in text.strip().splitlines()
    )


def _work_md(work: Work, pairs, level: int) -> list[str]:
    h = "#" * level
    lines = [f"{h} {work.title}", ""]
    if work.author:
        lines += [f"*{work.author}*", ""]
    chapter = None
    for a, _ in pairs:
        if a.chapter and a.chapter != chapter:
            chapter = a.chapter
            lines += [f"{h}# {chapter}", ""]
        lines += [_quote(a.text), ""]
        if a.note:
            note = a.note.strip().splitlines()
            lines += [f"**Note:** {note[0]}", *map(_line, note[1:]), ""]
        lines += [f"*{where(a)}*", ""]
    return lines


def markdown(groups, *, title: str | None = None) -> str:
    """The export: one work (``title`` None) or all of them under ``title``."""
    if title is None:
        lines = _work_md(*groups[0], 1) if groups else []
    else:
        lines = [f"# {title}", ""]
        for work, pairs in groups:
            lines += _work_md(work, pairs, 2)
    return "\n".join(lines).rstrip() + "\n"
