"""Per-user "Want to read" queue (issue #70): manual only, never filled from series or shelves.

Separate from the automatic Next in series list (``lectern.series``), Up next on shelves
(``lectern.reading_lists``), and the playback queue (``lectern.listening_queue``). Works the
person cannot see stay stored (they return if access does) but are never listed, and their
existence is never revealed.
"""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from lectern.access import can_see_work, visible_works
from lectern.models import User, WantToReadItem, Work


def work_ids(db: Session, user: User) -> list[int]:
    """Visible queued works, in the person's order."""
    return list(
        db.scalars(
            select(WantToReadItem.work_id)
            .join(Work, Work.id == WantToReadItem.work_id)
            .where(WantToReadItem.user_id == user.id, visible_works(user))
            .order_by(WantToReadItem.position, WantToReadItem.id)
        )
    )


def add(db: Session, user: User, work_id: int) -> bool:
    """Append; adding a work already queued keeps its place. False if the work is not visible."""
    if not can_see_work(db, user, work_id):
        return False
    if db.scalar(
        select(WantToReadItem.id).where(
            WantToReadItem.user_id == user.id, WantToReadItem.work_id == work_id
        )
    ):
        return True
    last = db.scalar(
        select(func.coalesce(func.max(WantToReadItem.position), -1)).where(
            WantToReadItem.user_id == user.id
        )
    )
    db.add(WantToReadItem(user_id=user.id, work_id=work_id, position=int(last) + 1))
    return True


def remove(db: Session, user: User, work_id: int) -> None:
    db.query(WantToReadItem).filter(
        WantToReadItem.user_id == user.id, WantToReadItem.work_id == work_id
    ).delete()


def reorder(db: Session, user: User, ordered: list[int]) -> bool:
    """Put the visible queue in ``ordered`` (which must be exactly that set); hidden entries
    follow, in their old order."""
    current = work_ids(db, user)
    if len(ordered) != len(current) or set(ordered) != set(current):
        return False
    rows = db.scalars(
        select(WantToReadItem)
        .where(WantToReadItem.user_id == user.id)
        .order_by(WantToReadItem.position, WantToReadItem.id)
    ).all()
    hidden = [r.work_id for r in rows if r.work_id not in set(current)]
    final = ordered + hidden
    db.execute(
        update(WantToReadItem)
        .where(WantToReadItem.user_id == user.id)
        .values(position=WantToReadItem.position + len(final) + 1)
    )
    for position, wid in enumerate(final):
        db.execute(
            update(WantToReadItem)
            .where(WantToReadItem.user_id == user.id, WantToReadItem.work_id == wid)
            .values(position=position)
        )
    return True
