"""Full-text search inside books (the Cmd/Ctrl+K palette's "In books"): ``lectern.search``."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status

from lectern import search
from lectern.security.deps import Auth, DbDep, SettingsDep

router = APIRouter()


@router.get("/search/text")
def search_text(
    request: Request,
    auth: Auth,
    db: DbDep,
    settings: SettingsDep,
    # A little over MAX_QUERY_CHARS, so a long query gets the friendly 422 from lectern.search.
    q: Annotated[str, Query(max_length=search.MAX_QUERY_CHARS * 2)],
    limit: Annotated[int, Query(ge=1, le=search.MAX_RESULTS)] = 8,
    # "1" while the last word is still being typed: it matches as a prefix.
    partial: bool = True,
) -> dict:
    if not request.app.state.limiters["search"].allow(f"user:{auth.user.id}"):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            {"detail": "too_many_searches", "message": "Too many searches; wait a moment."},
            headers={"Retry-After": "30"},
        )
    try:
        return search.search(db, auth.user, settings, q, limit=limit, prefix=partial)
    except search.QueryError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, {"detail": exc.code, "message": str(exc)}
        ) from None
