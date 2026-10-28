"""Your private vocabulary list (issue #80, lectern.vocabulary). Session only; everything is
``no-store`` and only the caller's own words are ever visible.

- ``POST /api/vocabulary``: save a word (sent only by the Save button; lookups store nothing).
- ``GET /api/vocabulary``: the list with ``facets``; ``PATCH``/``DELETE /api/vocabulary/{id}``.
- ``GET /api/vocabulary/export?format=anki|csv``: a download, built here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from lectern import dictionary, vocabulary
from lectern.models import VocabularyWord
from lectern.security.deps import Auth, AuthCsrf, DbDep, SettingsDep

router = APIRouter()

PRIVATE = {"Cache-Control": "no-store"}
MAX_LIST = 200
_DAY = r"^\d{4}-\d{2}-\d{2}$"
Lang = Annotated[str | None, Query(max_length=16, pattern=dictionary.LANG_RE.pattern)]


def _filters(
    work: int | None = None,
    lang: Lang = None,
    since: Annotated[str | None, Query(pattern=_DAY)] = None,
    until: Annotated[str | None, Query(pattern=_DAY)] = None,
) -> vocabulary.Filters:
    return vocabulary.Filters(work=work, lang=lang, since=since, until=until)


Filters = Annotated[vocabulary.Filters, Depends(_filters)]


def _json(body: dict, code: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=code, headers=PRIVATE)


def _bad_date(f: vocabulary.Filters) -> None:
    from datetime import date

    try:
        for s in (f.since, f.until):
            if s:
                date.fromisoformat(s)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "bad_date") from None


@router.post("/vocabulary")
def save(auth: AuthCsrf, db: DbDep, body: vocabulary.SavePayload) -> JSONResponse:
    try:
        row, created = vocabulary.save(db, auth.user, body)
    except vocabulary.NotVisible:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None
    except vocabulary.BadWord:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "empty_word") from None
    db.commit()
    visible = vocabulary.shown_work(db, auth.user, row)
    return _json(
        {"created": created, "word": vocabulary.as_json(row, visible)},
        status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@router.get("/vocabulary")
def listing(
    auth: Auth,
    db: DbDep,
    f: Filters,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JSONResponse:
    _bad_date(f)
    f.limit, f.offset = limit, offset
    rows, total = vocabulary.visible_list(db, auth.user, f)
    return _json(
        {
            "words": [vocabulary.as_json(r, w) for r, w in rows],
            "total": total,
            "facets": vocabulary.facets(db, auth.user),
        }
    )


@router.get("/vocabulary/export")
def export(
    auth: Auth,
    db: DbDep,
    settings: SettingsDep,
    f: Filters,
    format: Annotated[str, Query(pattern="^(anki|csv)$")],
) -> Response:
    _bad_date(f)
    f.limit = vocabulary.MAX_EXPORT
    rows, _ = vocabulary.visible_list(db, auth.user, f)
    if format == "anki":
        text, media, name = vocabulary.export_anki(rows), "text/tab-separated-values", "anki.txt"
    else:
        text, media, name = (
            vocabulary.export_csv(rows, settings.base_url.rstrip("/")),
            "text/csv",
            "vocabulary.csv",
        )
    return Response(
        text,
        media_type=f"{media}; charset=utf-8",
        headers={
            **PRIVATE,
            "Content-Disposition": f'attachment; filename="lectern-vocabulary-{name}"',
        },
    )


class VocabularyPatch(BaseModel):
    definition: str | None = Field(default=None, min_length=1, max_length=vocabulary.MAX_DEFINITION)
    note: str | None = Field(default=None, max_length=vocabulary.MAX_DEFINITION)


@router.patch("/vocabulary/{word_id}")
def change(auth: AuthCsrf, db: DbDep, word_id: int, body: VocabularyPatch) -> JSONResponse:
    row = _own(db, auth, word_id)
    if body.definition is not None:
        row.definition = body.definition
    if "note" in body.model_fields_set:
        row.note = (body.note or "").strip() or None
    db.commit()
    visible = vocabulary.shown_work(db, auth.user, row)
    return _json({"word": vocabulary.as_json(row, visible)})


@router.delete("/vocabulary/{word_id}")
def remove(auth: AuthCsrf, db: DbDep, word_id: int) -> JSONResponse:
    db.delete(_own(db, auth, word_id))
    db.commit()
    return _json({"removed": word_id})


def _own(db, auth, word_id: int) -> VocabularyWord:
    row = db.scalar(
        select(VocabularyWord).where(
            VocabularyWord.id == word_id, VocabularyWord.user_id == auth.user.id
        )
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return row
