"""Multi-select over the Library's current view, resolved on the server.

The web app selects "everything in this view" (Cmd/Ctrl+A) without enumerating ids: some of
those works may not be loaded or rendered. Bulk endpoints then take a :class:`Selection`
(the view's filter, minus the works the user deselected) instead of ``work_ids`` and resolve
it here, with exactly the filter the Library applies on the client (``frontend/src/pages/
Library.tsx``: ``fold`` on title, author, and ISBN; reading status from KOReader progress;
shelf membership).

- ``GET /api/library/count?q=&status=&shelf=``: how many works the view holds, for
  "All N selected".
"""

from __future__ import annotations

import unicodedata
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from lectern.access import visible_works
from lectern.models import ReadingProgress, ShelfItem, User, Work
from lectern.security.deps import Auth, DbDep

router = APIRouter()

Status = Literal["all", "reading", "unread", "finished"]
FINISHED = 0.985  # Library.tsx FINISHED
MAX_SELECTION = 5000


class ViewFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = Field(default="", max_length=200)
    status: Status = "all"
    shelf: int | None = None


class Selection(BaseModel):
    """Every work in ``filter``'s view except ``exclude_ids``. ``expected_count`` is the
    number the user saw ("All 212 selected"); a different count now is a 409."""

    model_config = ConfigDict(extra="forbid")

    filter: ViewFilter
    exclude_ids: list[int] = Field(default_factory=list, max_length=MAX_SELECTION)
    expected_count: int | None = Field(default=None, ge=0)


def fold(s: str) -> str:
    """``frontend/src/lib/fuzzy.ts`` ``fold``: compatibility-decompose, drop combining
    diacritical marks (U+0300–U+036F), lower-case."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not "\u0300" <= c <= "\u036f"
    ).lower()


def status_of(percentage: float | None) -> Status:
    p = percentage or 0.0
    if p <= 0:
        return "unread"
    return "finished" if p >= FINISHED else "reading"


def _latest_percentage(work: Work, progress: dict[str, ReadingProgress]) -> float | None:
    """The percentage of the work's most recently synced file, as ``_attach_progress``
    picks ``work.progress`` for the client."""
    best: ReadingProgress | None = None
    for f in work.files:
        p = progress.get(f.partial_md5 or "")
        if p is not None and (best is None or p.updated_at > best.updated_at):
            best = p
    return best.percentage if best is not None else None


def view_work_ids(db: Session, user: User, f: ViewFilter) -> list[int]:
    """Ids of the works ``user`` sees in the Library under ``f``, in title order."""
    stmt = select(Work).where(visible_works(user))
    if f.shelf is not None:
        stmt = stmt.where(
            Work.id.in_(select(ShelfItem.work_id).where(ShelfItem.shelf_id == f.shelf))
        )
    if f.status != "all":
        stmt = stmt.options(selectinload(Work.files))
    works = db.scalars(stmt.order_by(Work.sort_title, Work.id)).all()
    needle = fold(f.q.strip())
    if needle:
        works = [w for w in works if needle in fold(f"{w.title} {w.author} {w.isbn or ''}")]
    if f.status != "all":
        progress = {
            p.document: p
            for p in db.scalars(select(ReadingProgress).where(ReadingProgress.user_id == user.id))
        }
        works = [w for w in works if status_of(_latest_percentage(w, progress)) == f.status]
    return [w.id for w in works]


def resolve(
    db: Session, user: User, work_ids: list[int] | None, selection: Selection | None
) -> list[int]:
    """The works a bulk request targets: explicit ``work_ids`` (request order, deduplicated)
    or a server-side ``selection``. Exactly one must be given."""
    if (not work_ids) == (selection is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "work_ids_or_selection")
    if selection is None:
        return list(dict.fromkeys(work_ids or []))
    skip = set(selection.exclude_ids)
    ids = [i for i in view_work_ids(db, user, selection.filter) if i not in skip]
    if selection.expected_count is not None and len(ids) != selection.expected_count:
        raise HTTPException(status.HTTP_409_CONFLICT, "selection_changed")
    if not ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "selection_empty")
    if len(ids) > MAX_SELECTION:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "selection_too_large")
    return ids


@router.get("/library/count")
def library_count(
    auth: Auth,
    db: DbDep,
    q: str = Query(default="", max_length=200),
    status_: Status = Query(default="all", alias="status"),
    shelf: int | None = None,
) -> dict:
    return {
        "total": len(view_work_ids(db, auth.user, ViewFilter(q=q, status=status_, shelf=shelf)))
    }
