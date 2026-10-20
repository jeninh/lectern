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

Concurrency
    Enqueue is one ``INSERT … ON CONFLICT DO UPDATE … WHERE status <> 'PENDING'`` per work, so
    two requests racing to queue the same book leave one pending row; delivery is one
    ``UPDATE … WHERE status = 'PENDING'``, so two complete downloads deliver (and audit) once.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from lectern.access import can_see_work, visible_works
from lectern.models import (
    AppPassword,
    BookFile,
    DeviceQueueItem,
    FileKind,
    QueueStatus,
    Role,
    User,
    Work,
    utcnow,
)
from lectern.security import audit

MAX_BATCH = 200
HISTORY_LIMIT = 50


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


def mark_delivered(pw_id: int, file_id: int, ip: str | None) -> bool:
    """Called when device ``pw_id`` was sent every byte of EPUB ``file_id``: its pending item
    for that work, if any, becomes delivered. Atomic (one conditional ``UPDATE``) and with its
    own session, since it runs after the response body, outside the request's session."""
    from lectern.db import session_factory

    with session_factory()() as db:
        book = db.get(BookFile, file_id)
        if book is None or book.kind != FileKind.EBOOK:
            return False
        now = utcnow()
        row = db.execute(
            update(DeviceQueueItem)
            .where(
                DeviceQueueItem.app_password_id == pw_id,
                DeviceQueueItem.work_id == book.work_id,
                DeviceQueueItem.status == QueueStatus.PENDING.name,
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


def _device_detail(t: Target) -> str:
    return f"device {t.pw.label} ({t.owner.username})"
