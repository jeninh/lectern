"""Browser uploads: stream an EPUB into the watched inbox, where the normal ingest picks it up."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from lectern.ingest.library import safe_component
from lectern.models import Role
from lectern.security import audit
from lectern.security.deps import AuthContext, DbDep, SettingsDep, require_role
from lectern.security.sessions import client_ip

router = APIRouter(prefix="/books")

Uploader = Annotated[AuthContext, Depends(require_role(Role.OWNER, Role.MEMBER))]
_ZIP_MAGIC = b"PK\x03\x04"


@router.post("/upload", status_code=status.HTTP_202_ACCEPTED)
async def upload_book(
    request: Request,
    auth: Uploader,
    db: DbDep,
    settings: SettingsDep,
    content_type: Annotated[str, Header()] = "",
    x_filename: Annotated[str, Header(max_length=600)] = "book.epub",
) -> dict:
    """Body is the raw EPUB (``Content-Type: application/epub+zip``); the name rides in a header.

    Auth and CSRF run before any of the body is read, and the size cap is enforced while
    streaming, so unauthenticated or oversized requests never touch the disk.
    """
    if content_type.split(";")[0].strip().lower() != "application/epub+zip":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Upload an EPUB file")
    limit = settings.upload_max_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "File is too large")

    stem = safe_component(Path(unquote(x_filename)).stem, "upload")[:80]
    inbox = settings.inbox_path
    inbox.mkdir(parents=True, exist_ok=True)
    tmp = inbox / f".upload-{secrets.token_hex(8)}.part"  # dot-prefixed: the watcher skips it
    size = 0
    head = b""
    try:
        with tmp.open("wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "File is too large")
                if len(head) < 4:
                    head += chunk[: 4 - len(head)]
                fh.write(chunk)
        if not head.startswith(_ZIP_MAGIC):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That is not an EPUB file")
        dest = inbox / f"{stem}-{secrets.token_hex(3)}.epub"
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)

    audit.record(
        db,
        "book.upload",
        actor_user_id=auth.user.id,
        target=dest.name,
        detail=f"{size} bytes",
        ip=client_ip(request),
    )
    db.commit()
    return {"queued": dest.name, "size": size}
