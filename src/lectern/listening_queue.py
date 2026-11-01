"""Per-user playback queue operations. Hidden works remain stored but are never returned."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from lectern.access import can_see_work
from lectern.models import AudioTrack, ListeningQueueItem, ListeningQueueState, User, Work


def items(db: Session, user: User) -> list[ListeningQueueItem]:
    return list(
        db.scalars(
            select(ListeningQueueItem)
            .where(ListeningQueueItem.user_id == user.id)
            .order_by(ListeningQueueItem.position, ListeningQueueItem.id)
        )
    )


def version(db: Session, user: User) -> int:
    state = db.get(ListeningQueueState, user.id)
    return state.version if state else 0


def advance_version(db: Session, user: User, expected: int) -> bool:
    """Atomically take one version. The caller commits its item change with this update."""
    state = db.get(ListeningQueueState, user.id)
    if state is None:
        if expected != 0:
            return False
        state = ListeningQueueState(user_id=user.id, version=0)
        db.add(state)
        db.flush()
    result = db.execute(
        update(ListeningQueueState)
        .where(ListeningQueueState.user_id == user.id, ListeningQueueState.version == expected)
        .values(version=expected + 1)
    )
    return result.rowcount == 1


def enqueue(
    db: Session,
    user: User,
    work_id: int,
    track_id: int | None,
    chapter_start_s: float | None = None,
    chapter_end_s: float | None = None,
) -> ListeningQueueItem | None:
    work = db.get(Work, work_id)
    if work is None or not can_see_work(db, user, work_id):
        return None
    if (chapter_start_s is None) != (chapter_end_s is None):
        raise ValueError("chapter_bounds_required")
    if track_id is not None:
        track = db.get(AudioTrack, track_id)
        if track is None or track.work_id != work_id:
            raise ValueError("track_not_found")
        if chapter_start_s is not None and not (
            0 <= chapter_start_s < chapter_end_s <= track.duration_s
        ):
            raise ValueError("chapter_bounds_invalid")
    elif chapter_start_s is not None:
        raise ValueError("track_not_found")
    row = db.scalar(
        select(ListeningQueueItem).where(
            ListeningQueueItem.user_id == user.id,
            ListeningQueueItem.work_id == work_id,
            ListeningQueueItem.track_id == track_id,
            ListeningQueueItem.chapter_start_s == chapter_start_s,
        )
    )
    last = db.scalar(
        select(func.coalesce(func.max(ListeningQueueItem.position), -1)).where(
            ListeningQueueItem.user_id == user.id
        )
    )
    if row is None:
        row = ListeningQueueItem(
            user_id=user.id,
            work_id=work_id,
            track_id=track_id,
            chapter_start_s=chapter_start_s,
            chapter_end_s=chapter_end_s,
            position=int(last) + 1,
        )
        db.add(row)
    else:
        # Duplicate enqueue is an intentional move-to-end, not another item.
        row.position = int(last) + 1
    return row


def remove(db: Session, user: User, item_id: int) -> bool:
    row = db.get(ListeningQueueItem, item_id)
    if row is None or row.user_id != user.id:
        return False
    db.delete(row)
    return True


def clear(db: Session, user: User) -> None:
    db.query(ListeningQueueItem).filter(ListeningQueueItem.user_id == user.id).delete()


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
    return True
