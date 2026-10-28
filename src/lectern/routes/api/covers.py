"""The cover editor's grid of generated candidates. Editors only (owners and members).

Renders nothing: it lists designs and the ``generated.jpg`` URL of each, which the client
fetches (and the server caches) one at a time.
"""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, status

from lectern import covergen
from lectern.access import can_see_work
from lectern.models import Work
from lectern.routes.api.editor import EditorRead
from lectern.security.deps import DbDep

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
