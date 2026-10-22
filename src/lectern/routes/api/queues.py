"""Per-device queues for the web app (lectern.device_queue has the rules).

- ``GET  /api/device-queues[?work_id=]``: OPDS devices the caller may queue to, with their
  pending counts (and, with ``work_id``, whether that work is already pending on each).
- ``GET  /api/device-queues/{app_password_id}``: the queue: pending items in feed order and
  recent history (delivered and removed, newest first).
- ``POST /api/device-queues/{app_password_id}/items {"work_ids": [...]}``: queue (idempotent).
- ``DELETE /api/device-queues/{app_password_id}/items/{item_id}``: take a pending item off.
- ``POST /api/device-queues/{app_password_id}/order {"item_ids": [...]}``: reorder.

A device the caller may not manage answers 404, as does an unknown id.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from lectern import device_queue
from lectern.access import can_see_work
from lectern.covergen import cover_fields
from lectern.models import DeviceQueueItem, User, Work
from lectern.routes.api.selection import Selection
from lectern.routes.api.selection import resolve as resolve_selection
from lectern.routes.api.serializers import iso
from lectern.security.deps import Auth, AuthCsrf, DbDep
from lectern.security.sessions import client_ip

router = APIRouter(prefix="/device-queues")


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


def _device_json(db, t: device_queue.Target, viewer: User) -> dict:
    pw = t.pw
    return {
        "id": pw.id,
        # the Devices page's id for the paired device (sync key) or the lone app password
        "device_id": f"k{pw.sync_key_id}" if pw.sync_key_id else f"p{pw.id}",
        "label": pw.label,
        "own": pw.user_id == viewer.id,
        "user": {
            "id": t.owner.id,
            "username": t.owner.username,
            "display_name": t.owner.display_name,
        },
        "last_used_at": iso(pw.last_used_at),
        "revoked": pw.revoked_at is not None,
        "pending": device_queue.pending_count(db, pw.id, t.owner),
    }


def _item_json(item: DeviceQueueItem, work: Work, names: dict[int, str]) -> dict:
    return {
        "id": item.id,
        "status": item.status.value,
        "position": item.position,
        "queued_at": iso(item.queued_at),
        "queued_by": names.get(item.queued_by or -1),
        "delivered_at": iso(item.delivered_at),
        "removed_at": iso(item.removed_at),
        "removed_by": names.get(item.removed_by or -1),
        "work": {
            "id": work.id,
            "title": work.title,
            "author": work.author,
            **cover_fields(work),
        },
    }


@router.get("")
def list_targets(auth: Auth, db: DbDep, work_id: int | None = None) -> dict:
    found = device_queue.targets(db, auth.user)
    out = [_device_json(db, t, auth.user) for t in found]
    if work_id is not None:
        # a work the caller cannot see is pending nowhere, so its id reveals nothing
        queued = (
            device_queue.pending_devices(db, [t.owner.id for t in found], work_id)
            if can_see_work(db, auth.user, work_id)
            else set()
        )
        for d in out:
            d["queued"] = d["id"] in queued
    return {"devices": out}


@router.get("/{pw_id}")
def show(auth: Auth, db: DbDep, pw_id: int) -> dict:
    t = device_queue.target(db, auth.user, pw_id, active=False)
    if t is None:
        raise _not_found()
    pending = db.execute(device_queue.pending_query(pw_id, t.owner)).all()
    past = device_queue.history(db, pw_id, t.owner)
    people = {i.queued_by for i, _ in pending + past if i.queued_by} | {
        i.removed_by for i, _ in past if i.removed_by
    }
    names = {u.id: u.username for u in (db.get(User, uid) for uid in people) if u is not None}
    return {
        "device": _device_json(db, t, auth.user),
        "pending": [_item_json(i, w, names) for i, w in pending],
        "history": [_item_json(i, w, names) for i, w in past],
    }


class EnqueueIn(BaseModel):
    work_ids: list[int] = Field(default_factory=list, max_length=device_queue.MAX_BATCH)
    selection: Selection | None = None


@router.post("/{pw_id}/items")
def enqueue(request: Request, auth: AuthCsrf, db: DbDep, pw_id: int, body: EnqueueIn) -> dict:
    t = device_queue.target(db, auth.user, pw_id)
    if t is None:
        raise _not_found()
    ids = resolve_selection(
        db, auth.user, body.work_ids, body.selection, limit=device_queue.MAX_BATCH
    )
    results = device_queue.enqueue(db, auth.user, t, ids, client_ip(request))
    return {"results": results, "device": _device_json(db, t, auth.user)}


@router.delete("/{pw_id}/items/{item_id}")
def remove(request: Request, auth: AuthCsrf, db: DbDep, pw_id: int, item_id: int) -> dict:
    t = device_queue.target(db, auth.user, pw_id, active=False)
    if t is None:
        raise _not_found()
    removed = device_queue.remove(db, auth.user, t, item_id, client_ip(request))
    if removed is None:
        raise _not_found()
    return {"removed": removed, "device": _device_json(db, t, auth.user)}


class OrderIn(BaseModel):
    item_ids: list[int] = Field(max_length=10_000)


@router.post("/{pw_id}/order")
def reorder(auth: AuthCsrf, db: DbDep, pw_id: int, body: OrderIn) -> dict:
    t = device_queue.target(db, auth.user, pw_id, active=False)
    if t is None:
        raise _not_found()
    if not device_queue.reorder(db, t, body.item_ids):
        raise HTTPException(status.HTTP_409_CONFLICT, "queue_changed")
    return {"ok": True}
