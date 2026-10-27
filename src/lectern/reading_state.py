"""How far each person is with each work, whatever they read it on (``reading_states``).

Positions arrive in two tables: ``reading_progress`` (KOReader and the page viewer, keyed by
a file's KOReader document id, with KOReader's own percentage) and ``work_progress`` (the web
reader and the audio player, in sentence coordinates). A ``before_flush`` hook turns every
position written to either into the person's reading state for that work, in the same flush:

- ``fraction``: of the position just written (the latest one wins, as the Library always
  showed the most recently synced file);
- ``last_read_at``: now; ``started_at``: the first time the fraction was above 0;
  ``finished_at``: the first time it reached :data:`FINISHED` (kept if they read it again).

Every write path (KOReader sync and its projection, the page viewer, web reader, audio
player, offline replays) goes through the ORM, so the hook sees all of them. The Library's
reading status and the ``status:``, ``progress:``, ``read:``, ``started:``, and ``finished:``
filters read only this table (``lectern.filters``).
"""

from __future__ import annotations

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from lectern.models import (
    AudioTrack,
    BookFile,
    ProgressSource,
    ReadingProgress,
    ReadingState,
    Sentence,
    WorkProgress,
    utcnow,
)

# frontend/src/pages/Library.tsx FINISHED: at or past this a work counts as finished.
FINISHED = 0.985


def status_of(fraction: float | None) -> str:
    f = fraction or 0.0
    if f <= 0:
        return "unread"
    return "finished" if f >= FINISHED else "reading"


def work_progress_fraction(db: Session, row: WorkProgress) -> float | None:
    """The share of the work a canonical position is at, as ``routes.api.core`` reports it:
    the sentence over the sentence count, or for an audiobook without text the seconds over
    its length."""
    sentences = db.scalar(select(func.count()).where(Sentence.work_id == row.work_id))
    if sentences:
        return min(1.0, row.sentence_n / sentences)
    if row.source == ProgressSource.AUDIO:
        total = db.scalar(
            select(func.sum(AudioTrack.duration_s)).where(AudioTrack.work_id == row.work_id)
        )
        try:
            return max(0.0, min(1.0, float(row.raw) / total)) if total else None
        except ValueError:
            return None
    return None


def record(
    db: Session, user_id: int, work_id: int, fraction: float | None, pending: dict | None = None
) -> None:
    """Update the reading state for a position just written. ``pending``: states added in
    this flush and not yet in the identity map (one flush can write two positions)."""
    now = utcnow()
    pending = {} if pending is None else pending
    state = pending.get((user_id, work_id)) or db.get(ReadingState, (user_id, work_id))
    if state is None:
        state = ReadingState(user_id=user_id, work_id=work_id, fraction=0.0, last_read_at=now)
        db.add(state)
    pending[(user_id, work_id)] = state
    f = max(0.0, min(1.0, fraction or 0.0))
    state.fraction = f
    state.last_read_at = now
    if f > 0 and state.started_at is None:
        state.started_at = now
    if f >= FINISHED and state.finished_at is None:
        state.finished_at = now


def _written(db: Session, cls):
    for obj in list(db.new) + list(db.dirty):
        if isinstance(obj, cls) and (obj in db.new or db.is_modified(obj)):
            yield obj


@event.listens_for(Session, "before_flush")
def _track(db: Session, _ctx, _instances) -> None:
    pending: dict = {}
    with db.no_autoflush:
        # A KOReader-sourced canonical position is kosync's projection of a sync whose own
        # percentage was just recorded from reading_progress (in this flush or the one
        # before it); KOReader's figure stands, not the projected sentence's.
        for wp in list(_written(db, WorkProgress)):
            if wp.source == ProgressSource.KOREADER:
                continue
            if wp.user_id is not None and wp.work_id is not None:
                record(db, wp.user_id, wp.work_id, work_progress_fraction(db, wp), pending)
        for rp in list(_written(db, ReadingProgress)):
            if rp.user_id is None or not rp.document:
                continue
            for work_id in set(
                db.scalars(select(BookFile.work_id).where(BookFile.partial_md5 == rp.document))
            ):
                record(db, rp.user_id, work_id, rp.percentage, pending)
