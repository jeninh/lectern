"""KOReader progress sync, wire-compatible with koreader-sync-server.

Authentication is x-auth-user (Lectern username) + x-auth-key (md5 of a per-device sync key,
as KOReader computes it). Progress is stored per user, so every device of a user shares it.

Cross-format sync: ``reading_progress`` stays KOReader's own record, keyed by the document
hash. When that hash belongs to a work with a sentence list, a PUT is also projected onto
``work_progress`` (xpointer → text position → sentence, source ``koreader``), and a GET
answers with the *newer* of the two: KOReader's own record, or a web/audio position
translated back into an xpointer for the same document. The device that wrote last wins,
exactly as between two KOReader devices. Projection needs both visibility of the work and the
exact aligned edition (``_projectable``); otherwise the sync is plain koreader-sync-server.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern.access import can_see_work
from lectern.models import (
    Alignment,
    BookFile,
    FileKind,
    ProgressSource,
    ReadingProgress,
    User,
    Work,
    WorkProgress,
)
from lectern.positions import SentencePos
from lectern.progress import (
    get_progress as work_progress_of,
)
from lectern.progress import (
    sentence_index,
    sentence_to_xpointer,
    set_progress,
    xpointer_to_sentence,
)
from lectern.security.deps import DbDep, KoSync, SettingsDep

log = logging.getLogger("lectern.kosync")

router = APIRouter()

_DOC_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
PROJECTED_DEVICE_ID = "lectern"


@dataclass(frozen=True)
class _Edition:
    """The work a KOReader document belongs to, when its progress can be projected."""

    work: Work
    epub: Path


def _projectable(db: Session, library: Path, user: User, document: str) -> _Edition | None:
    """The work and EPUB file for ``document`` if, and only if, the caller may see the work
    and this exact file is the edition the work's sentences were aligned against.

    Anything else (an unknown hash, a work the caller cannot see, another edition of the
    same work, an alignment whose EPUB no longer matches) is ``None``, and the sync then
    behaves exactly like plain koreader-sync-server: nothing is read from or written to
    ``work_progress``, so a response never depends on works the caller cannot see.
    """
    books = db.scalars(
        select(BookFile)
        .where(BookFile.partial_md5 == document, BookFile.kind == FileKind.EBOOK)
        .order_by(BookFile.id)
    ).all()
    for book in books:
        if not can_see_work(db, user, book.work_id):
            continue
        alignment = db.scalar(select(Alignment).where(Alignment.work_id == book.work_id))
        if alignment is None or alignment.epub_sha256 != book.sha256:
            continue  # another edition: its DOM paths and offsets do not match the sentences
        work = db.get(Work, book.work_id)
        root = library.resolve()
        path = (root / work.dir_path / book.rel_path).resolve()
        if root in path.parents and path.is_file():
            return _Edition(work, path)
    return None


def _project_put(db: Session, library: Path, user: User, body: ProgressIn) -> None:
    edition = _projectable(db, library, user, body.document)
    if edition is None:
        return
    idx = sentence_index(db, edition.work.id)
    pos = xpointer_to_sentence(edition.epub, idx, body.progress)
    if pos is None:
        return
    set_progress(
        db,
        user_id=user.id,
        work_id=edition.work.id,
        pos=pos,
        source=ProgressSource.KOREADER,
        raw=body.progress,
        device=body.device,
    )


def _newer_projection(
    db: Session, library: Path, user: User, document: str, row: ReadingProgress | None
) -> dict | None:
    """A web/audio position written after KOReader's last sync, as KOReader would store it."""
    edition = _projectable(db, library, user, document)
    if edition is None:
        return None
    wp: WorkProgress | None = work_progress_of(db, user.id, edition.work.id)
    if wp is None or wp.source == ProgressSource.KOREADER:
        return None
    if row is not None and row.updated_at >= wp.updated_at:
        return None
    idx = sentence_index(db, edition.work.id)
    if not len(idx):
        return None
    pos = SentencePos(min(wp.sentence_n, len(idx) - 1), wp.sentence_offset)
    xpointer = sentence_to_xpointer(edition.epub, idx, pos)
    if xpointer is None:
        return None
    return {
        "document": document,
        "progress": xpointer,
        "percentage": round(pos.n / len(idx), 4),
        "device": wp.device or f"Lectern {wp.source.value}",
        "device_id": f"{PROJECTED_DEVICE_ID}-{wp.source.value}",
        "timestamp": int(wp.updated_at.timestamp()),
    }


class ProgressIn(BaseModel):
    document: str = Field(min_length=1, max_length=128)
    progress: str = Field(max_length=4096)
    percentage: float = Field(ge=0.0, le=1.0)
    device: str = Field(default="", max_length=128)
    device_id: str = Field(default="", max_length=128)

    @field_validator("document")
    @classmethod
    def _doc(cls, v: str) -> str:
        if not _DOC_RE.match(v):
            raise ValueError("bad document id")
        return v

    @field_validator("progress", mode="before")
    @classmethod
    def _progress(cls, v):
        # KOReader sends page numbers as integers for PDFs and xpointers as strings for EPUBs.
        return str(v)


@router.post("/users/create", include_in_schema=False)
async def users_create(_request: Request) -> JSONResponse:
    # Registration is closed by policy: credentials are minted in the Lectern web UI.
    # KOReader shows this message verbatim, so keep it actionable.
    return JSONResponse(
        {
            "message": "Registration is disabled. Create a sync key in Lectern (Account page) and use Login."
        },
        status_code=status.HTTP_403_FORBIDDEN,
    )


@router.get("/users/auth", include_in_schema=False)
def users_auth(auth: KoSync) -> JSONResponse:
    return JSONResponse({"authorized": "OK"})


@router.put("/syncs/progress", include_in_schema=False)
def put_progress(auth: KoSync, db: DbDep, settings: SettingsDep, body: ProgressIn) -> JSONResponse:
    now = int(time.time())
    row = db.scalar(
        select(ReadingProgress).where(
            ReadingProgress.user_id == auth.user.id, ReadingProgress.document == body.document
        )
    )
    if row is None:
        row = ReadingProgress(
            user_id=auth.user.id, document=body.document, progress="", percentage=0.0, timestamp=now
        )
        db.add(row)
    row.progress = body.progress
    row.percentage = body.percentage
    row.device = body.device
    row.device_id = body.device_id
    row.timestamp = now
    row.sync_key_id = auth.key.id
    db.flush()
    try:
        # Savepoint: a failed projection must never cost KOReader its own sync record.
        with db.begin_nested():
            _project_put(db, settings.library_dir, auth.user, body)
    except Exception:  # noqa: BLE001
        log.exception("could not project KOReader progress for %s", body.document)
    db.commit()
    return JSONResponse({"document": body.document, "timestamp": now})


@router.get("/syncs/progress/{document}", include_in_schema=False)
def get_progress(auth: KoSync, db: DbDep, settings: SettingsDep, document: str) -> JSONResponse:
    if not _DOC_RE.match(document):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"message": "Invalid document"})
    row = db.scalar(
        select(ReadingProgress).where(
            ReadingProgress.user_id == auth.user.id, ReadingProgress.document == document
        )
    )
    try:
        projected = _newer_projection(db, settings.library_dir, auth.user, document, row)
    except Exception:  # noqa: BLE001
        log.exception("could not project Lectern progress for %s", document)
        projected = None
    if projected is not None:
        return JSONResponse(projected)
    if row is None:
        return JSONResponse({})
    return JSONResponse(
        {
            "document": row.document,
            "progress": row.progress,
            "percentage": row.percentage,
            "device": row.device,
            "device_id": row.device_id,
            "timestamp": row.timestamp,
        }
    )
