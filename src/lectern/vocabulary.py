"""Your private vocabulary list (issue #80): words saved from the dictionary lookup.

Saving is explicit (a Save button, ``POST /api/vocabulary``); the lookup itself stores
nothing. Rows belong to one person. A word keeps its book only while that person can still
see the book: otherwise the book, location, page and context read as null, so an unshared
book never leaks through a saved word. Words, notes and contexts are never logged and never
passed to the audit log.
"""

from __future__ import annotations

import csv
import html
import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lectern.access import can_see_work, visible_works
from lectern.dictionary import fold
from lectern.models import BookFile, Sentence, User, VocabularyWord, Work, utcnow

MAX_DEFINITION = 4000
MAX_CONTEXT = 1000
MAX_EXPORT = 10_000


class SavePayload(BaseModel):
    word: str = Field(min_length=1, max_length=200)
    lemma: str | None = Field(default=None, max_length=200)
    lang: str = Field(default="", max_length=16)
    to: str | None = Field(default=None, max_length=16)
    definition: str = Field(min_length=1, max_length=MAX_DEFINITION)
    note: str | None = Field(default=None, max_length=MAX_DEFINITION)
    pos: str | None = Field(default=None, max_length=32)
    dictionary: str | None = Field(default=None, max_length=120)
    context: str | None = Field(default=None, max_length=MAX_CONTEXT)
    work_id: int | None = None
    file_id: int | None = None
    spine_index: int | None = Field(default=None, ge=0)
    char: int | None = Field(default=None, ge=0)
    length: int | None = Field(default=None, ge=0)
    page: int | None = Field(default=None, ge=1)


class BadWord(ValueError):
    """The word has nothing left to save once folded (only punctuation or spaces)."""


class NotVisible(LookupError):
    """The book is not one this person can see."""


@dataclass
class Filters:
    work: int | None = None
    lang: str | None = None
    since: str | None = None  # YYYY-MM-DD, UTC, on created_at
    until: str | None = None  # inclusive
    limit: int | None = None
    offset: int = 0


def _clean(s: str | None) -> str | None:
    s = (s or "").strip()
    return s or None


def _sentence_n(db: Session, p: SavePayload) -> int | None:
    if p.work_id is None or p.spine_index is None or p.char is None:
        return None
    return db.scalar(
        select(Sentence.n).where(
            Sentence.work_id == p.work_id,
            Sentence.spine_index == p.spine_index,
            Sentence.char_start <= p.char,
            Sentence.char_end > p.char,
        )
    )


def _find(db: Session, user: User, lang: str, key: str) -> VocabularyWord | None:
    return db.scalar(
        select(VocabularyWord).where(
            VocabularyWord.user_id == user.id,
            VocabularyWord.lang == lang,
            VocabularyWord.lemma_key == key,
        )
    )


def save(db: Session, user: User, p: SavePayload) -> tuple[VocabularyWord, bool]:
    """Save a word; ``(row, created)``. Saving it again counts it and keeps the first
    context, location and definition, so the person's edits survive."""
    if p.work_id is not None and not can_see_work(db, user, p.work_id):
        raise NotVisible
    word = p.word.strip()
    lemma = _clean(p.lemma) or word
    key = fold(lemma) or fold(word)
    if not key:
        raise BadWord
    lang = p.lang.strip()
    now = utcnow()
    row = _find(db, user, lang, key)
    if row is not None:
        row.seen_count += 1
        row.last_seen_at = now
        db.flush()
        return row, False
    file_id = p.file_id
    if file_id is not None and not db.scalar(
        select(BookFile.id).where(BookFile.id == file_id, BookFile.work_id == p.work_id)
    ):
        file_id = None
    row = VocabularyWord(
        user_id=user.id,
        lang=lang,
        lemma=lemma,
        lemma_key=key,
        word=word,
        to=_clean(p.to),
        definition=p.definition,
        note=_clean(p.note),
        pos=_clean(p.pos),
        dictionary=_clean(p.dictionary),
        context=_clean(p.context),
        work_id=p.work_id,
        file_id=file_id,
        spine_index=p.spine_index,
        char=p.char,
        length=p.length,
        page=p.page,
        sentence_n=_sentence_n(db, p),
        created_at=now,
        last_seen_at=now,
    )
    try:
        with db.begin_nested():
            db.add(row)
    except IntegrityError:  # saved twice at once: the other one won
        existing = _find(db, user, lang, key)
        if existing is None:
            raise
        existing.seen_count += 1
        existing.last_seen_at = now
        db.flush()
        return existing, False
    return row, True


def _day(s: str) -> datetime:
    return datetime.combine(date.fromisoformat(s), datetime.min.time(), tzinfo=UTC)


def visible_list(
    db: Session, user: User, f: Filters
) -> tuple[list[tuple[VocabularyWord, Work | None]], int]:
    """The person's words, newest first, each with its book if they can still see it; and
    the total matching the filters."""
    where = [VocabularyWord.user_id == user.id]
    if f.lang is not None:
        where.append(VocabularyWord.lang == f.lang)
    if f.work is not None:
        where += [VocabularyWord.work_id == f.work, Work.id.is_not(None)]
    if f.since:
        where.append(VocabularyWord.created_at >= _day(f.since))
    if f.until:
        where.append(VocabularyWord.created_at < _day(f.until) + timedelta(days=1))
    join = and_(Work.id == VocabularyWord.work_id, visible_works(user))
    q = (
        select(VocabularyWord, Work)
        .outerjoin(Work, join)
        .where(*where)
        .order_by(VocabularyWord.created_at.desc(), VocabularyWord.id.desc())
    )
    total = db.scalar(
        select(func.count()).select_from(VocabularyWord).outerjoin(Work, join).where(*where)
    )
    if f.limit is not None:
        q = q.limit(f.limit)
    q = q.offset(f.offset)
    return [(w, b) for w, b in db.execute(q)], int(total or 0)


def facets(db: Session, user: User) -> dict:
    """Languages and (visible) books the person has words in, with counts."""
    langs = db.execute(
        select(VocabularyWord.lang, func.count())
        .where(VocabularyWord.user_id == user.id)
        .group_by(VocabularyWord.lang)
        .order_by(VocabularyWord.lang)
    )
    works = db.execute(
        select(Work.id, Work.title, Work.author, func.count())
        .join(VocabularyWord, VocabularyWord.work_id == Work.id)
        .where(VocabularyWord.user_id == user.id, visible_works(user))
        .group_by(Work.id)
        .order_by(Work.sort_title)
    )
    return {
        "languages": [{"lang": lang, "count": n} for lang, n in langs],
        "works": [{"id": i, "title": t, "author": a, "count": n} for i, t, a, n in works],
    }


def shown_work(db: Session, user: User, row: VocabularyWord) -> Work | None:
    """The row's book if ``user`` can still see it."""
    if row.work_id is None:
        return None
    return db.scalar(select(Work).where(Work.id == row.work_id, visible_works(user)))


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def link(row: VocabularyWord) -> str | None:
    """Where the reader opens at this word (a path on this server)."""
    if row.work_id is None:
        return None
    base = f"/app/reader/{row.work_id}"
    if row.spine_index is not None and row.char is not None:
        return f"{base}?at={row.spine_index}:{row.char}&len={row.length or 0}"
    if row.page is not None:
        file = f"file={row.file_id}&" if row.file_id else ""
        return f"{base}?{file}page={row.page}"
    return base


def as_json(row: VocabularyWord, work: Work | None) -> dict:
    """``work`` is the row's book if the person can still see it, else None: then the book,
    location, page and context read as null (word, definition and note stay)."""
    shown = work is not None
    return {
        "id": row.id,
        "word": row.word,
        "lemma": row.lemma,
        "lang": row.lang,
        "to": row.to,
        "definition": row.definition,
        "note": row.note,
        "pos": row.pos,
        "dictionary": row.dictionary,
        "context": row.context if shown else None,
        "book": {"id": work.id, "title": work.title, "author": work.author} if work else None,
        "location": (
            {"spine_index": row.spine_index, "char": row.char, "length": row.length}
            if shown and row.spine_index is not None and row.char is not None
            else None
        ),
        "page": row.page if shown else None,
        "file_id": row.file_id if shown else None,
        "sentence_n": row.sentence_n if shown else None,
        "seen_count": row.seen_count,
        "created_at": _iso(row.created_at),
        "last_seen_at": _iso(row.last_seen_at),
        "updated_at": _iso(row.updated_at),
    }


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()[:60]


def _html(s: str | None) -> str:
    s = html.escape(s or "", quote=True).replace("\t", " ")
    return re.sub(r"\r\n|\r|\n", "<br>", s)


def export_anki(rows: list[tuple[VocabularyWord, Work | None]]) -> str:
    """Anki's text import: tab-separated, HTML, tags in column 5. Built here, offline."""
    out = [
        "#separator:tab",
        "#html:true",
        "#columns:Front\tBack\tContext\tBook\tTags",
        "#tags column:5",
    ]
    for row, work in rows:
        back = _html(row.definition)
        if row.note:
            back += "<br><br>" + _html(row.note)
        tags = ["lectern"]
        if row.lang:
            tags.append("lang::" + re.sub(r"[^\w\-]+", "_", row.lang))
        if work and _slug(work.title):
            tags.append("book::" + _slug(work.title))
        out.append(
            "\t".join(
                [
                    _html(row.lemma),
                    back,
                    _html(row.context) if work else "",
                    _html(work.title) if work else "",
                    " ".join(tags),
                ]
            )
        )
    return "\n".join(out) + "\n"


def _defuse(cell: str) -> str:
    return "'" + cell if cell[:1] in ("=", "+", "-", "@", "\t", "\r") else cell


def export_csv(rows: list[tuple[VocabularyWord, Work | None]], base_url: str = "") -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(
        ["word", "lemma", "language", "translation language", "definition", "note", "context",
         "book", "author", "saved", "seen count", "link"]
    )  # fmt: skip
    for row, work in rows:
        cells = [
            row.word,
            row.lemma,
            row.lang,
            row.to or "",
            row.definition,
            row.note or "",
            (row.context or "") if work else "",
            work.title if work else "",
            work.author if work else "",
            row.created_at.astimezone(UTC).date().isoformat(),
            str(row.seen_count),
            (base_url + link(row)) if work and link(row) else "",
        ]
        w.writerow([_defuse(c) for c in cells])
    return buf.getvalue()
