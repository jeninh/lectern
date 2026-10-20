"""A work's descriptive metadata as one schema, and its ``book.json`` sidecar.

``book.json`` sits in every work folder (``<library>/<Author>/<Title>/book.json``) and mirrors
the database row: metadata, files with their SHA-256 and KOReader partial MD5, and tracks. It
lets a person (or a rebuild) understand a folder without the database. It is written
atomically after every change and never read back as the source of truth; the database is.
Original EPUBs and audio files are never rewritten to carry metadata.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern.fsops import write_json_atomic
from lectern.ingest.epub import normalize_isbn
from lectern.ingest.library import safe_component
from lectern.ingest.matching import sort_author, sort_title
from lectern.models import AudioTrack, BookFile, ShelfItem, Work, utcnow

log = logging.getLogger("lectern.workmeta")

BOOK_JSON = "book.json"
BOOK_JSON_VERSION = 1
PLAYBACK = ("read", "listen", "readalong")

Line = Annotated[str, StringConstraints(strip_whitespace=True, max_length=512)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
_LANG = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$")
_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


class AuthorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
    sort: Annotated[str, StringConstraints(strip_whitespace=True, max_length=256)] | None = None


class Metadata(BaseModel):
    """Every editable descriptive field. Used by imports (review) and the work editor alike,
    and by any later automation client: validation lives here, not in a UI."""

    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]
    sort_title: Line | None = None
    authors: list[AuthorIn] = Field(default_factory=list, max_length=20)
    series: Annotated[str, StringConstraints(strip_whitespace=True, max_length=256)] | None = None
    series_index: float | None = Field(default=None, ge=0, le=100000)
    description: Annotated[str, StringConstraints(max_length=20000)] | None = None
    language: Annotated[str, StringConstraints(strip_whitespace=True, max_length=16)] | None = None
    isbn: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)] | None = None
    publisher: Annotated[str, StringConstraints(strip_whitespace=True, max_length=256)] | None = (
        None
    )
    published: Annotated[str, StringConstraints(strip_whitespace=True, max_length=32)] | None = None
    tags: list[Short] = Field(default_factory=list, max_length=50)
    narrators: list[Short] = Field(default_factory=list, max_length=20)

    @field_validator("language")
    @classmethod
    def _lang(cls, v: str | None) -> str | None:
        if not v:
            return None
        if not _LANG.match(v):
            raise ValueError("language must be a language tag such as en or pt-BR")
        return v

    @field_validator("isbn")
    @classmethod
    def _isbn(cls, v: str | None) -> str | None:
        if not v:
            return None
        isbn = normalize_isbn(v)
        if isbn is None:
            raise ValueError("not a valid ISBN-10 or ISBN-13")
        return isbn

    @field_validator("published")
    @classmethod
    def _date(cls, v: str | None) -> str | None:
        if not v:
            return None
        if not _DATE.match(v):
            raise ValueError("publication date must be YYYY, YYYY-MM, or YYYY-MM-DD")
        return v

    @field_validator("tags", "narrators")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        seen: dict[str, str] = {}
        for item in v:
            seen.setdefault(item.casefold(), item)
        return list(seen.values())

    @field_validator("series", "publisher", "sort_title", "description")
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        return v if v and v.strip() else None


PlaybackDefault = Literal["read", "listen", "readalong"] | None


def authors_of(work: Work) -> list[dict]:
    if work.authors:
        return [
            {"name": a["name"], "sort": a.get("sort") or sort_author(a["name"])}
            for a in work.authors
        ]
    return [{"name": work.author, "sort": work.sort_author}] if work.author else []


def display_author(authors: list[dict]) -> str:
    return " & ".join(a["name"] for a in authors) or "Unknown Author"


def metadata_of(work: Work) -> dict:
    """Editable metadata. Sort forms Lectern derives itself are ``None`` (shown as a
    placeholder); only a sort title or sort name someone chose is returned as a value."""
    return {
        "title": work.title,
        "sort_title": None if work.sort_title == sort_title(work.title) else work.sort_title,
        "authors": [
            {"name": a["name"], "sort": None if a["sort"] == sort_author(a["name"]) else a["sort"]}
            for a in authors_of(work)
        ],
        "series": work.series,
        "series_index": work.series_index,
        "description": work.description,
        "language": work.language,
        "isbn": work.isbn,
        "publisher": work.publisher,
        "published": work.published,
        "tags": list(work.tags or []),
        "narrators": list(work.narrators or []),
    }


def apply_metadata(work: Work, meta: Metadata | dict) -> None:
    """Write validated metadata onto the row, deriving the display and sort forms."""
    m = meta if isinstance(meta, Metadata) else Metadata.model_validate(meta)
    authors = [{"name": a.name, "sort": a.sort or sort_author(a.name)} for a in m.authors]
    if not authors:
        authors = [{"name": "Unknown Author", "sort": sort_author("Unknown Author")}]
    work.title = m.title[:512]
    work.sort_title = (m.sort_title or sort_title(m.title))[:512]
    work.authors = authors
    work.author = display_author(authors)[:512]
    work.sort_author = authors[0]["sort"][:512]
    work.series = m.series
    work.series_index = m.series_index if m.series else None
    work.description = m.description
    work.language = m.language
    work.isbn = m.isbn
    work.publisher = m.publisher
    work.published = m.published
    work.tags = m.tags or None
    work.narrators = m.narrators or None


def desired_dir_parts(work: Work) -> tuple[str, str]:
    """Folder components for the work: ``<first author>/<title>`` made filesystem-safe."""
    authors = authors_of(work)
    first = authors[0]["name"] if authors else "Unknown Author"
    return safe_component(first, "Unknown Author"), safe_component(work.title, "Untitled")


def folder_matches(work: Work) -> bool:
    """The work's folder is already named after its (first) author and title, allowing the
    `` (n)`` suffix collisions get."""
    author, title = desired_dir_parts(work)
    p = Path(work.dir_path)
    if len(p.parts) != 2 or p.parts[0] != author:
        return False
    return p.name == title or re.fullmatch(re.escape(title) + r" \(\d+\)", p.name) is not None


def book_json(db: Session, work: Work) -> dict:
    files = db.scalars(select(BookFile).where(BookFile.work_id == work.id).order_by(BookFile.id))
    tracks = db.scalars(
        select(AudioTrack).where(AudioTrack.work_id == work.id).order_by(AudioTrack.position)
    )
    shelves = db.scalars(select(ShelfItem.shelf_id).where(ShelfItem.work_id == work.id))
    return {
        "lectern_book": BOOK_JSON_VERSION,
        "id": work.id,
        "version": work.version,
        **metadata_of(work),
        "playback_default": work.playback_default,
        "readalong_enabled": work.readalong_enabled,
        "primary_file_id": work.primary_file_id,
        "cover": "cover.jpg" if work.has_cover else None,
        "files": [
            {
                "id": f.id,
                "kind": f.kind.value,
                "path": f.rel_path,
                "media_type": f.media_type,
                "size": f.size,
                "sha256": f.sha256,
                "koreader_partial_md5": f.partial_md5,
            }
            for f in files
        ],
        "tracks": [
            {
                "id": t.id,
                "position": t.position,
                "title": t.title,
                "path": t.rel_path,
                "duration_s": t.duration_s,
                "sha256": t.sha256,
                "chapters": t.chapters or [],
            }
            for t in tracks
        ],
        "shelf_ids": sorted(shelves),
        "written_at": utcnow().isoformat().replace("+00:00", "Z"),
    }


def write_book_json(db: Session, library: Path, work: Work) -> None:
    """Best effort: a full disk must not undo an edit that the database already holds."""
    d = library / work.dir_path
    if not d.is_dir():
        return
    try:
        db.flush()
        write_json_atomic(d / BOOK_JSON, book_json(db, work))
    except OSError:
        log.exception("could not write book.json for work %s", work.id)
