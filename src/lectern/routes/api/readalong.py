"""Read-along and read-aloud API (milestone 4): spine text and the read-aloud EPUB.

Sentences, progress, and tracks live in :mod:`lectern.routes.api.works` (milestone 2).

Shapes follow docs/contracts.md. Every route checks visibility with
:func:`lectern.access.can_see_work` and answers ``404`` for a work the caller may not see.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from lectern.access import can_see_work
from lectern.ingest.epub import EpubError, read_spine
from lectern.models import (
    Alignment,
    AlignmentStatus,
    BookFile,
    FileKind,
    User,
    Work,
)
from lectern.readaloud.cache import ReadAloudBuilder, Unavailable, gather_inputs
from lectern.readaloud.text import SpineText
from lectern.security.deps import Auth, AuthCsrf, DbDep, SettingsDep

router = APIRouter()


def visible_work(db, user: User, work_id: int) -> Work:
    work = db.get(Work, work_id)
    if work is None or not can_see_work(db, user, work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return work


def alignment_json(a: Alignment | None) -> dict | None:
    if a is None:
        return None
    return {"status": a.status.value, "model": a.model, "coverage": a.coverage}


def current_alignment(db, work_id: int) -> Alignment | None:
    return db.scalar(select(Alignment).where(Alignment.work_id == work_id))


# --- spine text --------------------------------------------------------------------------------


@lru_cache(maxsize=64)
def _spine_blocks(path: str, sha256: str, spine_index: int) -> dict:
    """Normalised text of one spine item, split into display blocks. ``sha256`` keys the cache."""
    import zipfile

    names = read_spine(Path(path))
    if spine_index >= len(names):
        raise IndexError(spine_index)
    with zipfile.ZipFile(path) as zf:
        st = SpineText(zf.read(names[spine_index]))
    return {
        "length": len(st.text),
        "blocks": [
            {"kind": b.kind, "start": b.start, "text": st.text[b.start : b.end]}
            for b in st.blocks()
        ],
    }


@router.get("/works/{work_id}/text/{spine_index}")
def spine_text(
    auth: Auth, db: DbDep, settings: SettingsDep, work_id: int, spine_index: int
) -> dict:
    """The text the read-along view shows, in the same coordinates as sentence maps.

    Blocks carry ``start`` (offset of their first character in the spine item's normalised
    text) so a client can place sentences without re-implementing the normalisation. Markup
    never reaches the browser.
    """
    work = visible_work(db, auth.user, work_id)
    alignment = current_alignment(db, work_id)
    stmt = select(BookFile).where(BookFile.work_id == work_id, BookFile.kind == FileKind.EBOOK)
    book = None
    if alignment is not None:
        book = db.scalar(stmt.where(BookFile.sha256 == alignment.epub_sha256))
    book = book or db.scalar(stmt.order_by(BookFile.id))
    if book is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No EPUB")
    root = settings.library_dir.resolve()
    path = (root / work.dir_path / book.rel_path).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File missing")
    try:
        data = _spine_blocks(str(path), book.sha256, spine_index)
    except IndexError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such spine item") from exc
    except (EpubError, ValueError, KeyError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unreadable spine item") from exc
    return {"work_id": work_id, "file_id": book.id, "spine_index": spine_index, **data}


# --- read-aloud EPUB ---------------------------------------------------------------------------


def builder(request: Request) -> ReadAloudBuilder:
    return request.app.state.readaloud


def read_aloud_status(db, settings, work: Work, build: ReadAloudBuilder) -> dict:
    """Formats-card state: unavailable, not_generated, generating, ready, or failed."""
    alignment = current_alignment(db, work.id)
    out: dict = {
        "work_id": work.id,
        "status": "unavailable",
        "reason": None,
        "message": None,
        "readalong": bool(alignment and alignment.status == AlignmentStatus.SUCCEEDED),
        "alignment": alignment_json(alignment),
        "download_url": None,
        "size": None,
        "generated_at": None,
        "duration_s": None,
    }
    try:
        inputs = gather_inputs(db, settings, work)
    except Unavailable as exc:
        out.update(reason=exc.code, message=exc.message)
        if exc.code in ("no_audio", "missing_file"):
            out["readalong"] = False
        return out
    state, sidecar, error = build.status(inputs)
    out.update(status=state, message=error)
    if sidecar:
        out.update(
            download_url=f"/library/works/{work.id}/read-aloud.epub",
            size=sidecar["size"],
            generated_at=sidecar["generated_at"],
            duration_s=sidecar.get("duration_s"),
        )
    return out


@router.get("/works/{work_id}/read-aloud")
def read_aloud(
    request: Request, auth: Auth, db: DbDep, settings: SettingsDep, work_id: int
) -> dict:
    work = visible_work(db, auth.user, work_id)
    return read_aloud_status(db, settings, work, builder(request))


@router.post("/works/{work_id}/read-aloud", status_code=status.HTTP_202_ACCEPTED)
def build_read_aloud(
    request: Request, auth: AuthCsrf, db: DbDep, settings: SettingsDep, work_id: int
) -> dict:
    """Start building the read-aloud EPUB in the background; poll ``GET`` for the state."""
    work = visible_work(db, auth.user, work_id)
    try:
        inputs = gather_inputs(db, settings, work)
    except Unavailable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc
    builder(request).start(inputs)
    return read_aloud_status(db, settings, work, builder(request))
