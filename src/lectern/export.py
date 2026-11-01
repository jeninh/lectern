"""Full personal-data export (issue #139): one streamed zip of the signed-in person's own data.

What it holds: highlights and their notes, ratings, notes and read-throughs, reading progress,
reading-stats rollups, vocabulary, the person's shelves, and the want-to-read and listening
queues. Each table is written as JSON (one ``data.json``, schema-versioned) and as one CSV; the
highlights and book notes are also written as Markdown grouped by book.

Rules this module keeps:

- Per person: every query is filtered on the user's id, and every book on ``visible_works`` (what
  that person can see now, exactly as the web app shows it). Nobody else's rows are selected.
- No secrets: only the tables below are read, and only named columns. Nothing from users,
  sessions, sync keys, app passwords, AI keys, pairing codes, or device or worker credentials.
  Highlights' ``sources`` (sync key ids) are left out.
- Bounded memory: rows are fetched in batches of ``BATCH`` and written straight into a zip entry,
  and the output is handed to the response in chunks of about ``CHUNK`` bytes, so memory does not
  grow with the amount of data. The archive is never built in RAM or on disk.
- Reading stats are the per-day rollups (``reading_stats_days``), not raw ``reading_activity``,
  which is the same data the stats page shows and which stays complete if raw events are pruned
  by a retention job.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session, sessionmaker

from lectern.access import visible_works
from lectern.annotations import _line, _quote
from lectern.models import (
    Annotation,
    BookFile,
    BookReview,
    FileKind,
    ListeningQueueItem,
    ReadingState,
    ReadingStatsDay,
    ReadThrough,
    Role,
    Shelf,
    ShelfItem,
    User,
    VocabularyWord,
    WantToReadItem,
    Work,
)

SCHEMA_VERSION = 1
BATCH = 500  # rows fetched from SQLite at a time
CHUNK = 64 * 1024  # bytes handed to the response at a time

README = """# Lectern personal-data export

Your own data from this Lectern server, as of the time in `data.json`.

- `data.json`: everything, one object with a `schema_version` and a list of rows per table.
- `csv/<table>.csv`: the same rows, one file per table (UTF-8; a leading ' guards cells that
  would otherwise start a spreadsheet formula).
- `markdown/highlights.md`, `markdown/notes.md`: your highlights and your notes on books,
  grouped by book.

Only books you can see today are included. `reading_stats_days` holds your per-day reading
and listening totals (the rollups behind the stats page), so it stays complete even when
Lectern prunes its raw reading events. Passwords, sessions, sync keys, AI keys and device
secrets are never part of an export.
"""


def _iso(v: datetime) -> str:
    return v.astimezone(UTC).isoformat()


def _book() -> tuple:
    return (Work.id.label("work_id"), Work.title.label("title"), Work.author.label("author"))


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[str, ...]
    query: Callable[[User], Select]


def _annotations(user: User) -> Select:
    # One file per document: the first ebook with that digest (as lectern.annotations.visible).
    first = (
        select(func.min(BookFile.id))
        .where(BookFile.partial_md5 == Annotation.document, BookFile.kind == FileKind.EBOOK)
        .correlate(Annotation)
        .scalar_subquery()
    )
    a = Annotation
    return (
        select(
            a.id,
            *_book(),
            a.text,
            a.note,
            a.chapter,
            a.color,
            a.drawer,
            a.favourite,
            a.hidden,
            a.page,
            a.pageno,
            a.created,
            a.updated,
            a.device,
            a.pos0,
            a.pos1,
            a.loc_spine,
            a.loc_char,
            a.loc_len,
        )  # fmt: skip
        .select_from(a)
        .join(BookFile, BookFile.id == first)
        .join(Work, Work.id == BookFile.work_id)
        .where(a.user_id == user.id, visible_works(user))
        .order_by(Work.sort_title, Work.id, a.created, a.id)
    )


def _reviews(user: User) -> Select:
    r = BookReview
    return (
        select(*_book(), r.rating, (r.rating / 2.0).label("stars"), r.notes, r.updated_at)
        .select_from(r)
        .join(Work, Work.id == r.work_id)
        .where(r.user_id == user.id, visible_works(user))
        .order_by(Work.sort_title, Work.id)
    )


def _read_throughs(user: User) -> Select:
    r = ReadThrough
    return (
        select(r.id, *_book(), r.started_at, r.finished_at)
        .select_from(r)
        .join(Work, Work.id == r.work_id)
        .where(r.user_id == user.id, visible_works(user))
        .order_by(Work.sort_title, Work.id, r.id)
    )


def _reading_status(user: User) -> Select:
    r = ReadingState
    return (
        select(*_book(), r.fraction, r.last_read_at, r.started_at, r.finished_at)
        .select_from(r)
        .join(Work, Work.id == r.work_id)
        .where(r.user_id == user.id, visible_works(user))
        .order_by(Work.sort_title, Work.id)
    )


def _stats_days(user: User) -> Select:
    d = ReadingStatsDay
    return (
        select(d.day, *_book(), d.read_seconds, d.listened_seconds, d.pages)
        .select_from(d)
        .join(Work, Work.id == d.work_id)
        .where(d.user_id == user.id, visible_works(user))
        .order_by(d.day, Work.id)
    )


def _vocabulary(user: User) -> Select:
    # A word whose book is gone or no longer visible stays, without its book and context
    # (the same rule as lectern.vocabulary.as_json).
    v = VocabularyWord
    shown = Work.id.is_not(None)
    return (
        select(
            v.id,
            v.word,
            v.lemma,
            v.lang,
            v.to,
            v.definition,
            v.note,
            v.pos,
            v.dictionary,
            func.iif(shown, v.context, None).label("context"),
            *_book(),
            func.iif(shown, v.page, None).label("page"),
            func.iif(shown, v.spine_index, None).label("spine_index"),
            func.iif(shown, v.char, None).label("char"),
            func.iif(shown, v.length, None).label("length"),
            v.seen_count,
            v.created_at,
            v.last_seen_at,
        )  # fmt: skip
        .select_from(v)
        .outerjoin(Work, and_(Work.id == v.work_id, visible_works(user)))
        .where(v.user_id == user.id)
        .order_by(v.created_at, v.id)
    )


def _shelves(user: User) -> Select:
    # A person's own shelves; shelves shared to them belong to their owner and are not exported.
    return (
        select(Shelf.id, Shelf.parent_id, Shelf.name, Shelf.rule, Shelf.created_at)
        .where(Shelf.owner_id == user.id)
        .order_by(Shelf.id)
    )


def _shelf_items(user: User) -> Select:
    i = ShelfItem
    return (
        select(i.shelf_id, Shelf.name.label("shelf"), *_book(), i.position, i.added_at)
        .select_from(i)
        .join(Shelf, Shelf.id == i.shelf_id)
        .join(Work, Work.id == i.work_id)
        .where(Shelf.owner_id == user.id, visible_works(user))
        .order_by(i.shelf_id, i.position, Work.id)
    )


def _want_to_read(user: User) -> Select:
    w = WantToReadItem
    return (
        select(w.position, *_book(), w.created_at)
        .select_from(w)
        .join(Work, Work.id == w.work_id)
        .where(w.user_id == user.id, visible_works(user))
        .order_by(w.position, w.id)
    )


def _listening_queue(user: User) -> Select:
    q = ListeningQueueItem
    return (
        select(q.position, *_book(), q.track_id, q.chapter_start_s, q.chapter_end_s, q.created_at)
        .select_from(q)
        .join(Work, Work.id == q.work_id)
        .where(q.user_id == user.id, visible_works(user))
        .order_by(q.position, q.id)
    )


def _table(name: str, query: Callable[[User], Select]) -> Table:
    probe = User(id=0, role=Role.READER)  # only to read the column names
    return Table(name, tuple(c.key for c in query(probe).selected_columns), query)


TABLES: tuple[Table, ...] = (
    _table("annotations", _annotations),
    _table("reviews", _reviews),
    _table("read_throughs", _read_throughs),
    _table("reading_status", _reading_status),
    _table("reading_stats_days", _stats_days),
    _table("vocabulary", _vocabulary),
    _table("shelves", _shelves),
    _table("shelf_items", _shelf_items),
    _table("want_to_read", _want_to_read),
    _table("listening_queue", _listening_queue),
)
HIGHLIGHTS, REVIEWS = TABLES[0], TABLES[1]


def _rows(db: Session, table: Table, user: User) -> Iterator[dict[str, Any]]:
    result = db.execute(table.query(user).execution_options(yield_per=BATCH))
    for row in result:
        yield {
            c: (_iso(v) if isinstance(v, datetime) else v)
            for c, v in zip(table.columns, row, strict=True)
        }


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, dict | list):
        return json.dumps(v, ensure_ascii=False)
    s = str(v)
    # Keep a spreadsheet from running a cell as a formula (JSON stays exact).
    return "'" + s if isinstance(v, str) and s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _csv_line(cells: list[str]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerow(cells)
    return buf.getvalue()


class _Sink(io.RawIOBase):
    """What the zip writes to: collects bytes until the generator hands them on. It has no
    ``seek``, so zipfile writes entries with data descriptors."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.pos = 0

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        self.buf += b
        self.pos += len(b)
        return len(b)

    def tell(self) -> int:
        return self.pos

    def take(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


def _json_table(db: Session, table: Table, user: User, first: bool) -> Iterator[str]:
    yield ("" if first else ",") + json.dumps(table.name) + ":["
    for i, row in enumerate(_rows(db, table, user)):
        yield ("," if i else "") + json.dumps(row, ensure_ascii=False)
    yield "]"


def _markdown_highlights(db: Session, user: User) -> Iterator[str]:
    yield "# Highlights\n\n"
    work_id = chapter = None
    for r in _rows(db, HIGHLIGHTS, user):
        if r["work_id"] != work_id:
            work_id, chapter = r["work_id"], None
            yield f"## {r['title']}\n\n"
            if r["author"]:
                yield f"*{r['author']}*\n\n"
        if r["chapter"] and r["chapter"] != chapter:
            chapter = r["chapter"]
            yield f"### {chapter}\n\n"
        yield _quote(r["text"]) + "\n\n"
        if r["note"] and r["note"].strip():
            note = r["note"].strip().splitlines()
            yield "\n".join([f"**Note:** {note[0]}", *map(_line, note[1:])]) + "\n\n"
        page = r["page"] or r["pageno"]
        when = (r["created"] or "")[:16]
        yield (f"*Page {page} · {when}*" if page else f"*{when}*") + "\n\n"


def _markdown_notes(db: Session, user: User) -> Iterator[str]:
    yield "# Notes on books\n\n"
    for r in _rows(db, REVIEWS, user):
        notes = (r["notes"] or "").strip()
        if not notes and r["rating"] is None:
            continue
        yield f"## {r['title']}\n\n"
        if r["author"]:
            yield f"*{r['author']}*\n\n"
        if r["rating"] is not None:
            yield f"Rating: {r['stars']:g} / 5\n\n"
        if notes:
            # Their own Markdown; a heading in it is kept below the book's heading.
            lines = (
                "#### " + ln.lstrip("# ") if ln.startswith("#") else ln for ln in notes.splitlines()
            )
            yield "\n".join(lines) + "\n\n"


def stream_archive(factory: sessionmaker[Session], user_id: int) -> Iterator[bytes]:
    """The zip, in chunks. Opens its own session (the request's is closed before a streaming
    body runs) and reads ``user_id``'s rows only."""
    sink = _Sink()
    now = datetime.now(UTC)
    stamp = (now.year, now.month, now.day, now.hour, now.minute, now.second)
    with factory() as db, zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as zf:
        user = db.get(User, user_id)
        if user is None:
            return

        def entry(name: str, parts: Iterator[str]) -> Iterator[bytes]:
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            with zf.open(info, "w", force_zip64=True) as f:
                for part in parts:
                    f.write(part.encode())
                    if len(sink.buf) >= CHUNK:
                        yield sink.take()
            yield sink.take()

        def data_json() -> Iterator[str]:
            head = {
                "schema_version": SCHEMA_VERSION,
                "exported_at": _iso(now),
                "user": {
                    "username": user.username,
                    "display_name": user.display_name,
                    "reading_timezone": user.reading_timezone,
                },
            }
            yield json.dumps(head, ensure_ascii=False)[:-1] + ',"tables":{'
            for i, t in enumerate(TABLES):
                yield from _json_table(db, t, user, i == 0)
            yield "}}\n"

        def csv_lines(t: Table) -> Iterator[str]:
            yield _csv_line(list(t.columns))
            for row in _rows(db, t, user):
                yield _csv_line([_cell(row[c]) for c in t.columns])

        yield from entry("README.md", iter([README]))
        yield from entry("data.json", data_json())
        for t in TABLES:
            yield from entry(f"csv/{t.name}.csv", csv_lines(t))
        yield from entry("markdown/highlights.md", _markdown_highlights(db, user))
        yield from entry("markdown/notes.md", _markdown_notes(db, user))
    yield sink.take()  # the central directory
