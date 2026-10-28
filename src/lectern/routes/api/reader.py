"""The web reader's typography and theme, per person (issue #79): a default, and settings for
one work over it ("Just this book"). Stored on the server so they follow the person to every
browser; the app keeps a copy in localStorage for offline use (frontend/src/lib/reader.ts).

Only the fields a person changed are stored, so a later change to the app's own defaults, or
to their default, still reaches every field they never touched.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete

from lectern.models import ReaderOverride
from lectern.routes.api.works import _visible_work
from lectern.security.deps import Auth, AuthCsrf, DbDep

router = APIRouter()


class ReaderSettings(BaseModel):
    """Every field optional: what is absent comes from the default (or the app's defaults).
    Names and ranges as in ``ReaderSettings`` in frontend/src/lib/reader.ts."""

    model_config = ConfigDict(extra="forbid")

    flow: Literal["paginated", "scrolled"] | None = None
    size: int | None = Field(None, ge=50, le=300)  # percent
    lineHeight: float | None = Field(None, ge=1.0, le=3.0)
    font: Literal["publisher", "serif", "sans", "dyslexic", "hyperlegible", "lexend"] | None = None
    width: Literal["narrow", "normal", "wide"] | None = None
    margin: Literal["small", "normal", "large"] | None = None
    theme: Literal["auto", "light", "sepia", "dark", "contrast"] | None = None
    justify: bool | None = None
    letterSpacing: float | None = Field(None, ge=0, le=0.3)  # em; 0 = the book's
    wordSpacing: float | None = Field(None, ge=0, le=1)  # em; 0 = the book's
    paragraphSpacing: float | None = Field(None, ge=0, le=3)  # em; 0 = the book's
    heavier: bool | None = None


class SettingsIn(BaseModel):
    settings: ReaderSettings


def _stored(s: ReaderSettings) -> dict:
    return s.model_dump(exclude_none=True)


def _for_work(db, user, work_id: int) -> dict:
    row = db.get(ReaderOverride, (user.id, work_id))
    return {"default": user.reader_settings, "book": row.settings if row else None}


@router.put("/me/reader-settings")
def put_default(auth: AuthCsrf, db: DbDep, body: SettingsIn) -> dict:
    """Replace the caller's default; ``{}`` resets it to the app's defaults."""
    auth.user.reader_settings = _stored(body.settings)
    db.commit()
    return {"default": auth.user.reader_settings}


@router.get("/works/{work_id}/reader-settings")
def get_for_work(work_id: int, auth: Auth, db: DbDep) -> dict:
    """The caller's default and their settings for this work (``null``: it follows the
    default)."""
    _visible_work(db, auth.user, work_id)
    return _for_work(db, auth.user, work_id)


@router.put("/works/{work_id}/reader-settings")
def put_for_work(work_id: int, auth: AuthCsrf, db: DbDep, body: SettingsIn) -> dict:
    """Replace the caller's settings for this work; ``{}`` keeps the work on its own settings,
    all equal to the default."""
    _visible_work(db, auth.user, work_id)
    row = db.get(ReaderOverride, (auth.user.id, work_id))
    if row is None:
        row = ReaderOverride(user_id=auth.user.id, work_id=work_id, settings={})
        db.add(row)
    row.settings = _stored(body.settings)
    db.commit()
    return _for_work(db, auth.user, work_id)


@router.delete("/works/{work_id}/reader-settings")
def delete_for_work(work_id: int, auth: AuthCsrf, db: DbDep) -> dict:
    """The work follows the caller's default again."""
    _visible_work(db, auth.user, work_id)
    db.execute(
        delete(ReaderOverride).where(
            ReaderOverride.user_id == auth.user.id, ReaderOverride.work_id == work_id
        )
    )
    db.commit()
    return _for_work(db, auth.user, work_id)
