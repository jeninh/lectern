"""Audiobook bookmarks and clips with notes (issue #81).

Mark
    A bookmark is a moment on a work's audio timeline (``start_s``); a clip is a span
    (``start_s``..``end_s``, at most :data:`MAX_CLIP_S`). Both keep times only: a clip plays
    back from the work's original tracks, nothing is cut, encoded, or queued. ``chapter`` is
    the audio chapter the player showed.

Place
    When the work's audiobook is aligned with its text (read-along), a mark is placed on the
    sentences spoken in it (:func:`place`): the first and last sentence numbers and the text
    range the reader opens, from the primary EPUB, the same coordinates KOReader highlights
    are placed in (``lectern.annotations``), so both land on the same passage. Placement is
    worked out when marks are read, so a re-alignment moves them with the text. Without
    alignment a mark keeps its time and chapter only.

Privacy
    Marks are private-source data like highlights: only their owner sees them, only for works
    they can still see (:func:`lectern.access.visible_works`), never other household members,
    OPDS, or MCP. Nothing here logs notes. They live in the database, so backups and restore
    carry them.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern import search
from lectern.access import visible_works
from lectern.models import AudioBookmark, User, Work
from lectern.positions import normalize_spine_text

MAX_NOTE = 20000
MAX_CLIP_S = 30 * 60.0
EXCERPT = 600  # characters of the passage shown and exported


@dataclass(frozen=True)
class Placement:
    sentence_n: int
    sentence_end_n: int
    spine_index: int
    char: int
    length: int
    text: str | None


@lru_cache(maxsize=8)
def _spine_text(path: str, sha256: str, spine_index: int) -> str:  # sha256 keys the cache
    from lectern.align.text import read_spine_item

    return normalize_spine_text(read_spine_item(Path(path), spine_index))


def place(db: Session, library: Path, work: Work, marks: list[AudioBookmark]) -> dict:
    """``{mark id: Placement}`` for the marks that fall on aligned text; none when the work's
    audio is not aligned with its text."""
    from lectern.align.tracks import work_epub
    from lectern.progress import SentenceCache

    if not marks:
        return {}
    wt = SentenceCache.get(db, work.id)
    if not wt.matched:
        return {}
    idx = wt.index
    found = work_epub(db, library, work)
    out = {}
    for m in marks:
        first = idx.at_time(m.start_s)
        if first is None:
            continue
        last = idx.at_time(m.end_s) if m.end_s is not None else first
        if last is None or last.n < first.n:
            last = first
        a = idx.sentence(first.n)
        # One spine item: the reader marks a range inside one section.
        end = a.char_end
        for n in range(last.n, first.n, -1):
            s = idx.sentence(n)
            if s.spine_index == a.spine_index:
                end = s.char_end
                break
        text = None
        if found is not None:
            try:
                full = _spine_text(str(found[1]), found[0].sha256, a.spine_index)
                text = full[a.char_start : end][:EXCERPT] or None
            except Exception:  # noqa: BLE001 - an unreadable EPUB leaves the passage out
                text = None
        out[m.id] = Placement(first.n, last.n, a.spine_index, a.char_start,
                              end - a.char_start, text)  # fmt: skip
    return out


def visible(db: Session, user: User, work_id: int | None = None):
    """``[(work, [mark, ...]), ...]``: the user's own marks on works they can see, each work's
    in time order, works by title."""
    q = (
        select(AudioBookmark, Work)
        .join(Work, Work.id == AudioBookmark.work_id)
        .where(AudioBookmark.user_id == user.id, visible_works(user))
        .order_by(AudioBookmark.start_s, AudioBookmark.id)
    )
    if work_id is not None:
        q = q.where(Work.id == work_id)
    by_work: dict[int, tuple[Work, list[AudioBookmark]]] = {}
    for m, w in db.execute(q):
        by_work.setdefault(w.id, (w, []))[1].append(m)
    return sorted(by_work.values(), key=lambda p: (p[0].title.casefold(), p[0].id))


def iso(dt) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def as_json(m: AudioBookmark, p: Placement | None) -> dict:
    return {
        "id": m.id,
        "work_id": m.work_id,
        "kind": "clip" if m.end_s is not None else "bookmark",
        "start_s": m.start_s,
        "end_s": m.end_s,
        "note": m.note,
        "chapter": m.chapter,
        "device": m.device,
        "created_at": iso(m.created_at),
        "updated_at": iso(m.updated_at),
        "sentence_n": p.sentence_n if p else None,
        "sentence_end_n": p.sentence_end_n if p else None,
        "location": (
            {"spine_index": p.spine_index, "char": p.char, "length": p.length} if p else None
        ),
        "text": p.text if p else None,
    }


# --- searching and filtering (the highlights page, issue #72) -------------------------------


def _fold(text: str) -> str:
    """Case- and accent-folded, as the highlights index (``remove_diacritics 2``) folds."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


_FIELD = re.compile(r"^\s*(highlight|note)\s*:\s*", re.IGNORECASE)


def matches(note: str | None, q: str, *, prefix: bool) -> bool:
    """Whether a mark's note has every word of ``q`` (the last one as a prefix while typed),
    the way the highlights search matches. ``highlight:`` searches highlighted text, which a
    mark does not have. Raises :class:`lectern.search.QueryError`."""
    q = q.strip()
    if not q:
        return True
    m = _FIELD.match(q)
    if m and m.group(1).lower() == "highlight":
        return False
    terms = [_fold(t) for t in search.query_terms(q[m.end() :] if m else q)]
    if not terms or max(len(t) for t in terms) < 2 or not note:
        return False
    words = search._TERM.findall(_fold(note))
    for i, t in enumerate(terms):
        last = prefix and i == len(terms) - 1 and len(t) >= search.PREFIX_MIN
        if not any(w == t or (last and w.startswith(t)) for w in words):
            return False
    return True


def find(db: Session, user: User, q: str, filters, *, prefix: bool = True):
    """As :func:`visible`, keeping the marks whose note matches ``q`` and that pass the
    highlights page's ``filters`` (``lectern.annotations.Filters``; dates are the day the
    mark was made, UTC)."""
    out = []
    for w, marks in visible(db, user, filters.work_id):
        if filters.author is not None and w.author != filters.author:
            continue
        if filters.tag is not None and filters.tag not in (w.tags or []):
            continue
        kept = []
        for m in marks:
            day = iso(m.created_at)[:10]
            if filters.since is not None and day < filters.since:
                continue
            if filters.until is not None and day > filters.until:
                continue
            if matches(m.note, q, prefix=prefix):
                kept.append(m)
        if kept:
            out.append((w, kept))
    return out


# --- Markdown export (with the highlights, lectern.annotations.markdown) ---------------------


def clock(s: float) -> str:
    """``1:02:03`` / ``2:03``, as the player shows times."""
    s = max(0, int(s))
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def where(m: AudioBookmark) -> str:
    """ "Audio clip 1:02:03–1:02:33 · Chapter 4 · 2026-10-28 09:30" (UTC)."""
    span = (
        f"Audio clip {clock(m.start_s)}–{clock(m.end_s)}"
        if m.end_s is not None
        else f"Audio bookmark {clock(m.start_s)}"
    )
    parts = [span, *([m.chapter] if m.chapter else []), iso(m.created_at)[:16].replace("T", " ")]
    return " · ".join(parts)


def markdown_lines(marks: list[AudioBookmark], placed: dict, level: int) -> list[str]:
    """The ``Audio bookmarks`` section of one work in the export (``level``: the work's
    heading level)."""
    from lectern.annotations import _line, _quote

    lines = [f"{'#' * (level + 1)} Audio bookmarks", ""]
    for m in marks:
        p = placed.get(m.id)
        if p is not None and p.text:
            lines += [_quote(p.text), ""]
        if m.note:
            note = m.note.strip().splitlines()
            lines += [f"**Note:** {_line(note[0])}", *map(_line, note[1:]), ""]
        lines += [f"*{where(m)}*", ""]
    return lines
