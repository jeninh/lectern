"""Word lookup in the reader from offline dictionaries on this server (``lectern.dictionary``).

- ``GET /api/dictionary/lookup?q=&lang=&to=`` (any signed-in reader): definitions and
  translations. Reads only local files; never logged.
- ``GET /api/admin/dictionaries`` (owner): what is installed.
- ``POST /api/admin/dictionaries?lang=&to=&name=`` (owner): install a StarDict archive sent
  as the request body (``.zip`` or ``.tar.gz``); converted in the request, once.
- ``PATCH``/``DELETE /api/admin/dictionaries/{id}`` (owner): turn on or off, correct the
  languages, or remove.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from lectern import dictionary
from lectern.models import Role
from lectern.security import audit
from lectern.security.deps import (
    Auth,
    AuthContext,
    DbDep,
    SettingsDep,
    require_role,
    require_role_read,
)
from lectern.security.sessions import client_ip

router = APIRouter()
OwnerRead = Annotated[AuthContext, Depends(require_role_read(Role.OWNER))]
OwnerWrite = Annotated[AuthContext, Depends(require_role(Role.OWNER))]
Lang = Annotated[str | None, Query(max_length=16, pattern=dictionary.LANG_RE.pattern)]

STATUS = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "import_running": status.HTTP_409_CONFLICT,
    "archive_too_large": status.HTTP_413_CONTENT_TOO_LARGE,
}


def _refused(exc: dictionary.DictionaryError) -> HTTPException:
    return HTTPException(
        STATUS.get(exc.code, status.HTTP_422_UNPROCESSABLE_CONTENT),
        {"detail": exc.code, "message": str(exc)},
    )


@router.get("/dictionary/lookup")
def lookup(
    request: Request,
    auth: Auth,
    settings: SettingsDep,
    q: Annotated[str, Query(min_length=1, max_length=dictionary.MAX_QUERY_CHARS * 2)],
    lang: Lang = None,
    to: Lang = None,
) -> dict:
    if not request.app.state.limiters["dictionary"].allow(f"user:{auth.user.id}"):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            {"detail": "too_many_lookups", "message": "Too many lookups; wait a moment."},
            headers={"Retry-After": "30"},
        )
    return dictionary.lookup(settings, q, lang, to)


@router.get("/admin/dictionaries")
def list_dictionaries(auth: OwnerRead, settings: SettingsDep) -> dict:
    return {"dictionaries": [d.json() for d in dictionary.installed(settings)]}


@router.post("/admin/dictionaries", status_code=status.HTTP_201_CREATED)
async def install(
    request: Request,
    auth: OwnerWrite,
    db: DbDep,
    settings: SettingsDep,
    lang: Annotated[str, Query(max_length=16, pattern=dictionary.LANG_RE.pattern)],
    to: Lang = None,
    name: Annotated[str | None, Query(max_length=120)] = None,
) -> dict:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > dictionary.MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "too_large")
    with dictionary.staging_file(settings) as tmp:
        size = 0
        with open(tmp, "wb") as f:
            async for block in request.stream():
                size += len(block)
                if size > dictionary.MAX_UPLOAD_BYTES:
                    raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "too_large")
                f.write(block)
        if not size:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "empty")
        try:
            meta = await run_in_threadpool(
                dictionary.import_path, settings, tmp, lang=lang, to=to, name=name
            )
        except dictionary.DictionaryError as exc:
            raise _refused(exc) from None
    audit.record(
        db, "dictionary.install", actor_user_id=auth.user.id, target=meta.id, ip=client_ip(request)
    )
    db.commit()
    return {"dictionary": meta.json()}


class DictionaryPatch(BaseModel):
    enabled: bool | None = None
    lang: str | None = Field(default=None, max_length=16)
    to: str | None = Field(default=None, max_length=16)


@router.patch("/admin/dictionaries/{dict_id}")
def change(auth: OwnerWrite, settings: SettingsDep, dict_id: str, body: DictionaryPatch) -> dict:
    try:
        meta = dictionary.update(
            settings, dict_id, enabled=body.enabled, lang=body.lang, to=body.to
        )
    except dictionary.DictionaryError as exc:
        raise _refused(exc) from None
    return {"dictionary": meta.json()}


@router.delete("/admin/dictionaries/{dict_id}")
def remove(
    request: Request, auth: OwnerWrite, db: DbDep, settings: SettingsDep, dict_id: str
) -> dict:
    try:
        meta = dictionary.remove(settings, dict_id)
    except dictionary.DictionaryError as exc:
        raise _refused(exc) from None
    audit.record(
        db, "dictionary.remove", actor_user_id=auth.user.id, target=meta.id, ip=client_ip(request)
    )
    db.commit()
    return {"removed": meta.id}
