"""Decide whether an incoming file belongs to an existing work."""

from __future__ import annotations

import re
import unicodedata

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


def find_matching_work(
    db: Session, title: str, authors: list[str], isbn: str | None
) -> Work | None:
    if isbn:
        hit = db.scalar(select(Work).where(Work.isbn == isbn))
        if hit is not None:
            return hit
    norm_title = sort_title(title)
    if not norm_title:
        return None
    primary = author_key(authors[0]) if authors else ""
    # Cheap prefilter on the first word so we do not fuzz the whole table.
    first = norm_title.split()[0]
    candidates = db.scalars(select(Work).where(Work.sort_title.contains(first))).all()
    best: tuple[int, Work] | None = None
    for work in candidates:
        t_score = fuzz.token_sort_ratio(norm_title, work.sort_title)
        if t_score < TITLE_THRESHOLD:
            continue
        if primary and work.author:
            a_score = fuzz.token_set_ratio(primary, author_key(work.author))
            if a_score < AUTHOR_THRESHOLD:
                continue
        if best is None or t_score > best[0]:
            best = (t_score, work)
    return best[1] if best else None
