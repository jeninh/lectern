"""Your audiobook bookmarks and clips (issue #81, lectern.audio_bookmarks): per book, made from
the player, with notes. Session only; only the caller's own marks, on works they can see. The
highlights routes (``highlights.py``) list, search, and export them with the highlights."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from lectern import audio_bookmarks
from lectern.access import can_see_work
from lectern.models import AudioBookmark, Work
from lectern.progress import audio_timeline
from lectern.security.deps import Auth, AuthCsrf, DbDep, SettingsDep

router = APIRouter()

PRIVATE = {"Cache-Control": "no-store"}
MAX_PER_WORK = 2000


class MarkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_s: float = Field(ge=0)
    end_s: float | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=audio_bookmarks.MAX_NOTE)
    chapter: str | None = Field(default=None, max_length=512)
    device: str = Field(default="", max_length=128)


class MarkPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=audio_bookmarks.MAX_NOTE)
    start_s: float | None = Field(default=None, ge=0)
    end_s: float | None = Field(default=None, ge=0)
    # A clip turned back into a moment: ``end_s`` cleared.
    bookmark: bool = False


def _work(db, user, work_id: int) -> Work:
    if not can_see_work(db, user, work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return db.get(Work, work_id)


def _own(db, user, mark_id: int) -> AudioBookmark:
    """The caller's mark on a work they can still see; 404 for everything else."""
    m = db.get(AudioBookmark, mark_id)
    if m is None or m.user_id != user.id or not can_see_work(db, user, m.work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return m


def _note(text: str | None) -> str | None:
    return (text or "").strip() or None


def _check_span(db, work_id: int, start: float, end: float | None) -> tuple[float, float | None]:
    """Clamp to the audiobook and refuse an empty, reversed, or overlong clip."""
    tl = audio_timeline(db, work_id)
    if tl.tracks == 0:
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": "no_audio",
                            "message": "This book has no audiobook."})  # fmt: skip
    start = min(start, tl.total_s)
    if end is not None:
        end = min(end, tl.total_s)
        if end - start < 0.5:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"detail": "clip_too_short", "message": "A clip must end after it starts."},
            )
        if end - start > audio_bookmarks.MAX_CLIP_S:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"detail": "clip_too_long", "message": "A clip is at most 30 minutes."},
            )
    return round(start, 2), (round(end, 2) if end is not None else None)


def _json(db, settings, work: Work, m: AudioBookmark) -> dict:
    placed = audio_bookmarks.place(db, settings.library_dir, work, [m])
    return audio_bookmarks.as_json(m, placed.get(m.id))


@router.get("/works/{work_id}/audio-bookmarks")
def list_marks(auth: Auth, db: DbDep, settings: SettingsDep, work_id: int) -> JSONResponse:
    work = _work(db, auth.user, work_id)
    groups = audio_bookmarks.visible(db, auth.user, work_id)
    marks = groups[0][1] if groups else []
    placed = audio_bookmarks.place(db, settings.library_dir, work, marks)
    body = {
        "work_id": work_id,
        "bookmarks": [audio_bookmarks.as_json(m, placed.get(m.id)) for m in marks],
        "export_url": f"/api/works/{work_id}/annotations.md",
    }
    return JSONResponse(body, headers=PRIVATE)


@router.post("/works/{work_id}/audio-bookmarks", status_code=status.HTTP_201_CREATED)
def create_mark(
    auth: AuthCsrf, db: DbDep, settings: SettingsDep, work_id: int, body: MarkIn
) -> JSONResponse:
    work = _work(db, auth.user, work_id)
    count = db.scalar(
        select(func.count(AudioBookmark.id)).where(
            AudioBookmark.user_id == auth.user.id, AudioBookmark.work_id == work_id
        )
    )
    if count >= MAX_PER_WORK:
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": "too_many_bookmarks",
                            "message": "This book has as many bookmarks as it can hold."})  # fmt: skip
    start, end = _check_span(db, work_id, body.start_s, body.end_s)
    m = AudioBookmark(user_id=auth.user.id, work_id=work_id, start_s=start, end_s=end,
                      note=_note(body.note), chapter=(body.chapter or "").strip() or None,
                      device=body.device.strip())  # fmt: skip
    db.add(m)
    db.commit()
    return JSONResponse(_json(db, settings, work, m), status_code=201, headers=PRIVATE)


@router.patch("/audio-bookmarks/{mark_id}")
def update_mark(
    auth: AuthCsrf, db: DbDep, settings: SettingsDep, mark_id: int, body: MarkPatch
) -> JSONResponse:
    m = _own(db, auth.user, mark_id)
    sent = body.model_fields_set
    if "note" in sent:
        m.note = _note(body.note)
    if {"start_s", "end_s", "bookmark"} & sent:
        start = body.start_s if body.start_s is not None else m.start_s
        end = None if body.bookmark else (body.end_s if body.end_s is not None else m.end_s)
        m.start_s, m.end_s = _check_span(db, m.work_id, start, end)
    db.commit()
    return JSONResponse(_json(db, settings, db.get(Work, m.work_id), m), headers=PRIVATE)


@router.delete("/audio-bookmarks/{mark_id}")
def delete_mark(auth: AuthCsrf, db: DbDep, mark_id: int) -> dict:
    db.delete(_own(db, auth.user, mark_id))
    db.commit()
    return {"deleted": True}
