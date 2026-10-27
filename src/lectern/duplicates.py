"""Likely duplicates among the works already in the library. Purely local: no network.

Three signals, each enough on its own:

- the same ISBN;
- the same file (an EPUB, PDF, or audio track with the same SHA-256) in two works;
- the same book by title and author, as ingest matching measures it
  (:mod:`lectern.ingest.matching`: title >= 92 and author >= 85, fuzzy, articles and edition
  noise ignored); two numbered volumes of one series are not duplicates.

Each pair found becomes a ``match_suggestions`` row (``origin = "library"``) in the same
review list as ingest's: *Merge* uses the existing merge path (the newer work into the older
one) and *Ignore* dismisses it. Any pair that already has a row, open or resolved, either way
round, is never flagged again, so an ignored pair stays ignored.

Merging a library pair (:func:`carry_over`, then the ingest merge) also keeps what someone
set on the copy that goes: its metadata fills the kept copy's empty fields, and each person's
reading position and reading state move over when they have none on the kept copy.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from itertools import combinations

from rapidfuzz import fuzz
from sqlalchemy import select, union_all
from sqlalchemy.orm import Session

from lectern.ingest.matching import (
    AUTHOR_THRESHOLD,
    TITLE_THRESHOLD,
    author_key,
    clean_title,
    sort_title,
)
from lectern.models import (
    AudioTrack,
    BookFile,
    MatchSuggestion,
    ProgressSource,
    ReadingState,
    Work,
    WorkProgress,
)
from lectern.workmeta import apply_metadata, metadata_of

MAX_NEW = 500  # pairs flagged per scan; the rest wait for the next one
MAX_GROUP = 20  # works sharing one ISBN or file that are paired up


def find_pairs(db: Session) -> dict[tuple[int, int], tuple[float, list[str]]]:
    """``(older id, newer id) -> (score, reasons)`` for every likely duplicate pair."""
    works = db.execute(
        select(Work.id, Work.title, Work.author, Work.isbn, Work.series, Work.series_index).where(
            Work.trashed_at.is_(None)
        )
    ).all()
    live = {w.id for w in works}
    pairs: dict[tuple[int, int], tuple[float, list[str]]] = {}

    def flag(a: int, b: int, score: float, reason: str) -> None:
        key = (min(a, b), max(a, b))
        old_score, reasons = pairs.get(key, (0.0, []))
        pairs[key] = (max(old_score, score), [*reasons, reason])

    by_isbn: dict[str, list[int]] = defaultdict(list)
    for w in works:
        if w.isbn:
            by_isbn[w.isbn].append(w.id)
    for ids in by_isbn.values():
        for a, b in combinations(ids[:MAX_GROUP], 2):
            flag(a, b, 100.0, "same ISBN")

    files = union_all(
        select(BookFile.sha256, BookFile.work_id), select(AudioTrack.sha256, AudioTrack.work_id)
    ).subquery()
    by_hash: dict[str, set[int]] = defaultdict(set)
    for sha, wid in db.execute(select(files.c.sha256, files.c.work_id)):
        if wid in live:
            by_hash[sha].add(wid)
    for ids in by_hash.values():
        for a, b in combinations(sorted(ids)[:MAX_GROUP], 2):
            flag(a, b, 100.0, "same file")

    # Title and author: compare only works whose normalized titles share a first word.
    buckets: dict[str, list] = defaultdict(list)
    for w in works:
        t = sort_title(clean_title(w.title))
        if t:
            buckets[t.split()[0]].append((w, t, author_key(w.author) if w.author else ""))
    for bucket in buckets.values():
        for (a, ta, aa), (b, tb, ab) in combinations(bucket, 2):
            t_score = fuzz.token_sort_ratio(ta, tb)
            if t_score < TITLE_THRESHOLD:
                continue
            a_score = fuzz.token_set_ratio(aa, ab) if aa and ab else None
            if a_score is not None and a_score < AUTHOR_THRESHOLD:
                continue
            if (
                a.series_index is not None
                and b.series_index is not None
                and a.series_index != b.series_index
            ):
                continue  # volume 1 and volume 2 of one series
            flag(
                a.id,
                b.id,
                float(t_score),
                f"title {t_score:.0f}%"
                + (f", author {a_score:.0f}%" if a_score is not None else ""),
            )
    return pairs


def scan(db: Session) -> int:
    """Flag new likely-duplicate pairs for review. Returns how many; the caller commits."""
    known = {
        frozenset(p)
        for p in db.execute(select(MatchSuggestion.work_id, MatchSuggestion.candidate_work_id))
    }
    added = 0
    for (older, newer), (score, reasons) in sorted(find_pairs(db).items()):
        if frozenset((older, newer)) in known:
            continue
        # Merging moves ``work_id`` into ``candidate_work_id``: the newer copy into the older.
        db.add(
            MatchSuggestion(
                work_id=newer,
                candidate_work_id=older,
                score=round(score, 1),
                reason="; ".join(reasons)[:200],
                origin="library",
            )
        )
        added += 1
        if added >= MAX_NEW:
            break
    db.flush()
    return added


# Identity (kept from the target) and the ISBN (``merge_work`` moves it) are not filled here.
_KEEP = ("title", "sort_title", "authors", "isbn")


def _empty(v: object) -> bool:
    return v is None or v == "" or v == []


def carry_over(db: Session, source: Work, target: Work) -> Callable[[], None]:
    """Before merging library duplicate ``source`` into ``target``: fill each empty target
    metadata field from the source, and move each person's reading state and text position
    to the target when they have none there (a state on both keeps the earliest start and
    finish). Audio positions are left to ``merge_work``, which moves them only when the
    target has no audio (seconds of another recording mean nothing).

    Returns ``restore``, to run after ``merge_work``: moving a position makes the
    reading-state hook stamp it as read just now, and this puts the moved states back."""
    src, dst = metadata_of(source), metadata_of(target)
    filled = {k: v for k, v in src.items() if k not in _KEEP and _empty(dst[k]) and not _empty(v)}
    if filled:
        apply_metadata(target, {**dst, **filled})

    on_target = {
        s.user_id: s
        for s in db.scalars(select(ReadingState).where(ReadingState.work_id == target.id))
    }
    moved: dict[int, tuple] = {}
    for s in db.scalars(select(ReadingState).where(ReadingState.work_id == source.id)).all():
        t = on_target.get(s.user_id)
        if t is None:
            moved[s.user_id] = (s.fraction, s.last_read_at, s.started_at, s.finished_at)
            db.delete(s)
        else:
            for col in ("started_at", "finished_at"):
                dates = [d for d in (getattr(t, col), getattr(s, col)) if d is not None]
                setattr(t, col, min(dates) if dates else None)
    db.flush()
    for user_id, (fraction, last, started, finished) in moved.items():
        db.add(
            ReadingState(
                user_id=user_id, work_id=target.id, fraction=fraction, last_read_at=last,
                started_at=started, finished_at=finished,
            )
        )  # fmt: skip
    db.flush()

    has_position = set(
        db.scalars(select(WorkProgress.user_id).where(WorkProgress.work_id == target.id))
    )
    for p in db.scalars(select(WorkProgress).where(WorkProgress.work_id == source.id)).all():
        if p.user_id not in has_position and p.source != ProgressSource.AUDIO:
            p.work_id = target.id
    db.flush()

    def restore() -> None:
        db.flush()
        for user_id, (fraction, last, started, finished) in moved.items():
            s = db.get(ReadingState, (user_id, target.id))
            if s is not None:
                s.fraction, s.last_read_at = fraction, last
                s.started_at, s.finished_at = started, finished
        db.flush()

    return restore
