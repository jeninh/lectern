"""Text recognition for scanned PDFs (docs/ocr.md).

- ``GET /api/files/{id}/ocr``: whether the PDF is a scan, and its OCR state (queued, running
  with progress, failed with the reason, done with the searchable copy), the languages the
  server can recognise, and the one it will use.
- ``POST /api/files/{id}/ocr`` ``{"language"?}`` (owners, members): recognise it now, again
  after a failure, or again in another language. A text PDF is refused (``has_text``).
- ``DELETE /api/files/{id}/ocr`` (owners, members): delete the searchable copy and the words
  (and stop a job that is running). The original is never touched.
- ``GET /api/files/{id}/ocr/pdf``: download the searchable copy, counted like any download.

Every route answers ``404`` for a file of a work the caller may not see.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from lectern.access import can_see_work
from lectern.fsops import LIBRARY_LOCK
from lectern.ingest import formats
from lectern.models import BookFile, Role, Work
from lectern.ocr import engine
from lectern.ocr import jobs as ocr
from lectern.security import audit, downloads
from lectern.security.deps import Auth, AuthContext, DbDep, SettingsDep, require_role
from lectern.security.sessions import client_ip

router = APIRouter()
Editor = Annotated[AuthContext, Depends(require_role(Role.OWNER, Role.MEMBER))]


class OcrIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str | None = Field(default=None, max_length=64)


def _pdf(db, user, file_id: int) -> tuple[BookFile, Work]:
    book = db.get(BookFile, file_id)
    if book is None or not can_see_work(db, user, book.work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if book.media_type != formats.PDF:
        raise HTTPException(status.HTTP_409_CONFLICT, "not_pdf")
    work = db.get(Work, book.work_id)
    if work is None or work.trashed_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return book, work


@router.get("/files/{file_id}/ocr")
def ocr_status(auth: Auth, db: DbDep, settings: SettingsDep, file_id: int) -> dict:
    book, work = _pdf(db, auth.user, file_id)
    return ocr.status_json(db, settings, book, work)


@router.post("/files/{file_id}/ocr")
def start_ocr(
    auth: Editor, db: DbDep, settings: SettingsDep, file_id: int, body: OcrIn | None = None
) -> dict:
    book, work = _pdf(db, auth.user, file_id)
    language = (body.language if body else None) or None
    if language is not None:
        try:
            engine.parse_spec(language)
        except ValueError:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_language") from None
    scan = ocr.load_scan(settings.data_dir, book.sha256) or ocr.scan_file(settings, book, work)
    if scan is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "busy")
    if "error" in scan:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, scan["error"])
    if not scan.get("needs_ocr"):
        raise HTTPException(status.HTTP_409_CONFLICT, "has_text")
    if not ocr.can_run(db, settings):
        raise HTTPException(status.HTTP_409_CONFLICT, "no_engine")
    with LIBRARY_LOCK:
        current = ocr.active_job(db, work.id)
        if current is not None and (current.payload or {}).get("file_id") == book.id:
            if language is None or language == (current.payload or {}).get("language"):
                return ocr.status_json(db, settings, book, work)
            ocr.cancel(db, current)  # a new language replaces the run in progress
            db.flush()
        try:
            ocr.enqueue(db, settings, book, work, scan, language=language, user_id=auth.user.id)
        except ocr.Busy:
            db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, "another_pdf_running") from None
        audit.record(db, "ocr.start", actor_user_id=auth.user.id, target=f"file:{book.id}",
                     detail=language or "")  # fmt: skip
        db.commit()
    return ocr.status_json(db, settings, book, work)


@router.delete("/files/{file_id}/ocr")
def delete_ocr(auth: Editor, db: DbDep, settings: SettingsDep, file_id: int) -> dict:
    book, work = _pdf(db, auth.user, file_id)
    with LIBRARY_LOCK:
        current = ocr.active_job(db, work.id)
        if current is not None and (current.payload or {}).get("file_id") == book.id:
            ocr.cancel(db, current)
        removed = ocr.delete_output(settings.library_dir, work, book.sha256)
        audit.record(db, "ocr.delete", actor_user_id=auth.user.id, target=f"file:{book.id}",
                     detail="removed" if removed else "")  # fmt: skip
        db.commit()
    return ocr.status_json(db, settings, book, work)


@router.get("/files/{file_id}/ocr/pdf")
def download_ocr_pdf(
    request: Request, auth: Auth, db: DbDep, settings: SettingsDep, file_id: int
) -> FileResponse:
    book, work = _pdf(db, auth.user, file_id)
    path = ocr.pdf_path(settings.library_dir, work, book.sha256)
    root = settings.library_dir.resolve()
    if not path.is_file() or root not in path.resolve().parents:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_recognised")
    ip = client_ip(request)
    downloads.admit(db, settings, auth.user, downloads.Device.session(auth.session.id),
                    f"ocr:{book.id}", ip)  # fmt: skip
    audit.record(db, "download", actor_user_id=auth.user.id, target=f"ocr:{book.id}", ip=ip,
                 detail="searchable PDF")  # fmt: skip
    db.commit()
    filename = f"{work.author} - {work.title} (searchable).pdf".replace("/", "-")[:200]
    return FileResponse(path, media_type=formats.PDF, filename=filename)
