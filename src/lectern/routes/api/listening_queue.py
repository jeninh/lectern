"""The user-owned listening queue. It deliberately has no OPDS or MCP surface."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from lectern import listening_queue
from lectern.access import can_see_work
from lectern.models import AudioTrack, Work
from lectern.routes.api.core import _audio_summary
from lectern.routes.api.serializers import work_json
from lectern.security.deps import Auth, AuthCsrf, DbDep

router = APIRouter(prefix="/listening-queue")


def view(db, user) -> dict:
    out = []
    stored = listening_queue.items(db, user)
    audio = _audio_summary(db, [item.work_id for item in stored])
    for item in stored:
        # Keep inaccessible rows for when a share returns, but expose no work metadata.
        if not can_see_work(db, user, item.work_id):
            continue
        work = db.get(Work, item.work_id)
        track = db.get(AudioTrack, item.track_id) if item.track_id else None
        if work is None or (item.track_id and (track is None or track.work_id != work.id)):
            continue
        out.append(
            {
                "id": item.id,
                "position": len(out),
                "work": work_json(work, audio=audio.get(work.id)),
                "track_id": item.track_id,
                "track_position": track.position if track else None,
            }
        )
    return {"items": out}


@router.get("")
def get_queue(auth: Auth, db: DbDep) -> dict:
    return view(db, auth.user)


class EnqueueIn(BaseModel):
    work_id: int
    track_id: int | None = None


@router.post("/items", status_code=status.HTTP_201_CREATED)
def add_item(auth: AuthCsrf, db: DbDep, body: EnqueueIn) -> dict:
    try:
        item = listening_queue.enqueue(db, auth.user, body.work_id, body.track_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from None
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return view(db, auth.user)


@router.delete("/items/{item_id}")
def delete_item(auth: AuthCsrf, db: DbDep, item_id: int) -> dict:
    if not listening_queue.remove(db, auth.user, item_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return view(db, auth.user)


@router.delete("")
def clear_queue(auth: AuthCsrf, db: DbDep) -> dict:
    listening_queue.clear(db, auth.user)
    return {"items": []}


class OrderIn(BaseModel):
    item_ids: list[int] = Field(max_length=10_000)


@router.put("/order")
def put_order(auth: AuthCsrf, db: DbDep, body: OrderIn) -> dict:
    if not listening_queue.reorder(db, auth.user, body.item_ids):
        raise HTTPException(status.HTTP_409_CONFLICT, "queue_changed")
    return view(db, auth.user)
