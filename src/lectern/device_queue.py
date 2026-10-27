"""Per-device queues: "send this book to my Kobo" (docs/device-queues.md).

A queue belongs to one OPDS device, i.e. one app password. Its pending works appear in that
credential's ``/opds/queue`` feed, and nowhere else: another device of the same person, or
anyone else's device, never sees them. An item becomes ``delivered`` the moment that exact
device has been sent the whole EPUB of the work (``lectern.http_range``'s ``on_complete``).

Who may queue to which device
    Everyone may queue to their own active devices; the owner may also queue to anyone's
    (household administration). A work can be queued only if the person queueing can see it
    *and* the device's user can: an owner cannot push a book onto a reader's e-reader that is
    not on a shelf shared with that reader. Visibility is checked again whenever the queue is
    read, so unsharing a shelf hides its works from the feed and the queue list at once.
    Members cannot see or touch other people's devices; to them those ids do not exist (404).

Following a shelf
    A device may follow one shelf (manual with its sub-shelves, or smart): :func:`sync_follow`
    queues the shelf's books for it, as its user sees them, whenever the device reads its
    queue, so a book put on the shelf or a changed smart-shelf rule reaches the device at its
    next sync. At most :data:`FOLLOW_WINDOW` followed books wait at once, so a large shelf
    trickles in. With clean-up on, followed books that leave the shelf are dequeued (removed
    with ``removed_by`` NULL) and listed by :func:`cleanup_work_ids` for the KOReader plugin,
    which deletes only the files it downloaded itself. A book a person took off the queue is
    never queued again by the shelf; one sent by hand is never cleaned up.

Concurrency
    Enqueue is one ``INSERT … ON CONFLICT DO UPDATE … WHERE status <> 'PENDING'`` per work, so
    two requests racing to queue the same book leave one pending row; delivery is one
    ``UPDATE … WHERE status = 'PENDING'``, so two complete downloads deliver (and audit) once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from lectern import filters
from lectern.access import can_see_work, visible_works
from lectern.models import (
    AppPassword,
    BookFile,
    DeviceFollow,
    DeviceQueueItem,
    FileKind,
    QueueStatus,
    Role,
    Shelf,
    User,
    Work,
    utcnow,
)
from lectern.security import audit

MAX_BATCH = 200
HISTORY_LIMIT = 50
# Followed books waiting at once: a large shelf is queued a batch at a time, as the device
# downloads what is already waiting, so neither the Pi nor the e-reader takes it in one go.
FOLLOW_WINDOW = 25
# Works the plugin is told to delete, newest first (it ignores those it did not download).
CLEANUP_LIMIT = 200


@dataclass
class Target:
    """An app password someone may queue to, with its owner."""

    pw: AppPassword
    owner: User


def may_manage(actor: User, pw: AppPassword) -> bool:
    return pw.user_id == actor.id or actor.role == Role.OWNER


def targets(db: Session, actor: User) -> list[Target]:
    """Active OPDS devices ``actor`` may queue to: their own, plus everyone's for the owner."""
    stmt = (
        select(AppPassword, User)
        .join(User, User.id == AppPassword.user_id)
        .where(AppPassword.revoked_at.is_(None), User.is_active.is_(True))
    )
    if actor.role != Role.OWNER:
        stmt = stmt.where(AppPassword.user_id == actor.id)
    rows = db.execute(stmt.order_by(AppPassword.created_at.desc(), AppPassword.id.desc())).all()
    out = [Target(pw, u) for pw, u in rows]
    out.sort(key=lambda t: (t.owner.id != actor.id, t.owner.username))  # own devices first
    return out


def target(db: Session, actor: User, pw_id: int, *, active: bool = True) -> Target | None:
    """The device if ``actor`` may manage it (and, with ``active``, it can still connect)."""
    pw = db.get(AppPassword, pw_id)
    if pw is None or not may_manage(actor, pw):
        return None
    owner = db.get(User, pw.user_id)
    if owner is None or (active and (pw.revoked_at is not None or not owner.is_active)):
        return None
    return Target(pw, owner)


def _has_ebook(db: Session, work_id: int) -> bool:
    return (
        db.scalar(
            select(func.count())
            .select_from(BookFile)
            .where(BookFile.work_id == work_id, BookFile.kind == FileKind.EBOOK)
        )
        or 0
    ) > 0


def enqueue(db: Session, actor: User, t: Target, work_ids: list[int], ip: str | None) -> list[dict]:
    """Queue works for the device; one result per requested id, in request order:
    ``queued`` (new), ``requeued`` (was delivered or removed), ``already_queued``,
    ``not_found`` (no such work, or the actor cannot see it), ``not_visible_to_device`` (the
    device's user cannot see it), or ``no_ebook``. Commits."""
    results: list[dict] = []
    seen: set[int] = set()
    for work_id in work_ids:
        if work_id in seen:
            continue
        seen.add(work_id)
        work = db.get(Work, work_id)
        if work is None or not can_see_work(db, actor, work_id):
            results.append({"work_id": work_id, "status": "not_found"})
            continue
        if not can_see_work(db, t.owner, work_id):
            results.append({"work_id": work_id, "status": "not_visible_to_device"})
            continue
        if not _has_ebook(db, work_id):
            results.append({"work_id": work_id, "status": "no_ebook"})
            continue
        before = db.scalar(
            select(DeviceQueueItem.status).where(
                DeviceQueueItem.app_password_id == t.pw.id, DeviceQueueItem.work_id == work_id
            )
        )
        now = utcnow()
        next_pos = (
            select(func.coalesce(func.max(DeviceQueueItem.position), 0) + 1)
            .where(DeviceQueueItem.app_password_id == t.pw.id)
            .scalar_subquery()
        )
        stmt = insert(DeviceQueueItem).values(
            app_password_id=t.pw.id,
            user_id=t.pw.user_id,
            work_id=work_id,
            status=QueueStatus.PENDING.name,
            position=next_pos,
            queued_by=actor.id,
            queued_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["app_password_id", "work_id"],
            set_={
                "status": QueueStatus.PENDING.name,
                "position": next_pos,
                "queued_by": actor.id,
                "followed": False,
                "queued_at": now,
                "delivered_at": None,
                "delivered_file_id": None,
                "removed_at": None,
                "removed_by": None,
                "updated_at": now,
            },
            where=DeviceQueueItem.status != QueueStatus.PENDING.name,
        )
        changed = db.execute(stmt).rowcount
        if not changed:
            results.append({"work_id": work_id, "status": "already_queued"})
            continue
        status = "queued" if before is None else "requeued"
        audit.record(
            db,
            "queue.add",
            actor_user_id=actor.id,
            target=f"work:{work_id}",
            ip=ip,
            detail=_device_detail(t),
        )
        results.append({"work_id": work_id, "status": status})
    db.commit()
    return results


def remove(db: Session, actor: User, t: Target, item_id: int, ip: str | None) -> bool | None:
    """Take a pending item off the queue. ``None`` if there is no such item on this device,
    ``False`` if it was not pending (nothing changes), ``True`` once removed. Commits."""
    item = db.get(DeviceQueueItem, item_id)
    if item is None or item.app_password_id != t.pw.id:
        return None
    now = utcnow()
    changed = db.execute(
        update(DeviceQueueItem)
        .where(DeviceQueueItem.id == item_id, DeviceQueueItem.status == QueueStatus.PENDING.name)
        .values(
            status=QueueStatus.REMOVED.name, removed_at=now, removed_by=actor.id, updated_at=now
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed:
        audit.record(
            db,
            "queue.remove",
            actor_user_id=actor.id,
            target=f"work:{item.work_id}",
            ip=ip,
            detail=_device_detail(t),
        )
    db.commit()
    return bool(changed)


def reorder(db: Session, t: Target, item_ids: list[int]) -> bool:
    """Put the device's pending items in this order. ``item_ids`` must be exactly the pending
    items the caller can see (``pending_query``); ``False`` (nothing changes) when the queue
    moved on in the meantime. Commits."""
    pending = [i.id for i, _ in db.execute(pending_query(t.pw.id, t.owner)).all()]
    if sorted(pending) != sorted(item_ids) or len(set(item_ids)) != len(item_ids):
        return False
    base = (
        db.scalar(
            select(func.max(DeviceQueueItem.position)).where(
                DeviceQueueItem.app_password_id == t.pw.id
            )
        )
        or 0
    )
    now = utcnow()
    for i, item_id in enumerate(item_ids, start=1):
        db.execute(
            update(DeviceQueueItem)
            .where(DeviceQueueItem.id == item_id)
            .values(position=base + i, updated_at=now)
            .execution_options(synchronize_session=False)
        )
    db.commit()
    return True


def pending_query(pw_id: int, owner: User):
    """Pending items of one device that its user may still see, in feed order."""
    return (
        select(DeviceQueueItem, Work)
        .join(Work, Work.id == DeviceQueueItem.work_id)
        .where(
            DeviceQueueItem.app_password_id == pw_id,
            DeviceQueueItem.status == QueueStatus.PENDING.name,
            visible_works(owner),
        )
        .order_by(DeviceQueueItem.position, DeviceQueueItem.id)
    )


def pending_count(db: Session, pw_id: int, owner: User) -> int:
    return db.scalar(select(func.count()).select_from(pending_query(pw_id, owner).subquery())) or 0


def history(db: Session, pw_id: int, owner: User) -> list[tuple[DeviceQueueItem, Work]]:
    """Recently delivered or removed items (newest first), for queueing again."""
    return list(
        db.execute(
            select(DeviceQueueItem, Work)
            .join(Work, Work.id == DeviceQueueItem.work_id)
            .where(
                DeviceQueueItem.app_password_id == pw_id,
                DeviceQueueItem.status != QueueStatus.PENDING.name,
                visible_works(owner),
            )
            .order_by(DeviceQueueItem.updated_at.desc(), DeviceQueueItem.id.desc())
            .limit(HISTORY_LIMIT)
        ).all()
    )


def pending_devices(db: Session, owner_ids: list[int], work_id: int) -> set[int]:
    """App password ids with ``work_id`` pending (for "already queued" in the send dialog)."""
    if not owner_ids:
        return set()
    return set(
        db.scalars(
            select(DeviceQueueItem.app_password_id).where(
                DeviceQueueItem.work_id == work_id,
                DeviceQueueItem.status == QueueStatus.PENDING.name,
                DeviceQueueItem.user_id.in_(owner_ids),
            )
        )
    )


def pending_item(db: Session, pw_id: int, work_id: int) -> tuple[int, datetime] | None:
    """``(item id, queued_at)`` of the work's pending item on this device, if any: taken when a
    download starts, so that only that queueing of the work can be delivered by it."""
    row = db.execute(
        select(DeviceQueueItem.id, DeviceQueueItem.queued_at).where(
            DeviceQueueItem.app_password_id == pw_id,
            DeviceQueueItem.work_id == work_id,
            DeviceQueueItem.status == QueueStatus.PENDING.name,
        )
    ).first()
    return (row[0], row[1]) if row else None


def mark_delivered(pw_id: int, file_id: int, ip: str | None, item: tuple[int, datetime]) -> bool:
    """Called when device ``pw_id`` was sent every byte of EPUB ``file_id`` in a response that
    started while ``item`` (:func:`pending_item`) was pending: that item becomes delivered if it
    is still pending and was not removed and queued again meanwhile (a new ``queued_at``), so a
    transfer that began before the current queueing never completes it. Atomic (one
    conditional ``UPDATE``) and with its own session, since it runs after the response body,
    outside the request's session."""
    from lectern.db import session_factory

    item_id, queued_at = item
    with session_factory()() as db:
        book = db.get(BookFile, file_id)
        if book is None or book.kind != FileKind.EBOOK:
            return False
        now = utcnow()
        row = db.execute(
            update(DeviceQueueItem)
            .where(
                DeviceQueueItem.id == item_id,
                DeviceQueueItem.app_password_id == pw_id,
                DeviceQueueItem.work_id == book.work_id,
                DeviceQueueItem.status == QueueStatus.PENDING.name,
                DeviceQueueItem.queued_at == queued_at,
            )
            .values(
                status=QueueStatus.DELIVERED.name,
                delivered_at=now,
                delivered_file_id=file_id,
                updated_at=now,
            )
            .returning(DeviceQueueItem.user_id)
            .execution_options(synchronize_session=False)
        ).first()
        if row is None:
            db.rollback()
            return False
        pw = db.get(AppPassword, pw_id)
        audit.record(
            db,
            "queue.delivered",
            actor_user_id=row[0],
            target=f"work:{book.work_id}",
            ip=ip,
            detail=f"device {pw.label if pw else pw_id}",
        )
        db.commit()
        return True


def _next_position(pw_id: int):
    return (
        select(func.coalesce(func.max(DeviceQueueItem.position), 0) + 1)
        .where(DeviceQueueItem.app_password_id == pw_id)
        .scalar_subquery()
    )


def shelf_work_ids(db: Session, owner: User, shelf: Shelf) -> list[int] | None:
    """Works on ``shelf`` as ``owner`` sees it, with a book file, in the shelf's order: a
    smart shelf's matches, else the books on it and on every shelf nested below it (as the
    ``shelf:`` filter). A shelf the owner cannot see holds nothing. ``None`` for a smart shelf
    whose rule this version cannot read, so a follow never empties a device by mistake."""
    if shelf.rule is not None and filters.rule_query(shelf) is None:
        return None
    ctx = filters.Context(db, owner, strict=False)
    clause = filters.Clause("shelf", (filters.Value("=", f"#{shelf.id}"),))
    order = filters.smart_order(shelf, owner) if shelf.rule else filters.order_by("title", owner)
    return list(
        db.scalars(
            select(Work.id)
            .where(
                visible_works(owner),
                filters.clause_pred(ctx, clause),
                exists().where(BookFile.work_id == Work.id, BookFile.kind == FileKind.EBOOK),
            )
            .order_by(*order)
        )
    )


def sync_follow(db: Session, pw_id: int) -> tuple[int, int]:
    """Bring the device's queue in line with the shelf it follows: queue new books (up to
    :data:`FOLLOW_WINDOW` followed books waiting) and, with clean-up on, dequeue followed books
    that left it. Returns ``(queued, removed)``; commits when anything changed. Safe to run
    concurrently: inserts and removals are conditional, as in :func:`enqueue`."""
    pw = db.get(AppPassword, pw_id)
    follow = db.get(DeviceFollow, pw_id)
    if pw is None or follow is None or pw.revoked_at is not None:
        return 0, 0
    owner = db.get(User, pw.user_id)
    shelf = db.get(Shelf, follow.shelf_id)
    if owner is None or not owner.is_active or shelf is None:
        return 0, 0
    members = shelf_work_ids(db, owner, shelf)
    if members is None:
        return 0, 0
    wanted = set(members)
    rows = {
        r.work_id: (r.id, r.status, r.followed, r.removed_by)
        for r in db.execute(
            select(
                DeviceQueueItem.id,
                DeviceQueueItem.work_id,
                DeviceQueueItem.status,
                DeviceQueueItem.followed,
                DeviceQueueItem.removed_by,
            ).where(DeviceQueueItem.app_password_id == pw.id)
        )
    }
    now = utcnow()
    removed = 0
    if follow.cleanup:
        for work_id, (item_id, status, followed, _by) in rows.items():
            if not followed or work_id in wanted or status == QueueStatus.REMOVED:
                continue
            removed += db.execute(
                update(DeviceQueueItem)
                .where(
                    DeviceQueueItem.id == item_id,
                    DeviceQueueItem.followed.is_(True),
                    DeviceQueueItem.status != QueueStatus.REMOVED.name,
                )
                .values(
                    status=QueueStatus.REMOVED.name, removed_at=now, removed_by=None, updated_at=now
                )
                .execution_options(synchronize_session=False)
            ).rowcount
    waiting = sum(
        1
        for work_id, (_id, status, followed, _by) in rows.items()
        if followed and status == QueueStatus.PENDING and work_id in wanted
    )
    queued = 0
    for work_id in members:
        if waiting + queued >= FOLLOW_WINDOW:
            break
        row = rows.get(work_id)
        if row is None:
            stmt = (
                insert(DeviceQueueItem)
                .values(
                    app_password_id=pw.id,
                    user_id=pw.user_id,
                    work_id=work_id,
                    status=QueueStatus.PENDING.name,
                    position=_next_position(pw.id),
                    queued_by=None,
                    queued_at=now,
                    updated_at=now,
                    followed=True,
                )
                .on_conflict_do_nothing(index_elements=["app_password_id", "work_id"])
            )
        elif row[1] == QueueStatus.REMOVED and row[2] and row[3] is None:
            # Left the shelf (clean-up) and came back: queue it again.
            stmt = (
                update(DeviceQueueItem)
                .where(
                    DeviceQueueItem.id == row[0],
                    DeviceQueueItem.status == QueueStatus.REMOVED.name,
                    DeviceQueueItem.followed.is_(True),
                    DeviceQueueItem.removed_by.is_(None),
                )
                .values(
                    status=QueueStatus.PENDING.name,
                    position=_next_position(pw.id),
                    queued_at=now,
                    delivered_at=None,
                    delivered_file_id=None,
                    removed_at=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
        else:
            continue  # waiting, delivered, sent by hand, or taken off the queue by a person
        queued += db.execute(stmt).rowcount
    if queued or removed:
        audit.record(
            db,
            "queue.follow",
            actor_user_id=owner.id,
            target=f"shelf:{shelf.id}",
            detail=f"device {pw.label}: {queued} queued, {removed} removed",
        )
        db.commit()
    return queued, removed


def cleanup_work_ids(db: Session, pw: AppPassword) -> list[int]:
    """Works that left the followed shelf with clean-up on (newest first): the KOReader plugin
    deletes its own downloads of them. Empty while clean-up is off."""
    follow = db.get(DeviceFollow, pw.id)
    if follow is None or not follow.cleanup:
        return []
    return list(
        db.scalars(
            select(DeviceQueueItem.work_id)
            .where(
                DeviceQueueItem.app_password_id == pw.id,
                DeviceQueueItem.followed.is_(True),
                DeviceQueueItem.status == QueueStatus.REMOVED.name,
                DeviceQueueItem.removed_by.is_(None),
            )
            .order_by(DeviceQueueItem.removed_at.desc(), DeviceQueueItem.id.desc())
            .limit(CLEANUP_LIMIT)
        )
    )


def _device_detail(t: Target) -> str:
    return f"device {t.pw.label} ({t.owner.username})"
