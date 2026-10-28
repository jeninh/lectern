"""Your KOReader highlights and notes (lectern.annotations): per book, all books, searched and
filtered (issue #72), as Markdown, and as the quotes feed and Home's highlight of the day
(issue #83). Session only, and only the caller's own highlights on works they can see."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from lectern import annotations, search
from lectern.access import can_see_work, shelf_permission
from lectern.models import Shelf, Work
from lectern.routes.api.shelves import shelf_works
from lectern.security.deps import Auth, AuthCsrf, DbDep, SettingsDep

router = APIRouter()

PRIVATE = {"Cache-Control": "no-store"}
MAX_LIST = 200
_DAY = r"^\d{4}-\d{2}-\d{2}$"
# A little over MAX_QUERY_CHARS, so a long query gets the friendly 422 from lectern.search.
Q = Annotated[str, Query(max_length=search.MAX_QUERY_CHARS * 2)]


def _filters(
    work: int | None = None,
    author: Annotated[str | None, Query(max_length=512)] = None,
    tag: Annotated[str | None, Query(max_length=512)] = None,
    since: Annotated[str | None, Query(pattern=_DAY)] = None,
    until: Annotated[str | None, Query(pattern=_DAY)] = None,
) -> annotations.Filters:
    return annotations.Filters(work, author or None, tag or None, since, until)


Filters = Annotated[annotations.Filters, Depends(_filters)]


def _find(request: Request, auth, db, settings, q: str, filters, partial: bool = True):
    """:func:`lectern.annotations.find`, with the same per-person rate limit and refusals as
    the search inside books (its own budget, so the palette's two searches do not share one)."""
    if q.strip() and not request.app.state.limiters["highlights"].allow(f"user:{auth.user.id}"):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            {"detail": "too_many_searches", "message": "Too many searches; wait a moment."},
            headers={"Retry-After": "30"},
        )
    try:
        found = annotations.find(db, settings.library_dir, auth.user, q, filters, prefix=partial)
    except search.QueryError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, {"detail": exc.code, "message": str(exc)}
        ) from None
    db.commit()  # placements redone against a changed EPUB
    return found


def _visible(db, user, work_id: int) -> None:
    if not can_see_work(db, user, work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


def _download(text: str, name: str) -> Response:
    slug = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", slug).strip("-")[:80] or "highlights"
    return Response(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={**PRIVATE, "Content-Disposition": f'attachment; filename="{slug}.md"'},
    )


@router.get("/works/{work_id}/annotations")
def work_annotations(auth: Auth, db: DbDep, settings: SettingsDep, work_id: int) -> JSONResponse:
    _visible(db, auth.user, work_id)
    groups = annotations.visible(db, settings.library_dir, auth.user, work_id)
    db.commit()  # placements redone against a changed EPUB
    pairs = groups[0][1] if groups else []
    body = {
        "work_id": work_id,
        "annotations": [annotations.as_json(a, f) for a, f in pairs],
        "export_url": f"/api/works/{work_id}/annotations.md",
    }
    return JSONResponse(body, headers=PRIVATE)


@router.get("/works/{work_id}/annotations.md")
def work_annotations_md(auth: Auth, db: DbDep, settings: SettingsDep, work_id: int) -> Response:
    _visible(db, auth.user, work_id)
    groups = annotations.visible(db, settings.library_dir, auth.user, work_id)
    db.commit()
    title = db.get(Work, work_id).title
    if not groups:
        return _download(f"# {title}\n\nNo highlights yet.\n", f"{title} highlights")
    return _download(annotations.markdown(groups), f"{title} highlights")


@router.get("/annotations")
def all_annotations(auth: Auth, db: DbDep, settings: SettingsDep) -> JSONResponse:
    """Which of your books have highlights, and how many."""
    groups = annotations.visible(db, settings.library_dir, auth.user)
    db.commit()
    works = [
        {
            "work_id": w.id,
            "title": w.title,
            "author": w.author,
            "tags": w.tags or [],
            "count": len(pairs),
        }
        for w, pairs in groups
    ]
    body = {
        "works": works,
        "total": sum(w["count"] for w in works),
        "export_url": "/api/annotations.md",
    }
    return JSONResponse(body, headers=PRIVATE)


@router.get("/annotations/search")
def search_annotations(
    request: Request,
    auth: Auth,
    db: DbDep,
    settings: SettingsDep,
    filters: Filters,
    q: Q = "",
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
    # "true" while the last word is still being typed: it matches as a prefix.
    partial: bool = True,
) -> JSONResponse:
    """Your highlights matching ``q`` (all of them when blank) and the filters, best first:
    each with its book, the text and note with the matched words marked, and a short
    ``snippet`` around the first match (``field``: ``text`` or ``note``)."""
    found = _find(request, auth, db, settings, q, filters, partial)
    hits = found.hits()
    shown = hits[:limit]
    marked = annotations.marks(db, found.mark, [a.id for _, a, _ in shown])
    results = []
    for w, a, f in shown:
        text, note = marked.get(a.id, (None, None))
        field, snip = next(
            (
                (k, sn)
                for k, m in (("text", text), ("note", note))
                if m and (sn := search.snippet(m))
            ),
            (None, None),
        )
        results.append(
            {
                "work_id": w.id,
                "title": w.title,
                "author": w.author,
                "annotation": annotations.as_json(a, f),
                "text_parts": annotations.parts(text),
                "note_parts": annotations.parts(note),
                "field": field,
                "snippet": snip and {k: snip[k] for k in ("parts", "before", "after")},
            }
        )
    body = {"results": results, "total": len(hits), "truncated": len(hits) > limit}
    return JSONResponse(body, headers=PRIVATE)


@router.get("/annotations.md")
def all_annotations_md(
    request: Request,
    auth: Auth,
    db: DbDep,
    settings: SettingsDep,
    filters: Filters,
    q: Q = "",
) -> Response:
    """All your highlights as Markdown, or those the highlights page shows (``q`` and the
    same filters as ``/api/annotations/search``)."""
    groups = _find(request, auth, db, settings, q, filters, partial=False).groups
    text = annotations.markdown(groups, title="Highlights")
    if not groups:
        text = "# Highlights\n\nNo highlights yet.\n"
    return _download(text, "Lectern highlights")


# --- quotes and the highlight of the day (issue #83) --------------------------------------------


def _quote_json(w: Work, a, f) -> dict:
    return {
        "work_id": w.id,
        "title": w.title,
        "author": w.author,
        "annotation": annotations.as_json(a, f),
    }


@router.get("/quotes")
def quotes(
    auth: Auth,
    db: DbDep,
    settings: SettingsDep,
    filters: Filters,
    shelf: int | None = None,
    order: Literal["newest", "shuffle"] = "newest",
    seed: Annotated[str, Query(max_length=64)] = "",
    show: Literal["all", "favourites", "hidden"] = "all",
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JSONResponse:
    """The quotes feed: your highlights under the highlights page's filters and on ``shelf``
    (one you can see), newest first or shuffled by ``seed``. Hidden ones only with
    ``show=hidden``; ``show=favourites`` only favourites."""
    on_shelf = None
    if shelf is not None:
        s = db.get(Shelf, shelf)
        if s is None or shelf_permission(db, auth.user, s) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        on_shelf = {w.id for w in shelf_works(db, auth.user, s)}
    found = annotations.find(db, settings.library_dir, auth.user, "", filters)
    hits = [
        (w, a, f)
        for w, a, f in found.hits()
        if (on_shelf is None or w.id in on_shelf)
        and (a.hidden if show == "hidden" else not a.hidden)
        and (show != "favourites" or a.favourite)
    ]
    db.commit()  # placements redone against a changed EPUB
    hits = annotations.in_order(hits, order, seed)
    body = {
        "results": [_quote_json(*h) for h in hits[offset : offset + limit]],
        "total": len(hits),
        "daily": auth.user.quote_of_the_day,
    }
    return JSONResponse(body, headers=PRIVATE)


@router.get("/quotes/today")
def quote_of_the_day(
    auth: Auth,
    db: DbDep,
    settings: SettingsDep,
    # The person's local date (the app sends its own); the server's date when left out.
    day: Annotated[str | None, Query(pattern=_DAY)] = None,
) -> JSONResponse:
    """Home's highlight of the day: one of your own, the same all day, another tomorrow;
    ``quote`` is null when it is turned off or there is none."""
    try:
        today = date.fromisoformat(day) if day else date.today()
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Not a date") from None
    hit = None
    if auth.user.quote_of_the_day:
        hit = annotations.of_the_day(db, settings.library_dir, auth.user, today)
        db.commit()
    body = {
        "enabled": auth.user.quote_of_the_day,
        "day": today.isoformat(),
        "quote": _quote_json(*hit) if hit else None,
    }
    return JSONResponse(body, headers=PRIVATE)


class QuoteSettingsIn(BaseModel):
    daily: bool


@router.post("/quotes/settings")
def quote_settings(auth: AuthCsrf, db: DbDep, body: QuoteSettingsIn) -> dict:
    """Turn Home's highlight of the day on or off."""
    auth.user.quote_of_the_day = body.daily
    db.commit()
    return {"daily": auth.user.quote_of_the_day}


class QuoteMarkIn(BaseModel):
    favourite: bool | None = None
    hidden: bool | None = None


@router.post("/annotations/{annotation_id}/mark")
def mark_quote(
    auth: AuthCsrf, db: DbDep, settings: SettingsDep, annotation_id: int, body: QuoteMarkIn
) -> JSONResponse:
    """Favourite or hide one of your highlights (only fields present change). 404 for anyone
    else's, and for yours on a work you can no longer see."""
    groups = annotations.visible(db, settings.library_dir, auth.user, ids=[annotation_id])
    if not groups:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    w, [(a, f)] = groups[0]
    if body.favourite is not None:
        a.favourite = body.favourite
    if body.hidden is not None:
        a.hidden = body.hidden
    db.commit()
    return JSONResponse(_quote_json(w, a, f), headers=PRIVATE)
