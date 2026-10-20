"""Resumable browser uploads (docs/contracts.md "Resumable uploads")."""

from __future__ import annotations

import hashlib
import re
import shutil
import threading
from datetime import timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from lectern.ingest.audio import AudioError, probe
from lectern.ingest.epub import EpubError, read_epub_metadata
from lectern.ingest.hashing import sha256_file
from lectern.models import UPLOAD_CHUNK_MAX_BYTES, Role, UploadSession, UploadStatus, User, utcnow
from lectern.routes.api.serializers import iso
from lectern.security import audit
from lectern.security.deps import AuthContext, DbDep, SettingsDep, require_role
from lectern.security.sessions import client_ip
from lectern.uploads import MEDIA_TYPES, inbox_name_for, move_into_inbox, new_public_id, part_path

router = APIRouter(prefix="/uploads")

Uploader = Annotated[AuthContext, Depends(require_role(Role.OWNER, Role.MEMBER))]


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ZIP_MAGIC = b"PK\x03\x04"
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _err(code: int, detail: str, **extra) -> HTTPException:
    headers = extra.pop("headers", None)
    return HTTPException(code, {"detail": detail, **extra}, headers=headers)


def session_json(s: UploadSession, users: dict[int, str] | None = None) -> dict:
    out = {
        "id": s.public_id,
        "filename": s.filename,
        "media_type": s.media_type,
        "size": s.size,
        "chunk_size": s.chunk_size,
        "received_bytes": s.received_bytes,
        "status": s.status.value,
        "error": s.error,
        "created_at": iso(s.created_at),
        "expires_at": iso(s.expires_at),
        "completed_at": iso(s.completed_at),
        "inbox_name": s.inbox_name,
    }
    if users is not None:
        out["user"] = users.get(s.user_id)
    return out


def _own(db, auth: AuthContext, public_id: str, *, owner_may: bool = False) -> UploadSession:
    row = db.scalar(select(UploadSession).where(UploadSession.public_id == public_id))
    if row is None or (
        row.user_id != auth.user.id and not (owner_may and auth.user.role == Role.OWNER)
    ):
        raise _err(status.HTTP_404_NOT_FOUND, "not_found")
    return row


class CreateIn(BaseModel):
    filename: str = Field(min_length=1, max_length=600)
    size: int = Field(gt=0)
    media_type: str = Field(max_length=64)
    sha256: str | None = Field(default=None, pattern=_HEX64.pattern)


@router.post("", status_code=status.HTTP_201_CREATED)
def create(
    request: Request,
    auth: Uploader,
    db: DbDep,
    settings: SettingsDep,
    body: CreateIn,
    idempotency_key: Annotated[str | None, Header(max_length=64)] = None,
):
    now = utcnow()
    if idempotency_key:
        prior = db.scalar(
            select(UploadSession).where(
                UploadSession.user_id == auth.user.id,
                UploadSession.idempotency_key == idempotency_key,
                UploadSession.created_at >= now - timedelta(hours=24),
            )
        )
        if prior is not None:
            return JSONResponse(session_json(prior), status_code=status.HTTP_200_OK)
    if body.size > settings.upload_max_bytes:
        raise _err(status.HTTP_413_CONTENT_TOO_LARGE, "too_large", limit=settings.upload_max_bytes)
    if body.media_type not in MEDIA_TYPES:
        raise _err(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "unsupported_media_type")
    active = db.execute(
        select(func.count(), func.coalesce(func.sum(UploadSession.size), 0)).where(
            UploadSession.user_id == auth.user.id, UploadSession.status == UploadStatus.ACTIVE
        )
    ).one()
    if active[0] >= settings.upload_max_active:
        raise _err(status.HTTP_429_TOO_MANY_REQUESTS, "too_many_uploads")
    if active[1] + body.size > settings.upload_quota_bytes:
        raise _err(
            status.HTTP_413_CONTENT_TOO_LARGE, "upload_quota", limit=settings.upload_quota_bytes
        )
    public_id = new_public_id()
    part = part_path(settings, public_id)
    if shutil.disk_usage(part.parent).free < body.size:
        raise _err(status.HTTP_507_INSUFFICIENT_STORAGE, "insufficient_storage")
    row = UploadSession(
        public_id=public_id,
        user_id=auth.user.id,
        filename=Path(body.filename).name[:600] or "upload",
        media_type=body.media_type,
        size=body.size,
        chunk_size=min(UPLOAD_CHUNK_MAX_BYTES, body.size),
        sha256=body.sha256,
        expires_at=now + timedelta(seconds=settings.upload_session_ttl_seconds),
        idempotency_key=idempotency_key,
    )
    db.add(row)
    db.commit()  # the row exists before the file, so a concurrent sweep never sees an orphan
    part.touch()
    audit.record(
        db,
        "upload.start",
        actor_user_id=auth.user.id,
        target=row.filename[:256],
        ip=client_ip(request),
        detail=f"{body.size} bytes",
    )
    db.commit()
    return session_json(row)


@router.get("")
def list_sessions(auth: Uploader, db: DbDep) -> dict:
    stmt = select(UploadSession).order_by(UploadSession.created_at.desc()).limit(100)
    if auth.user.role != Role.OWNER:
        stmt = stmt.where(UploadSession.user_id == auth.user.id)
        return {"uploads": [session_json(s) for s in db.scalars(stmt)]}
    users = {u.id: u.username for u in db.scalars(select(User))}
    return {"uploads": [session_json(s, users) for s in db.scalars(stmt)]}


@router.get("/{public_id}")
def get_session(public_id: str, auth: Uploader, db: DbDep) -> dict:
    return session_json(_own(db, auth, public_id))


def _expire_if_due(db, settings, row: UploadSession) -> None:
    if row.status == UploadStatus.ACTIVE and row.expires_at < utcnow():
        row.status = UploadStatus.EXPIRED
        row.error = "expired"
        part_path(settings, row.public_id).unlink(missing_ok=True)
        db.commit()


def _truncate(path: Path, size: int) -> None:
    with path.open("r+b") as fh:
        fh.truncate(size)


def _open_session(db, auth, settings, public_id: str) -> int:
    """The session's chunk size, or 404/410. Runs in the threadpool (database work)."""
    row = _own(db, auth, public_id)
    _expire_if_due(db, settings, row)
    if row.status != UploadStatus.ACTIVE:
        raise _err(status.HTTP_410_GONE, "gone", status=row.status.value)
    return row.chunk_size


def _store_chunk(db, auth, settings, public_id: str, offset: int, digest: str, data: bytes):
    """Check the offset and checksum and append ``data``. Runs in the threadpool: database,
    hashing, and file work never block the event loop. One chunk at a time per session."""
    with _locks_guard:
        lock = _locks.setdefault(public_id, threading.Lock())
    with lock:
        row = _own(db, auth, public_id)
        db.refresh(row)
        if row.status != UploadStatus.ACTIVE:
            raise _err(status.HTTP_410_GONE, "gone", status=row.status.value)
        received = row.received_bytes
        if offset != received:
            if offset + len(data) == received and row.last_chunk_sha256 == digest:
                return received  # a retry of the chunk we already have
            raise _err(
                status.HTTP_409_CONFLICT,
                "offset_mismatch",
                received_bytes=received,
                headers={"Upload-Offset": str(received)},
            )
        if len(data) > min(row.chunk_size, row.size - received):
            raise _err(
                status.HTTP_413_CONTENT_TOO_LARGE, "chunk_too_large", chunk_size=row.chunk_size
            )
        if not data:
            raise _err(status.HTTP_400_BAD_REQUEST, "empty_chunk")
        if received + len(data) < row.size and len(data) != row.chunk_size:
            raise _err(status.HTTP_400_BAD_REQUEST, "short_chunk", chunk_size=row.chunk_size)
        if hashlib.sha256(data).hexdigest() != digest:
            raise _err(status.HTTP_422_UNPROCESSABLE_CONTENT, "chunk_checksum")
        part = part_path(settings, public_id)
        if not part.exists():
            row.status, row.error = UploadStatus.FAILED, "partial file missing"
            db.commit()
            raise _err(status.HTTP_410_GONE, "gone", status=row.status.value)
        try:
            with part.open("r+b") as fh:
                fh.seek(received)
                fh.truncate()
                fh.write(data)
        except BaseException:
            _truncate(part, received)
            raise
        row.received_bytes = received + len(data)
        row.last_chunk_sha256 = digest
        row.expires_at = utcnow() + timedelta(seconds=settings.upload_session_ttl_seconds)
        db.commit()
        return row.received_bytes


@router.put("/{public_id}/chunk", status_code=status.HTTP_204_NO_CONTENT)
async def put_chunk(
    public_id: str,
    request: Request,
    auth: Uploader,
    db: DbDep,
    settings: SettingsDep,
    upload_offset: Annotated[str | None, Header()] = None,
    x_chunk_sha256: Annotated[str | None, Header()] = None,
):
    """Only reading the body happens on the event loop (bounded by the chunk size, so at most
    50 MiB is buffered per request); everything else runs in the threadpool."""
    ip = client_ip(request) or "?"
    if not request.app.state.limiters["uploads"].allow(ip):
        raise _err(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited")
    chunk_size = await run_in_threadpool(_open_session, db, auth, settings, public_id)
    if upload_offset is None or not upload_offset.isdigit() or len(upload_offset) > 19:
        raise _err(status.HTTP_400_BAD_REQUEST, "upload_offset_required")
    digest = (x_chunk_sha256 or "").lower()
    if not _HEX64.match(digest):
        raise _err(status.HTTP_400_BAD_REQUEST, "chunk_sha256_required")
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > chunk_size:
        raise _err(status.HTTP_413_CONTENT_TOO_LARGE, "chunk_too_large", chunk_size=chunk_size)
    body = bytearray()
    async for block in request.stream():
        body += block
        if len(body) > chunk_size:
            raise _err(status.HTTP_413_CONTENT_TOO_LARGE, "chunk_too_large", chunk_size=chunk_size)
    received = await run_in_threadpool(
        _store_chunk, db, auth, settings, public_id, int(upload_offset), digest, bytes(body)
    )
    return Response(
        status_code=status.HTTP_204_NO_CONTENT, headers={"Upload-Offset": str(received)}
    )


class FinalizeIn(BaseModel):
    sha256: str | None = Field(default=None, pattern=_HEX64.pattern)


def _fail(db, request, auth, settings, row: UploadSession, code: int, detail: str):
    row.status, row.error = UploadStatus.FAILED, detail
    part_path(settings, row.public_id).unlink(missing_ok=True)
    audit.record(
        db,
        "upload.fail",
        actor_user_id=auth.user.id,
        target=row.filename[:256],
        ip=client_ip(request),
        detail=detail,
    )
    db.commit()
    return _err(code, detail)


def _title(path: Path, media_type: str, settings) -> str | None:
    try:
        if media_type == "application/epub+zip":
            title = read_epub_metadata(path).title.strip()[:200]
            return None if title == path.stem else title
        info = probe(path, settings.ffprobe_path)
        return (info.book_title or info.title or "")[:200] or None
    except (EpubError, AudioError, OSError):
        return None


@router.post("/{public_id}/finalize")
def finalize(
    public_id: str,
    request: Request,
    auth: Uploader,
    db: DbDep,
    settings: SettingsDep,
    body: FinalizeIn | None = None,
) -> dict:
    row = _own(db, auth, public_id)
    if row.status == UploadStatus.COMPLETED:
        return {**session_json(row), "queued": row.inbox_name, "title": None}
    _expire_if_due(db, settings, row)
    if row.status != UploadStatus.ACTIVE:
        raise _err(status.HTTP_410_GONE, "gone", status=row.status.value)
    if row.received_bytes != row.size:
        raise _err(status.HTTP_409_CONFLICT, "incomplete", received_bytes=row.received_bytes)
    supplied = body.sha256 if body else None
    if row.sha256 and supplied and supplied != row.sha256:
        raise _err(status.HTTP_400_BAD_REQUEST, "sha256_conflict")
    expected = row.sha256 or supplied
    if not expected:
        raise _err(status.HTTP_400_BAD_REQUEST, "sha256_required")
    part = part_path(settings, public_id)
    if not part.is_file() or sha256_file(part) != expected:
        raise _fail(db, request, auth, settings, row, 422, "checksum_mismatch")
    with part.open("rb") as fh:
        head = fh.read(4)
    if row.media_type == "application/epub+zip" and not head.startswith(_ZIP_MAGIC):
        raise _fail(db, request, auth, settings, row, 422, "not_epub")
    title = _title(part, row.media_type, settings)
    if row.media_type != "application/epub+zip" and title is None:
        try:
            probe(part, settings.ffprobe_path)
        except AudioError:
            raise _fail(db, request, auth, settings, row, 422, "not_audio") from None
    name = inbox_name_for(row.filename, row.media_type)
    move_into_inbox(settings, part, name)
    row.sha256 = expected
    row.status = UploadStatus.COMPLETED
    row.inbox_name = name
    row.completed_at = utcnow()
    audit.record(
        db,
        "upload.complete",
        actor_user_id=auth.user.id,
        target=(title or row.filename)[:256],
        ip=client_ip(request),
        detail=name,
    )
    db.commit()
    _locks.pop(public_id, None)
    return {**session_json(row), "queued": name, "title": title}


@router.delete("/{public_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel(
    public_id: str, request: Request, auth: Uploader, db: DbDep, settings: SettingsDep
) -> Response:
    row = _own(db, auth, public_id, owner_may=True)
    if row.status == UploadStatus.ACTIVE:
        row.status = UploadStatus.CANCELLED
        part_path(settings, public_id).unlink(missing_ok=True)
        audit.record(
            db,
            "upload.cancel",
            actor_user_id=auth.user.id,
            target=row.filename[:256],
            ip=client_ip(request),
        )
        db.commit()
        _locks.pop(public_id, None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
