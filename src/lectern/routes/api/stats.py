"""Private reading statistics and year review; never a household/admin report."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query

from lectern import reading_stats
from lectern.security.deps import Auth, DbDep

router = APIRouter(prefix="/stats")


@router.get("")
def get_stats(
    auth: Auth, db: DbDep, year: int | None = Query(default=None, ge=1900, le=2999)
) -> dict:
    """The signed-in person's visible activity only, including their selected calendar zone."""
    return reading_stats.summary(db, auth.user, year or datetime.now().year)
