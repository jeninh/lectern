"""Multi-select over the Library's current view, resolved on the server.

The web app selects "everything in this view" (Cmd/Ctrl+A) without enumerating ids: some of
those works may not be loaded or rendered. Bulk endpoints then take a :class:`Selection`
(the view's filter, minus the works the user deselected) instead of ``work_ids`` and resolve
it here, with exactly the filter the Library applies (``frontend/src/pages/Library.tsx``:
``fold`` on title, author, and ISBN; the reading status from ``lectern.reading_state``;
direct membership of one shelf, or ``unshelved``: on none of the shelves the person sees,
the Library's root folder; and a filter, ``f``, which the server always evaluates:
``GET /api/library?f=``, lectern.filters).

- ``GET /api/library/count?q=&status=&shelf=&unshelved=&f=&subtree=``: how many works the
  view holds, for "All N selected", and ``view_token``, a fingerprint of exactly which works
  they are.

A selection names the view by ``view_token``; if the view resolves to a different set of
works by the time the action runs (one left and another arrived, say), it is ``409
selection_changed`` rather than an action on works the user never saw. A ``shelf`` the user
may not see is ``404``, as ``GET /api/shelves/{id}`` is. Each endpoint caps how many works one
selection may resolve to, as it caps ``work_ids``.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from lectern import filters
from lectern.access import visible_shelf_ids
from lectern.filters import fold
from lectern.models import ShelfItem, User, Work
from lectern.reading_state import FINISHED
from lectern.security.deps import Auth, DbDep

router = APIRouter()

Status = Literal["all", "reading", "unread", "finished"]
MAX_EXCLUDE = 5000


class ViewFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = Field(default="", max_length=200)
    status: Status = "all"
    shelf: int | None = None
    # The Library's root folder: works on none of the shelves this person sees.
    unshelved: bool = False
    # A filter (docs/filters.md), and with ``shelf``, the shelf's sub-shelves too.
    f: str = Field(default="", max_length=filters.MAX_CHARS)
    subtree: bool = False
    # ``is:offline``: the works this device holds offline.
    offline_ids: list[int] = Field(default_factory=list, max_length=MAX_EXCLUDE)


class Selection(BaseModel):
    """Every work in ``filter``'s view except ``exclude_ids``. ``view_token`` is the view the
    user saw (``GET /api/library/count``); ``expected_count``, if given, the number selected
    ("All 212 selected"). Either differing now is a 409."""

    model_config = ConfigDict(extra="forbid")

    filter: ViewFilter
    exclude_ids: list[int] = Field(default_factory=list, max_length=MAX_EXCLUDE)
    view_token: str = Field(min_length=1, max_length=64)
    expected_count: int | None = Field(default=None, ge=0)


def shelved_work_ids(user: User):
    """Works on at least one shelf ``user`` may see (the rest are at the Library's root)."""
    return select(ShelfItem.work_id).where(ShelfItem.shelf_id.in_(visible_shelf_ids(user)))


def view_token(ids: list[int]) -> str:
    """Fingerprint of which works a view holds (order does not matter: sorting a view does
    not change what \"all\" means)."""
    return hashlib.sha256(",".join(map(str, sorted(ids))).encode()).hexdigest()[:32]


def view_work_ids(db: Session, user: User, f: ViewFilter) -> list[int]:
    """Ids of the works ``user`` sees in the Library under ``f``, in title order. A shelf the
    user may not see is 404, exactly as ``GET /api/shelves/{id}``: its membership is not
    theirs to learn or to act on."""
    from lectern.routes.api.core import library_query

    stmt = library_query(
        db, user, f=f.f, shelf=f.shelf, subtree=f.subtree, unshelved=f.unshelved,
        offline=frozenset(f.offline_ids),
    )  # fmt: skip
    if f.status != "all":
        x = filters.fraction_of(user)
        stmt = stmt.where(
            {"unread": x <= 0, "reading": and_(x > 0, x < FINISHED), "finished": x >= FINISHED}[
                f.status
            ]
        )
    works = db.execute(
        stmt.with_only_columns(Work.id, Work.title, Work.author, Work.isbn).order_by(
            Work.sort_title, Work.id
        )
    ).all()
    needle = fold(f.q.strip())
    if needle:
        works = [w for w in works if needle in fold(f"{w.title} {w.author} {w.isbn or ''}")]
    return [w.id for w in works]


def resolve(
    db: Session,
    user: User,
    work_ids: list[int] | None,
    selection: Selection | None,
    *,
    limit: int,
) -> list[int]:
    """The works a bulk request targets: explicit ``work_ids`` (request order, deduplicated)
    or a server-side ``selection``. Exactly one must be given. A selection resolving to more
    than ``limit`` works (the endpoint's own cap on ``work_ids``) is refused."""
    if (not work_ids) == (selection is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "work_ids_or_selection")
    if selection is None:
        return list(dict.fromkeys(work_ids or []))
    view = view_work_ids(db, user, selection.filter)
    if view_token(view) != selection.view_token:
        raise HTTPException(status.HTTP_409_CONFLICT, "selection_changed")
    skip = set(selection.exclude_ids)
    ids = [i for i in view if i not in skip]
    if selection.expected_count is not None and len(ids) != selection.expected_count:
        raise HTTPException(status.HTTP_409_CONFLICT, "selection_changed")
    if not ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "selection_empty")
    if len(ids) > limit:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "detail": "selection_too_large",
                "limit": limit,
                "message": f"Select at most {limit} books for this action.",
            },
        )
    return ids


@router.get("/library/count")
def library_count(
    auth: Auth,
    db: DbDep,
    q: str = Query(default="", max_length=200),
    status_: Status = Query(default="all", alias="status"),
    shelf: int | None = None,
    unshelved: bool = False,
    f: str = Query(default="", max_length=filters.MAX_CHARS),
    subtree: bool = False,
    offline: str = Query(default="", max_length=60000),
) -> dict:
    from lectern.routes.api.core import offline_ids_param

    f = ViewFilter(
        q=q, status=status_, shelf=shelf, unshelved=unshelved, f=f, subtree=subtree,
        offline_ids=sorted(offline_ids_param(offline)),
    )  # fmt: skip
    ids = view_work_ids(db, auth.user, f)
    return {"total": len(ids), "view_token": view_token(ids)}
