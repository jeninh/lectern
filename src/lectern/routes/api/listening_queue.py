"""The user-owned listening queue. It deliberately has no OPDS or MCP surface."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from lectern import idempotency, listening_queue
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
                "chapter_start_s": track.offset_s + item.chapter_start_s
                if track and item.chapter_start_s is not None
                else None,
                "chapter_end_s": track.offset_s + item.chapter_end_s
                if track and item.chapter_end_s is not None
                else None,
            }
        )
    return {"version": listening_queue.version(db, user), "items": out}


@router.get("")
def get_queue(auth: Auth, db: DbDep) -> dict:
    return view(db, auth.user)


class MutationIn(BaseModel):
    """One replay-safe queue edit. ``base_version`` prevents stale writes from winning."""

    base_version: int = Field(ge=0)
    operation: Literal["enqueue", "remove", "clear", "reorder"]
    work_id: int | None = None
    track_id: int | None = None
    chapter_start_s: float | None = Field(default=None, ge=0)
    chapter_end_s: float | None = Field(default=None, gt=0)
    item_id: int | None = None
    # Clear receives the list the person saw, so a stale clear never erases a later enqueue.
    item_ids: list[int] | None = Field(default=None, max_length=10_000)


def _apply(db, user, body: MutationIn) -> None:
    if body.operation == "enqueue":
        if body.work_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "work_id_required")
        try:
            item = listening_queue.enqueue(
                db,
                user,
                body.work_id,
                body.track_id,
                body.chapter_start_s,
                body.chapter_end_s,
            )
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from None
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    elif body.operation == "remove":
        if body.item_id is None or not listening_queue.remove(db, user, body.item_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    elif body.operation == "clear":
        for item_id in body.item_ids or []:
            listening_queue.remove(db, user, item_id)
    elif body.operation == "reorder":
        if body.item_ids is None or not listening_queue.reorder(db, user, body.item_ids):
            raise HTTPException(status.HTTP_409_CONFLICT, "queue_changed")


@router.post("/mutations")
def mutate_queue(
    auth: AuthCsrf,
    db: DbDep,
    body: MutationIn,
    idempotency_key: str = Header(alias="idempotency-key", pattern=idempotency.KEY_PATTERN),
) -> dict:
    payload = body.model_dump(mode="json")
    replay = idempotency.lookup(
        db, auth.user.id, "listening_queue.mutation", idempotency_key, payload
    )
    if replay is not None:
        return replay
    if not listening_queue.advance_version(db, auth.user, body.base_version):
        raise HTTPException(status.HTTP_409_CONFLICT, "queue_changed")
    _apply(db, auth.user, body)
    out = view(db, auth.user)
    idempotency.store(db, auth.user.id, "listening_queue.mutation", idempotency_key, payload, out)
    db.commit()
    return out
