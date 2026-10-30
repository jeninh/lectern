"""Private per-book ratings, notes, and read-throughs (issue #84, docs/reviews.md).

Per person and work: a rating in half stars (stored 1–10, shown ½–5), Markdown notes, and
read-throughs, each with the day it started and finished. ``reading_states`` already derives
a first-started and first-finished time from reading positions. Until someone saves
read-throughs for a work, that is its one read-through (``derived``); once they do, theirs
replace it, so they can correct the dates, add books read before Lectern, and record re-reads.
An open read-through (started, not finished) is closed on the day the person reaches the end
(:mod:`lectern.reading_state`). Nothing here invents reading time: a read-through is only
dates.

Everything is private to its person. Every look-up is by the caller's own user id; owners do
not see members' reviews. Reviews of a work the person can no longer see stay stored but are
not shown. Not on OPDS; over MCP only with the ``reviews`` AI-key scope.

The Library's ``rating:``, ``started:``, and ``finished:`` filters and sorts read the same
rows (:mod:`lectern.filters`), and :func:`finished_in` lists a year's finished books with
their ratings for reading stats (#69).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import and_, case, exists, func, not_, or_, select
from sqlalchemy.orm import Session

from lectern.access import visible_works
from lectern.models import BookReview, ReadingState, ReadThrough, User, Work, utcnow
from lectern.reading_state import status_of

MAX_NOTES = 20_000
MAX_READS = 50
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ReviewError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message

    def json(self) -> dict:
        return {"detail": self.code, "message": self.message}


def stars(half: int | None) -> float | None:
    return half / 2 if half else None


def half_stars(value: float | None) -> int | None:
    """``4.5`` → 9. ``None`` clears the rating; anything but ½ to 5 in halves is refused."""
    if value is None:
        return None
    half = value * 2
    if half != int(half) or not 1 <= half <= 10:
        raise ReviewError("bad_rating", "A rating is ½ to 5 stars, in halves.")
    return int(half)


def day_of(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).date().isoformat() if dt else None


def at_day(text: str | None) -> datetime | None:
    """``"2026-03-15"`` → midnight UTC that day (how read-through days are stored)."""
    if text is None or text == "":
        return None
    if not _DAY.match(text):
        raise ReviewError("bad_date", f"“{text}” is not a date (YYYY-MM-DD).")
    try:
        d = date.fromisoformat(text)
    except ValueError:
        raise ReviewError("bad_date", f"“{text}” is not a date (YYYY-MM-DD).") from None
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


@dataclass(frozen=True)
class Read:
    started_at: datetime | None
    finished_at: datetime | None
    id: int | None = None
    derived: bool = False

    def json(self) -> dict:
        return {
            "id": self.id,
            "started_on": day_of(self.started_at),
            "finished_on": day_of(self.finished_at),
            "derived": self.derived,
        }


def _key(r: ReadThrough | Read):
    return (r.started_at or r.finished_at, r.id or 0)


def reads(db: Session, user_id: int, work_id: int) -> list[Read]:
    """The person's read-throughs of the work, oldest first: theirs, or the one their reading
    state implies (``derived``), or none."""
    rows = db.scalars(
        select(ReadThrough).where(ReadThrough.user_id == user_id, ReadThrough.work_id == work_id)
    ).all()
    if rows:
        return [Read(r.started_at, r.finished_at, r.id) for r in sorted(rows, key=_key)]
    state = db.get(ReadingState, (user_id, work_id))
    if state is not None and (state.started_at or state.finished_at):
        start = state.started_at
        if start and state.finished_at and start > state.finished_at:
            start = state.finished_at
        return [Read(start, state.finished_at, derived=True)]
    return []


def review_json(db: Session, user: User, work_id: int) -> dict:
    review = db.get(BookReview, (user.id, work_id))
    state = db.get(ReadingState, (user.id, work_id))
    rs = reads(db, user.id, work_id)
    finished = status_of(state.fraction if state else None) == "finished" or any(
        r.finished_at for r in rs
    )
    rating = stars(review.rating) if review else None
    return {
        "work_id": work_id,
        "rating": rating,
        "notes": review.notes if review else "",
        "read_throughs": [r.json() for r in rs],
        "corrected": bool(rs) and not rs[0].derived,
        "finished": finished,
        # "Rate it?": finished, not rated yet, and not dismissed.
        "dismissed": bool(review and review.prompt_dismissed_at),
        "prompt": finished and rating is None and not (review and review.prompt_dismissed_at),
        "updated_at": review.updated_at.isoformat() if review else None,
    }


def _review(db: Session, user_id: int, work_id: int) -> BookReview:
    review = db.get(BookReview, (user_id, work_id))
    if review is None:
        review = BookReview(user_id=user_id, work_id=work_id, notes="")
        db.add(review)
    return review


_UNSET = object()


def save(
    db: Session, user_id: int, work_id: int, *, rating=_UNSET, notes=_UNSET, read_throughs=_UNSET
) -> None:
    """Change what is given. ``read_throughs``: ``[(started "YYYY-MM-DD" | None, finished |
    None), …]`` replaces the person's read-throughs; ``[]`` drops them, so the derived dates
    show again. Caller commits."""
    if rating is not _UNSET:
        half = half_stars(rating)
    if notes is not _UNSET and len(notes) > MAX_NOTES:
        raise ReviewError("too_long", f"Notes are at most {MAX_NOTES:,} characters.")
    if read_throughs is not _UNSET:
        if len(read_throughs) > MAX_READS:
            raise ReviewError("too_many", f"At most {MAX_READS} read-throughs per book.")
        # Tomorrow in UTC is already today east of it.
        latest = at_day(utcnow().date().isoformat()) + timedelta(days=1)
        parsed = []
        for started, finished in read_throughs:
            s, f = at_day(started), at_day(finished)
            if s is None and f is None:
                raise ReviewError("empty_read", "A read-through needs a start or finish date.")
            if s and f and f < s:
                raise ReviewError("bad_order", "A read-through cannot finish before it starts.")
            if any(d and d > latest for d in (s, f)):
                raise ReviewError("future_date", "Dates cannot be in the future.")
            parsed.append((s, f))
    if rating is not _UNSET or notes is not _UNSET:
        review = _review(db, user_id, work_id)
        if rating is not _UNSET:
            review.rating = half
        if notes is not _UNSET:
            review.notes = notes
        review.updated_at = utcnow()
    if read_throughs is not _UNSET:
        for r in db.scalars(
            select(ReadThrough).where(
                ReadThrough.user_id == user_id, ReadThrough.work_id == work_id
            )
        ):
            db.delete(r)
        db.flush()
        for s, f in parsed:
            db.add(ReadThrough(user_id=user_id, work_id=work_id, started_at=s, finished_at=f))


def dismiss_prompt(db: Session, user_id: int, work_id: int) -> None:
    _review(db, user_id, work_id).prompt_dismissed_at = utcnow()


def clear(db: Session, user_id: int, work_id: int) -> None:
    """Forget the rating, notes, and read-throughs (the derived dates show again)."""
    review = db.get(BookReview, (user_id, work_id))
    if review is not None:
        db.delete(review)
    for r in db.scalars(
        select(ReadThrough).where(ReadThrough.user_id == user_id, ReadThrough.work_id == work_id)
    ):
        db.delete(r)


def move(db: Session, source_id: int, target_id: int) -> None:
    """Before merging work ``source_id`` into ``target_id``: each person's review and
    read-throughs follow. A person with a review on both keeps the target's rating and notes,
    each filled from the source's when empty, and both sets of read-throughs."""
    for src in db.scalars(select(BookReview).where(BookReview.work_id == source_id)).all():
        dst = db.get(BookReview, (src.user_id, target_id))
        if dst is None:
            dst = BookReview(user_id=src.user_id, work_id=target_id, notes="")
            db.add(dst)
        dst.rating = dst.rating or src.rating
        dst.notes = dst.notes or src.notes
        dst.prompt_dismissed_at = dst.prompt_dismissed_at or src.prompt_dismissed_at
        db.delete(src)
    for r in db.scalars(select(ReadThrough).where(ReadThrough.work_id == source_id)):
        r.work_id = target_id
    db.flush()


# --- in SQL, for filters and stats -------------------------------------------------------------


def _own(user_id: int):
    return and_(ReadThrough.user_id == user_id, ReadThrough.work_id == Work.id)


def date_pred(user_id: int, which: str, test):
    """SQL on ``Work``: one of the person's started (``which="started"``) or finished dates
    for it passes ``test``: a read-through's, or, with none saved, the reading state's."""
    rt = ReadThrough.started_at if which == "started" else ReadThrough.finished_at
    st = ReadingState.started_at if which == "started" else ReadingState.finished_at
    return or_(
        exists().where(_own(user_id), rt.is_not(None), test(rt)),
        and_(
            not_(exists().where(_own(user_id))),
            exists().where(
                ReadingState.user_id == user_id,
                ReadingState.work_id == Work.id,
                st.is_not(None),
                test(st),
            ),
        ),
    )


def latest_date(user_id: int, which: str):
    """SQL scalar on ``Work``: the person's most recent started or finished date (sorting)."""
    rt = ReadThrough.started_at if which == "started" else ReadThrough.finished_at
    st = ReadingState.started_at if which == "started" else ReadingState.finished_at
    mine = select(func.max(rt)).where(_own(user_id)).scalar_subquery()
    state = (
        select(st)
        .where(ReadingState.user_id == user_id, ReadingState.work_id == Work.id)
        .scalar_subquery()
    )
    return case((exists().where(_own(user_id)), mine), else_=state)


def rating_of(user_id: int):
    """SQL scalar on ``Work``: the person's rating in stars, or NULL."""
    return (
        select(BookReview.rating / 2.0)
        .where(BookReview.user_id == user_id, BookReview.work_id == Work.id)
        .scalar_subquery()
    )


def finished_in(db: Session, user: User, year: int) -> list[dict]:
    """Each time the person finished a book they can see in ``year`` (UTC), by date, with
    their rating: their read-throughs, or, for a work without any, its reading state's first
    finish. A book re-read twice in a year is there twice."""
    start, end = datetime(year, 1, 1, tzinfo=UTC), datetime(year + 1, 1, 1, tzinfo=UTC)
    rows: list[tuple[int, datetime | None, datetime]] = []
    rows += db.execute(
        select(ReadThrough.work_id, ReadThrough.started_at, ReadThrough.finished_at).where(
            ReadThrough.user_id == user.id,
            ReadThrough.finished_at >= start,
            ReadThrough.finished_at < end,
        )
    ).all()
    has_own = select(ReadThrough.work_id).where(ReadThrough.user_id == user.id)
    rows += db.execute(
        select(ReadingState.work_id, ReadingState.started_at, ReadingState.finished_at).where(
            ReadingState.user_id == user.id,
            ReadingState.finished_at >= start,
            ReadingState.finished_at < end,
            ReadingState.work_id.not_in(has_own),
        )
    ).all()
    ids = {w for w, _s, _f in rows}
    works = {w.id: w for w in db.scalars(select(Work).where(Work.id.in_(ids), visible_works(user)))}
    ratings = dict(
        db.execute(
            select(BookReview.work_id, BookReview.rating).where(
                BookReview.user_id == user.id, BookReview.work_id.in_(ids)
            )
        ).all()
    )
    out = [
        {
            "work_id": w,
            "title": works[w].title,
            "author": works[w].author,
            "started_on": day_of(s),
            "finished_on": day_of(f),
            "rating": stars(ratings.get(w)),
        }
        for w, s, f in rows
        if w in works
    ]
    out.sort(key=lambda r: (r["finished_on"], r["title"].lower()))
    return out
