"""Next in series (automatic) and Want to read (manual): two separate per-user lists."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from lectern import series, want_to_read
from lectern.models import Work
from lectern.routes.api.core import _audio_summary, _positions
from lectern.routes.api.serializers import work_json
from lectern.security.deps import Auth, AuthCsrf, DbDep

router = APIRouter()


def _items(db, user, ids: list[int]) -> dict:
    works = {
        w.id: w
        for w in db.scalars(select(Work).options(selectinload(Work.files)).where(Work.id.in_(ids)))
    }
    audio = _audio_summary(db, ids)
    positions = _positions(db, user.id, audio, ids)
    return {
        "items": [
            work_json(works[i], audio=audio.get(i), position=positions.get(i))
            for i in ids
            if i in works
        ]
    }


@router.get("/next-in-series")
def get_next_in_series(auth: Auth, db: DbDep) -> dict:
    return _items(db, auth.user, series.next_in_series_ids(db, auth.user))


@router.get("/want-to-read")
def get_want_to_read(auth: Auth, db: DbDep) -> dict:
    return _items(db, auth.user, want_to_read.work_ids(db, auth.user))


class AddIn(BaseModel):
    work_id: int


class OrderIn(BaseModel):
    work_ids: list[int] = Field(max_length=10_000)


@router.post("/want-to-read")
def add_want_to_read(auth: AuthCsrf, db: DbDep, body: AddIn) -> dict:
    if not want_to_read.add(db, auth.user, body.work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    db.commit()
    return _items(db, auth.user, want_to_read.work_ids(db, auth.user))


@router.put("/want-to-read/order")
def reorder_want_to_read(auth: AuthCsrf, db: DbDep, body: OrderIn) -> dict:
    if not want_to_read.reorder(db, auth.user, body.work_ids):
        raise HTTPException(status.HTTP_409_CONFLICT, "queue_changed")
    db.commit()
    return _items(db, auth.user, want_to_read.work_ids(db, auth.user))


@router.delete("/want-to-read/{work_id}")
def remove_want_to_read(work_id: int, auth: AuthCsrf, db: DbDep) -> dict:
    want_to_read.remove(db, auth.user, work_id)
    db.commit()
    return _items(db, auth.user, want_to_read.work_ids(db, auth.user))
