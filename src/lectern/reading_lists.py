"""Per-person reading lists shared by the web app and the OPDS catalogue.

- **Continue**: works the person is reading or listening to, most recent activity first. A
  work is in it when it has a KOReader progress record (any of its EPUB files' document hash)
  or a canonical position (web reader, read-along, audio player). Activity time is the newer
  of the two. This is exactly ``GET /api/continue``.
- **Up next**: works on the person's shelves and the shelves inside them (their own, then
  those shared with them, by name; within a shelf in shelf order) that they have not
  started, i.e. that are in neither source of Continue.

Both only ever contain works the person can see (``lectern.access``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern.access import own_shelf_ids, visible_works
from lectern.models import (
    BookFile,
    ReadingProgress,
    Shelf,
    ShelfItem,
    SyncKey,
    User,
    Work,
    WorkProgress,
)

UP_NEXT_LIMIT = 200


def continue_items(db: Session, user: User, limit: int) -> list[tuple[int, str, tuple | None]]:
    """``(work_id, latest, koreader)`` in Continue order; ``latest`` is ``"koreader"`` or
    ``"canonical"``, ``koreader`` the newest ``(ReadingProgress, SyncKey, BookFile)`` row."""
    rows = db.execute(
        select(ReadingProgress, SyncKey, BookFile)
        .join(BookFile, BookFile.partial_md5 == ReadingProgress.document)
        .outerjoin(SyncKey, SyncKey.id == ReadingProgress.sync_key_id)
        .where(ReadingProgress.user_id == user.id)
        .order_by(ReadingProgress.updated_at.desc())
        .limit(limit * 2)
    ).all()
    latest: dict[int, tuple[datetime, str]] = {}
    ko: dict[int, tuple] = {}
    for p, key, f in rows:
        if f.work_id in ko:
            continue
        ko[f.work_id] = (p, key, f)
        latest[f.work_id] = (p.updated_at, "koreader")
    canon = db.scalars(
        select(WorkProgress)
        .where(WorkProgress.user_id == user.id)
        .order_by(WorkProgress.received_at.desc())
        .limit(limit * 2)
    ).all()
    for r in canon:
        at = r.received_at
        if r.work_id not in latest or at > latest[r.work_id][0]:
            latest[r.work_id] = (at, "canonical")
    ordered = sorted(latest.items(), key=lambda kv: kv[1][0], reverse=True)
    ids = [w for w, _ in ordered]
    visible = set(db.scalars(select(Work.id).where(Work.id.in_(ids), visible_works(user))))
    return [(w, latest[w][1], ko.get(w)) for w in ids if w in visible][:limit]


def up_next_ids(db: Session, user: User, limit: int = UP_NEXT_LIMIT) -> list[int]:
    started_ko = (
        select(BookFile.work_id)
        .join(ReadingProgress, ReadingProgress.document == BookFile.partial_md5)
        .where(ReadingProgress.user_id == user.id)
    )
    started = select(WorkProgress.work_id).where(WorkProgress.user_id == user.id)
    rows = db.execute(
        select(ShelfItem.work_id)
        .join(Shelf, Shelf.id == ShelfItem.shelf_id)
        .join(Work, Work.id == ShelfItem.work_id)
        .where(
            Shelf.id.in_(own_shelf_ids(user)),
            visible_works(user),
            ShelfItem.work_id.not_in(started_ko),
            ShelfItem.work_id.not_in(started),
        )
        .order_by(
            (Shelf.owner_id != user.id),
            Shelf.name,
            Shelf.id,
            ShelfItem.position,
            ShelfItem.added_at,
        )
    ).all()
    out: list[int] = []
    seen: set[int] = set()
    for (work_id,) in rows:
        if work_id not in seen:
            seen.add(work_id)
            out.append(work_id)
            if len(out) >= limit:
                break
    return out
