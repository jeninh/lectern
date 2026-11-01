"""Series grouping and "Next in series" (issue #70), over the series metadata on ``Work``.

Series names that differ only in case, accents, punctuation, or a leading/trailing "The" or
trailing "Series" are one series (``series_key``, built on ``ingest.matching.normalize_text``).

**Next in series** is automatic and private to the person: for each series they have started
(any visible book with a reading state that has started), the first book they can see and have
not started, in series order (``series_index``, then ``published``, then title). Series are
listed by the person's latest activity in them. Works they cannot see are never counted or
mentioned: they neither start a series nor appear as the next book nor leave a visible gap.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern.access import visible_works
from lectern.ingest.matching import normalize_text
from lectern.models import ReadingState, User, Work

NEXT_IN_SERIES_LIMIT = 50


def series_key(name: str | None) -> str:
    words = normalize_text(name or "").split()
    if len(words) > 1 and words[0] == "the":
        words = words[1:]
    if len(words) > 1 and words[-1] == "the":
        words = words[:-1]
    if len(words) > 1 and words[-1] == "series":
        words = words[:-1]
    return " ".join(words)


def _order(w: Work) -> tuple:
    return (
        w.series_index is None,
        w.series_index if w.series_index is not None else 0.0,
        w.published or "",
        w.sort_title or "",
        w.id,
    )


def next_in_series_ids(db: Session, user: User, limit: int = NEXT_IN_SERIES_LIMIT) -> list[int]:
    works = db.scalars(select(Work).where(Work.series.is_not(None), visible_works(user))).all()
    states = {
        s.work_id: s
        for s in db.scalars(
            select(ReadingState).where(
                ReadingState.user_id == user.id,
                ReadingState.work_id.in_([w.id for w in works]),
            )
        )
    }
    groups: dict[str, list[Work]] = {}
    for w in works:
        key = series_key(w.series)
        if key:
            groups.setdefault(key, []).append(w)
    found = []
    for members in groups.values():
        started = [
            states[w.id].last_read_at
            for w in members
            if w.id in states and states[w.id].started_at is not None
        ]
        if not started:
            continue
        nxt = next(
            (
                w
                for w in sorted(members, key=_order)
                if w.id not in states or states[w.id].started_at is None
            ),
            None,
        )
        if nxt is not None:
            found.append((max(started), nxt.id))
    found.sort(key=lambda x: x[0], reverse=True)
    return [wid for _, wid in found][:limit]
