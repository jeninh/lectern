"""The cover editor's grid of generated candidates. Editors only (owners and members).

Renders nothing: it lists designs and the ``generated.jpg`` URL of each, which the client
fetches (and the server caches) one at a time.
"""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Response, status

from lectern import covergen
from lectern import covers as sources
from lectern.access import can_see_work
from lectern.editing import EditError
from lectern.models import Work
from lectern.routes.api.editor import Editor, EditorRead, _edit_error
from lectern.security.deps import DbDep, SettingsDep

router = APIRouter()

Font = Literal["display", "text", "noto"]


@router.get("/works/{work_id}/cover/candidates")
def cover_candidates(
    auth: EditorRead,
    db: DbDep,
    work_id: int,
    title: Annotated[str | None, Query(max_length=covergen.MAX_TEXT)] = None,
    author: Annotated[str | None, Query(max_length=covergen.MAX_TEXT)] = None,
    font: Font | None = None,
    offset: Annotated[int, Query(ge=0, le=1_000_000 - 6)] = 0,
    count: Annotated[int, Query(ge=1, le=6)] = 6,
):
    """Candidate ``count`` designs from ``offset``: candidate ``i`` has seed ``i`` (stepping
    through the palettes) and the ``i``-th style in rotation, so 11 consecutive ones show
    every palette."""
    work = db.get(Work, work_id)
    if work is None or work.trashed_at is not None or not can_see_work(db, auth.user, work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    t = work.title if title is None else title
    a = covergen.design_of(work).author if author is None else author
    f = font or work.cover_font or covergen.DEFAULT_FONT
    out = []
    for i in range(offset, offset + count):
        style = covergen.STYLES[i % len(covergen.STYLES)]
        design = covergen.design_for(t, a, style=style, seed=i, lang=work.language, font=f)
        query = urlencode({"title": t, "author": a, "style": style, "seed": i, "font": f, "w": 240})
        out.append(
            {
                "style": style,
                "seed": i,
                "font": f,
                "palette": design.palette.name,
                "url": f"/api/works/{work_id}/cover/generated.jpg?{query}",
            }
        )
    return {
        "fonts": list(covergen.FONTS),
        "styles": list(covergen.STYLES),
        "candidates": out,
    }


def _visible_work(db, auth, work_id: int) -> Work:
    work = db.get(Work, work_id)
    if work is None or work.trashed_at is not None or not can_see_work(db, auth.user, work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return work


@router.post("/works/{work_id}/cover/stage")
def stage_cover_source(
    auth: Editor, db: DbDep, settings: SettingsDep, work_id: int, body: sources.StageRequest
) -> dict:
    """Stage a cover from an earlier staged image, an EPUB image, or a PDF/comic page, with an
    optional normalised crop. Returns a token to save through the ``upload`` cover action."""
    work = _visible_work(db, auth, work_id)
    try:
        return sources.stage_source(db, settings, auth.user.id, work, body)
    except EditError as exc:
        raise _edit_error(exc) from None


@router.get("/works/{work_id}/cover/sources/epub/{file_id}/{index}.jpg", include_in_schema=False)
def epub_image_thumbnail(
    auth: EditorRead, db: DbDep, settings: SettingsDep, work_id: int, file_id: int, index: int
) -> Response:
    work = _visible_work(db, auth, work_id)
    try:
        data = sources.thumbnail(db, settings, work, file_id, index)
    except EditError as exc:
        raise _edit_error(exc) from None
    return Response(
        data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"}
    )
