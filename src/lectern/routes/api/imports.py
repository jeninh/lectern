"""Review before filing (``lectern.imports``): ``/api/imports``.

Every route is owner/member only and session + CSRF like the rest of ``/api``; an import is
visible only to its uploader (``404`` otherwise). Writes that create or file accept an
``Idempotency-Key`` header. Shapes are documented in docs/contracts.md "Imports".
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from lectern import idempotency, imports
from lectern.editing import Journal
from lectern.fsops import LIBRARY_LOCK
from lectern.models import Import, ImportStatus, Role
from lectern.security import audit
from lectern.security.deps import (
    AuthContext,
    DbDep,
    SettingsDep,
    require_role,
    require_role_read,
)
from lectern.security.sessions import client_ip

router = APIRouter(prefix="/imports")

Editor = Annotated[AuthContext, Depends(require_role(Role.OWNER, Role.MEMBER))]
EditorRead = Annotated[AuthContext, Depends(require_role_read(Role.OWNER, Role.MEMBER))]
IdemKey = Annotated[str | None, Header(alias="idempotency-key", pattern=idempotency.KEY_PATTERN)]


def _raise(exc: imports.ImportError_):
    raise HTTPException(exc.status, {"detail": exc.code, **exc.extra}) from None


def _owned(db, auth: AuthContext, import_id: str) -> Import:
    try:
        return imports.get_owned(db, auth.user, import_id)
    except imports.ImportError_ as exc:
        _raise(exc)


class CreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="", max_length=256)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_import(
    response: Response,
    auth: Editor,
    db: DbDep,
    settings: SettingsDep,
    body: CreateIn,
    key: IdemKey = None,
) -> dict:
    try:
        imp, created = imports.create(db, auth.user, body.label, key)
    except imports.ImportError_ as exc:
        _raise(exc)
    db.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return imports.import_json(db, settings, imp)


@router.get("")
def list_imports(
    auth: EditorRead,
    db: DbDep,
    settings: SettingsDep,
    status_: str = Query(default="open", alias="status", pattern="^(open|all|ready|filed)$"),
) -> dict:
    stmt = select(Import).where(Import.user_id == auth.user.id)
    if status_ == "open":
        stmt = stmt.where(Import.status.in_((*imports.OPEN, ImportStatus.FAILED)))
    elif status_ == "ready":
        stmt = stmt.where(Import.status == ImportStatus.READY)
    elif status_ == "filed":
        stmt = stmt.where(Import.status == ImportStatus.FILED)
    rows = db.scalars(stmt.order_by(Import.created_at.desc()).limit(200)).all()
    out = [imports.import_json(db, settings, i) for i in rows]
    db.commit()  # refreshed proposals
    return {"imports": out}


@router.get("/{import_id}")
def get_import(auth: EditorRead, db: DbDep, settings: SettingsDep, import_id: str) -> dict:
    out = imports.import_json(db, settings, _owned(db, auth, import_id))
    db.commit()
    return out


@router.get("/{import_id}/cover.jpg", include_in_schema=False)
def import_cover(auth: EditorRead, db: DbDep, settings: SettingsDep, import_id: str):
    imp = _owned(db, auth, import_id)
    path = imports.staging_dir(settings, imp) / "cover.jpg"
    if imp.status not in imports.OPEN or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No cover")
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=600"}
    )


@router.patch("/{import_id}")
def update_import(
    auth: Editor, db: DbDep, settings: SettingsDep, import_id: str, body: imports.DraftPatch
) -> dict:
    imp = _owned(db, auth, import_id)
    try:
        imports.update_draft(db, imp, body)
    except imports.ImportError_ as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    return imports.import_json(db, settings, imp, refresh=False)


def _file(request, auth, db, settings, imp: Import, decision: imports.Decision, *, auto: bool):
    journal = Journal()
    with LIBRARY_LOCK:
        try:
            work, report = imports.file(db, settings, auth.user, imp, decision, journal, auto=auto)
            if not report.get("already_filed"):
                audit.record(
                    db,
                    "import.auto_file" if auto else "import.file",
                    actor_user_id=auth.user.id,
                    target=work.title[:256],
                    ip=client_ip(request),
                    detail=(
                        f"work:{work.id} {'new' if report['created'] else 'joined'}; "
                        f"{len(imp.files)} file(s); import:{imp.public_id}"
                    )[:500],
                )
            db.commit()
        except imports.ImportError_ as exc:
            db.rollback()
            journal.rollback()
            _raise(exc)
        except Exception:
            db.rollback()
            journal.rollback()
            raise
    imports.cleanup_staging(settings, imp)
    return work, report


@router.post("/{import_id}/complete")
def complete_import(
    request: Request, auth: Editor, db: DbDep, settings: SettingsDep, import_id: str
) -> dict:
    """All files are uploaded: detect metadata and propose a match. With the uploader's
    ``auto_file_imports`` preference, a certain match is filed in the same call."""
    return complete(request, auth, db, settings, import_id, auto_file=True)


def complete(request, auth, db, settings, import_id: str, *, auto_file: bool) -> dict:
    """``complete_import``; ``auto_file=False`` never files (an AI key without ``file``)."""
    imp = _owned(db, auth, import_id)
    try:
        imports.complete(db, settings, auth.user, imp)
    except imports.ImportError_ as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    if auto_file and imports.auto_file_eligible(db, auth.user, imp):
        try:
            _file(request, auth, db, settings, imp, imports.Decision(), auto=True)
        except HTTPException:
            pass  # stays ready for review; the reason shows when the person files it
    return imports.import_json(db, settings, imp)


@router.post("/{import_id}/file")
def file_import(
    request: Request,
    auth: Editor,
    db: DbDep,
    settings: SettingsDep,
    import_id: str,
    body: imports.Decision,
    key: IdemKey = None,
) -> dict:
    scope = f"import.file:{import_id}"
    payload = body.model_dump(mode="json")
    replay = idempotency.lookup(db, auth.user.id, scope, key, payload)
    if replay is not None:
        return replay
    imp = _owned(db, auth, import_id)
    work, report = _file(request, auth, db, settings, imp, body, auto=False)
    out = {
        "import": imports.import_json(db, settings, imp, refresh=False),
        "work_id": work.id,
        "report": report,
    }
    idempotency.store(db, auth.user.id, scope, key, payload, out)
    db.commit()
    return out


class CombineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    other_id: str = Field(max_length=32)


@router.post("/{import_id}/combine")
def combine_imports(
    request: Request,
    auth: Editor,
    db: DbDep,
    settings: SettingsDep,
    import_id: str,
    body: CombineIn,
) -> dict:
    """Move the other import's files into this one (an EPUB and its audiobook, or loose MP3s
    of one book, uploaded separately)."""
    imp, other = _owned(db, auth, import_id), _owned(db, auth, body.other_id)
    with LIBRARY_LOCK:
        try:
            imports.combine(db, settings, imp, other)
        except imports.ImportError_ as exc:
            db.rollback()
            _raise(exc)
        audit.record(
            db,
            "import.combine",
            actor_user_id=auth.user.id,
            target=(imp.label or imp.public_id)[:256],
            ip=client_ip(request),
            detail=f"{other.public_id} -> {imp.public_id}",
        )
        db.commit()
    return imports.import_json(db, settings, imp)


@router.delete("/{import_id}/files/{file_id}")
def remove_import_file(
    auth: Editor, db: DbDep, settings: SettingsDep, import_id: str, file_id: int
) -> dict:
    imp = _owned(db, auth, import_id)
    with LIBRARY_LOCK:
        try:
            imports.remove_file(db, settings, imp, file_id)
        except imports.ImportError_ as exc:
            db.rollback()
            _raise(exc)
        db.commit()
    return imports.import_json(db, settings, imp)


@router.delete("/{import_id}", status_code=status.HTTP_204_NO_CONTENT)
def discard_import(
    request: Request, auth: Editor, db: DbDep, settings: SettingsDep, import_id: str
) -> Response:
    imp = _owned(db, auth, import_id)
    if imp.status in (ImportStatus.DISCARDED,):
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    with LIBRARY_LOCK:
        try:
            imports.discard(db, settings, imp)
        except imports.ImportError_ as exc:
            db.rollback()
            _raise(exc)
        audit.record(
            db,
            "import.discard",
            actor_user_id=auth.user.id,
            target=(imp.label or imp.public_id)[:256],
            ip=client_ip(request),
            detail=f"{len(imp.files)} file(s)",
        )
        db.commit()
    imports.cleanup_staging(settings, imp)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
