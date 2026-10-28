"""Your private rating, notes, and read-throughs of a book (lectern.reviews, issue #84).
Session only, the caller's own, and only for works they can see; never anyone else's, owners
included."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from lectern import reviews
from lectern.access import can_see_work
from lectern.security.deps import Auth, AuthCsrf, DbDep

router = APIRouter()

PRIVATE = {"Cache-Control": "no-store"}
_DAY = r"^\d{4}-\d{2}-\d{2}$"
Day = Annotated[str | None, Field(default=None, pattern=_DAY)]


class ReadThroughIn(BaseModel):
    started_on: Day = None
    finished_on: Day = None


class ReviewIn(BaseModel):
    """Only the fields sent change. ``rating``: stars, ½ to 5 in halves, or ``null`` to clear.
    ``read_throughs`` replaces them all; ``[]`` goes back to the derived dates."""

    rating: float | None = None
    notes: Annotated[str, Field(max_length=reviews.MAX_NOTES)] = ""
    read_throughs: Annotated[list[ReadThroughIn], Field(max_length=reviews.MAX_READS)] = []


def _visible(db, user, work_id: int) -> None:
    if not can_see_work(db, user, work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


def _reply(db, user, work_id: int) -> JSONResponse:
    return JSONResponse(reviews.review_json(db, user, work_id), headers=PRIVATE)


@router.get("/works/{work_id}/review")
def get_review(auth: Auth, db: DbDep, work_id: int) -> JSONResponse:
    _visible(db, auth.user, work_id)
    return _reply(db, auth.user, work_id)


@router.put("/works/{work_id}/review")
def put_review(auth: AuthCsrf, db: DbDep, work_id: int, body: ReviewIn) -> JSONResponse:
    _visible(db, auth.user, work_id)
    sent = body.model_fields_set
    changes: dict = {}
    if "rating" in sent:
        changes["rating"] = body.rating
    if "notes" in sent:
        changes["notes"] = body.notes
    if "read_throughs" in sent:
        changes["read_throughs"] = [(r.started_on, r.finished_on) for r in body.read_throughs]
    try:
        reviews.save(db, auth.user.id, work_id, **changes)
    except reviews.ReviewError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.json()) from None
    db.commit()
    return _reply(db, auth.user, work_id)


@router.delete("/works/{work_id}/review")
def delete_review(auth: AuthCsrf, db: DbDep, work_id: int) -> JSONResponse:
    _visible(db, auth.user, work_id)
    reviews.clear(db, auth.user.id, work_id)
    db.commit()
    return _reply(db, auth.user, work_id)


@router.post("/works/{work_id}/review/dismiss")
def dismiss_prompt(auth: AuthCsrf, db: DbDep, work_id: int) -> JSONResponse:
    """Say "not now" to "Rate it?": it is not offered again for this book."""
    _visible(db, auth.user, work_id)
    reviews.dismiss_prompt(db, auth.user.id, work_id)
    db.commit()
    return _reply(db, auth.user, work_id)


@router.get("/reviews/finished")
def finished(auth: Auth, db: DbDep, year: Annotated[int, Query(ge=1900, le=2999)]) -> JSONResponse:
    """The books you finished in ``year`` (UTC), by date, with your ratings and dates as you
    corrected them (reading stats, #69). Re-reads count each time."""
    items = reviews.finished_in(db, auth.user, year)
    rated = [i for i in items if i["rating"] is not None]
    top = sorted(rated, key=lambda i: (-i["rating"], i["finished_on"]))[:10]
    return JSONResponse(
        {"year": year, "finished": items, "count": len(items), "top_rated": top},
        headers=PRIVATE,
    )
