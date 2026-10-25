"""Resumable upload sessions: storage, finalize hand-off to the inbox, and the sweeper.

Bytes are appended in order to ``<data>/uploads/<public_id>.part``; ``received_bytes`` is the
resume point. Finalize verifies the whole-file SHA-256 and the file type, then moves the file
into the inbox where the ordinary watcher ingests it. See docs/contracts.md "Resumable uploads".
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from lectern.config import Settings
from lectern.ingest.library import safe_component
from lectern.models import UploadSession, UploadStatus, utcnow

log = logging.getLogger("lectern.uploads")

TERMINAL_RETENTION = timedelta(days=7)
ORPHAN_GRACE_S = 600  # .part files younger than this are never treated as orphans
# media type -> (allowed extensions, default extension)
MEDIA_TYPES: dict[str, tuple[tuple[str, ...], str]] = {
    "application/epub+zip": ((".epub",), ".epub"),
    "application/pdf": ((".pdf",), ".pdf"),
    "application/vnd.comicbook+zip": ((".cbz",), ".cbz"),
    # Kindle books, converted to EPUB at finalize (lectern.ingest.kindle); never stored as is.
    "application/x-mobipocket-ebook": ((".mobi",), ".mobi"),
    "application/vnd.amazon.mobi8-ebook": ((".azw3",), ".azw3"),
    "audio/mpeg": ((".mp3",), ".mp3"),
    "audio/mp4": ((".m4b", ".m4a", ".mp4"), ".m4b"),
    "audio/ogg": ((".ogg", ".opus", ".oga"), ".ogg"),
    "audio/flac": ((".flac",), ".flac"),
}


def uploads_dir(settings: Settings) -> Path:
    d = settings.data_dir / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def part_path(settings: Settings, public_id: str) -> Path:
    return uploads_dir(settings) / f"{public_id}.part"


def new_public_id() -> str:
    return secrets.token_urlsafe(16)  # 22 URL-safe characters


def media_suffix(filename: str, media_type: str) -> str:
    """The extension a stored copy gets: the client's own only when it is one the declared
    media type allows (``.m4a`` for ``audio/mp4``), else the type's default. A client name never
    supplies an extension the allowlist does not know (a 300-character suffix, ``.exe``)."""
    allowed, default = MEDIA_TYPES[media_type]
    ext = Path(filename).suffix.lower()
    return ext if ext in allowed else default


def inbox_name_for(filename: str, media_type: str) -> str:
    stem = safe_component(Path(filename).stem, "upload")[:80]
    return f"{stem}-{secrets.token_hex(3)}{media_suffix(filename, media_type)}"


def move_into_inbox(settings: Settings, src: Path, name: str) -> Path:
    """Copy-safe move: the data volume and the library may be different filesystems, so the
    file lands under a dot-name the watcher ignores and is renamed in one step."""
    inbox = settings.inbox_path
    inbox.mkdir(parents=True, exist_ok=True)
    tmp = inbox / f".incoming-{secrets.token_hex(6)}"
    try:
        shutil.move(str(src), str(tmp))
        dest = inbox / name
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def sweep(db: Session, settings: Settings) -> dict[str, int]:
    """Expire abandoned sessions, delete orphan ``.part`` files, forget old finished rows."""
    now = utcnow()
    expired = 0
    for row in db.scalars(
        select(UploadSession).where(
            UploadSession.status == UploadStatus.ACTIVE, UploadSession.expires_at < now
        )
    ):
        row.status = UploadStatus.EXPIRED
        row.error = "expired"
        part_path(settings, row.public_id).unlink(missing_ok=True)
        expired += 1
    db.commit()
    active = set(
        db.scalars(
            select(UploadSession.public_id).where(UploadSession.status == UploadStatus.ACTIVE)
        )
    )
    orphans = 0
    young = time.time() - ORPHAN_GRACE_S
    for f in uploads_dir(settings).glob("*.part"):
        if f.stem in active:
            continue
        try:
            if f.stat().st_mtime > young:
                continue  # may belong to a session being created right now
        except FileNotFoundError:
            continue
        f.unlink(missing_ok=True)
        orphans += 1
    res = db.execute(
        delete(UploadSession).where(
            UploadSession.status != UploadStatus.ACTIVE,
            UploadSession.updated_at < now - TERMINAL_RETENTION,
        )
    )
    db.commit()
    counts = {"expired": expired, "orphans": orphans, "forgotten": res.rowcount or 0}
    if any(counts.values()):
        log.info("upload sweep: %s", counts)
    return counts
