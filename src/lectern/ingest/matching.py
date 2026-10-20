"""Decide whether an incoming file belongs to an existing work."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern.models import Work

TITLE_THRESHOLD = 92
AUTHOR_THRESHOLD = 85
_ARTICLES = ("the", "a", "an")
_SURNAME_PARTICLES = {
    "le",
    "la",
    "de",
    "del",
    "della",
    "di",
    "da",
    "du",
    "van",
    "von",
    "der",
    "den",
    "ten",
    "ter",
    "st",
    "mac",
    "mc",
    "o",
}


def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.split())


def sort_title(title: str) -> str:
    """Drop a leading or trailing article: 'The X' and 'X, The' both become 'x'."""
    raw = title.strip()
    for art in _ARTICLES:
        if raw.lower().endswith(f", {art}"):
            raw = raw[: -(len(art) + 2)]
            break
    t = normalize_text(raw)
    words = t.split()
    if len(words) > 1 and words[0] in _ARTICLES:
        return " ".join(words[1:])
    return t


def sort_author(author: str) -> str:
    """'Ursula K. Le Guin' -> 'le guin, ursula k'. Keeps 'Last, First' input as is."""
    if "," in author:
        last, _, first = author.partition(",")
        return f"{normalize_text(last)}, {normalize_text(first)}".rstrip(", ")
    parts = normalize_text(author).split()
    if len(parts) < 2:
        return " ".join(parts)
    split = len(parts) - 1
    while split > 1 and parts[split - 1] in _SURNAME_PARTICLES:
        split -= 1
    return f"{' '.join(parts[split:])}, {' '.join(parts[:split])}"


def author_key(author: str) -> str:
    """Order-insensitive form so 'Le Guin, Ursula K.' == 'Ursula K. Le Guin'."""
    return " ".join(sorted(normalize_text(author.replace(",", " ")).split()))


_EDITION_NOISE = re.compile(
    r"\s*[(\[][^)\]]*\b(?:un)?abridged\b[^)\]]*[)\]]"
    r"|\s*[(\[][^)\]]*\baudio ?book\b[^)\]]*[)\]]"
    r"|\s*[-:,]\s*(?:un)?abridged\s*$",
    re.IGNORECASE,
)
# Below this a candidate is not worth asking about.
SUGGEST_THRESHOLD = 75
# A title this close to a work that already has audio is that audiobook's next file, whatever
# the artist tag says (it is often the narrator).
AUDIO_CONTINUATION_THRESHOLD = 97


def clean_title(title: str) -> str:
    """Drop edition noise audiobooks add to titles: "(Unabridged)", "[Audiobook]"."""
    return _EDITION_NOISE.sub("", title).strip()


@dataclass
class MatchResult:
    """``certain``: attach to ``work``. Otherwise ``work`` (if any) is a candidate to confirm."""

    work: Work | None = None
    certain: bool = False
    score: float = 0.0
    reason: str = ""


def match_work(
    db: Session,
    title: str,
    authors: list[str],
    isbn: str | None,
    *,
    prefer_audio: bool = False,
) -> MatchResult:
    """Find the work an incoming file belongs to, with how sure we are.

    Certain: same ISBN, or title >= 92 and author >= 85 (fuzzy). For audio, also a near-exact
    title on a work that already has audio tracks. Uncertain (a person confirms in the UI):
    title >= 75, or a title match whose author differs (audiobook tags often name the narrator).
    """
    if isbn:
        hit = db.scalar(select(Work).where(Work.isbn == isbn))
        if hit is not None:
            return MatchResult(hit, True, 100.0, "same ISBN")
    norm_title = sort_title(clean_title(title))
    if not norm_title:
        return MatchResult()
    primary = author_key(authors[0]) if authors else ""
    # Cheap prefilter on the first word so we do not fuzz the whole table.
    first = norm_title.split()[0]
    candidates = db.scalars(select(Work).where(Work.sort_title.contains(first))).all()
    with_audio: set[int] = set()
    if prefer_audio and candidates:
        from lectern.models import AudioTrack

        with_audio = set(
            db.scalars(
                select(AudioTrack.work_id).where(AudioTrack.work_id.in_([w.id for w in candidates]))
            )
        )
    best_certain: tuple[float, Work, str] | None = None
    best_maybe: tuple[float, Work, str] | None = None
    for work in candidates:
        t_score = fuzz.token_sort_ratio(norm_title, work.sort_title)
        if t_score < SUGGEST_THRESHOLD:
            continue
        a_score = (
            fuzz.token_set_ratio(primary, author_key(work.author))
            if primary and work.author
            else None
        )
        certain = t_score >= TITLE_THRESHOLD and (a_score is None or a_score >= AUTHOR_THRESHOLD)
        reason = f"title {t_score:.0f}%" + (
            f", author {a_score:.0f}%" if a_score is not None else ""
        )
        if not certain and work.id in with_audio and t_score >= AUDIO_CONTINUATION_THRESHOLD:
            certain, reason = True, f"{reason}, continues its audiobook"
        if certain:
            if best_certain is None or t_score > best_certain[0]:
                best_certain = (t_score, work, reason)
        else:
            combined = 0.7 * t_score + 0.3 * (a_score if a_score is not None else 50)
            if best_maybe is None or combined > best_maybe[0]:
                best_maybe = (combined, work, reason)
    if best_certain:
        return MatchResult(best_certain[1], True, best_certain[0], best_certain[2])
    if best_maybe:
        return MatchResult(best_maybe[1], False, round(best_maybe[0], 1), best_maybe[2])
    return MatchResult()
