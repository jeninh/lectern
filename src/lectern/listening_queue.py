"""Per-user playback queue operations. Hidden works remain stored but are never returned."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from lectern.access import can_see_work
from lectern.models import AudioTrack, ListeningQueueItem, User, Work


def items(db: Session, user: User) -> list[ListeningQueueItem]:
    return list(
        db.scalars(
            select(ListeningQueueItem)
            .where(ListeningQueueItem.user_id == user.id)
            .order_by(ListeningQueueItem.position, ListeningQueueItem.id)
        )
    )


def enqueue(
    db: Session, user: User, work_id: int, track_id: int | None
) -> ListeningQueueItem | None:
    work = db.get(Work, work_id)
    if work is None or not can_see_work(db, user, work_id):
        return None
    if track_id is not None:
        track = db.get(AudioTrack, track_id)
        if track is None or track.work_id != work_id:
            raise ValueError("track_not_found")
    row = db.scalar(
        select(ListeningQueueItem).where(
            ListeningQueueItem.user_id == user.id,
            ListeningQueueItem.work_id == work_id,
            ListeningQueueItem.track_id == track_id,
        )
    )
    last = db.scalar(
        select(func.coalesce(func.max(ListeningQueueItem.position), -1)).where(
            ListeningQueueItem.user_id == user.id
        )
    )
    if row is None:
        row = ListeningQueueItem(
            user_id=user.id, work_id=work_id, track_id=track_id, position=int(last) + 1
        )
        db.add(row)
    else:
        # Duplicate enqueue is an intentional move-to-end, not another item.
        row.position = int(last) + 1
    db.commit()
    db.refresh(row)
    return row


def remove(db: Session, user: User, item_id: int) -> bool:
    row = db.get(ListeningQueueItem, item_id)
    if row is None or row.user_id != user.id:
        return False
    db.delete(row)
    db.commit()
    return True


def clear(db: Session, user: User) -> None:
    db.query(ListeningQueueItem).filter(ListeningQueueItem.user_id == user.id).delete()
    db.commit()


def reorder(db: Session, user: User, item_ids: list[int]) -> bool:
    current = items(db, user)
    if len(item_ids) != len(current) or set(item_ids) != {x.id for x in current}:
        return False
    # Offset first so the unique ordering can be updated safely on all SQL backends.
    db.execute(
        update(ListeningQueueItem)
        .where(ListeningQueueItem.user_id == user.id)
        .values(position=ListeningQueueItem.position + len(current) + 1)
    )
    for position, item_id in enumerate(item_ids):
        db.execute(
            update(ListeningQueueItem)
            .where(ListeningQueueItem.id == item_id, ListeningQueueItem.user_id == user.id)
            .values(position=position)
        )
    db.commit()
    return True
