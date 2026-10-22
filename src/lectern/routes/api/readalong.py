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

from lectern.access import can_edit_library, can_see_work
from lectern.align.jobs import SYNTHESIZED_PREFIX, alignment_activity
from lectern.ingest.epub import EpubError, read_spine
from lectern.ingest.sentences import primary_epub
from lectern.models import (
    Alignment,
    AlignmentStatus,
    BookFile,
    FileKind,
    Role,
    User,
    Work,
)
from lectern.readaloud.cache import ReadAloudBuilder, Unavailable, gather_inputs
from lectern.readaloud.text import SpineText
from lectern.security import audit, downloads
from lectern.security.deps import Auth, AuthCsrf, DbDep, SettingsDep
from lectern.security.sessions import client_ip

router = APIRouter()


def visible_work(db, user: User, work_id: int) -> Work:
    work = db.get(Work, work_id)
    if work is None or not can_see_work(db, user, work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return work


def alignment_json(a: Alignment | None, *, detailed: bool = True) -> dict | None:
    if a is None:
        return None
    return {
        "status": a.status.value,
        "model": a.model,
        # "synthesis": a generated audiobook's own timings; no alignment run made them
        "source": "synthesis" if a.model.startswith(SYNTHESIZED_PREFIX) else "alignment",
        "coverage": a.coverage,
        "error": a.error if detailed else None,  # paths and logs: owners and members only
    }


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
    request: Request,
    auth: Auth,
    db: DbDep,
    settings: SettingsDep,
    work_id: int,
    spine_index: int,
) -> dict:
    """The text the read-along view shows, in the same coordinates as sentence maps.

    Blocks carry ``start`` (offset of their first character in the spine item's normalised
    text) so a client can place sentences without re-implementing the normalisation. Markup
    never reaches the browser.

    The whole book's text is one download per session and work per hour (caps, audit, burst
    alerts), charged on the first chapter; later chapters in that hour are free.
    """
    work = visible_work(db, auth.user, work_id)
    alignment = current_alignment(db, work_id)
    stmt = select(BookFile).where(BookFile.work_id == work_id, BookFile.kind == FileKind.EBOOK)
    book = None
    if alignment is not None:
        book = db.scalar(stmt.where(BookFile.sha256 == alignment.epub_sha256))
    book = book or primary_epub(db, work_id)
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
    downloads.admit_once(
        db,
        settings,
        auth.user,
        downloads.Device.session(auth.session.id),
        f"text:{work_id}",
        client_ip(request),
        "read-along text",
    )
    return {"work_id": work_id, "file_id": book.id, "spine_index": spine_index, **data}


# --- read-aloud EPUB ---------------------------------------------------------------------------


def builder(request: Request) -> ReadAloudBuilder:
    return request.app.state.readaloud


def read_aloud_status(
    db, settings, work: Work, build: ReadAloudBuilder, *, detailed: bool = True
) -> dict:
    """Formats-card state: unavailable, not_generated, generating, ready, or failed.
    ``detailed`` (owners and members) includes alignment error text and the Retry model."""
    alignment = current_alignment(db, work.id)
    out: dict = {
        "work_id": work.id,
        "status": "unavailable",
        "reason": None,
        "message": None,
        "readalong": bool(
            alignment and alignment.status == AlignmentStatus.SUCCEEDED and work.readalong_enabled
        ),
        "readalong_enabled": work.readalong_enabled,
        "alignment": alignment_json(alignment, detailed=detailed),
        "download_url": None,
        "size": None,
        "generated_at": None,
        "duration_s": None,
    }
    if alignment is not None:
        # live progress, ETA, error, and whether anything can run it (the Formats card)
        out["alignment"].update(alignment_activity(db, work.id, settings, detailed=detailed))
    try:
        inputs = gather_inputs(db, settings, work)
    except Unavailable as exc:
        out.update(reason=exc.code, message=exc.message)
        if exc.code in ("no_audio", "missing_file", "needs_epub"):
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
    return read_aloud_status(
        db, settings, work, builder(request), detailed=can_edit_library(auth.user)
    )


@router.post("/works/{work_id}/read-aloud", status_code=status.HTTP_202_ACCEPTED)
def build_read_aloud(
    request: Request,
    auth: AuthCsrf,
    db: DbDep,
    settings: SettingsDep,
    work_id: int,
    force: bool = False,
) -> dict:
    """Start building the read-aloud EPUB in the background; poll ``GET`` for the state.
    ``force=true`` (owners and members, from the editor) throws the cached file away first."""
    work = visible_work(db, auth.user, work_id)
    if force and auth.user.role not in (Role.OWNER, Role.MEMBER):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
    try:
        inputs = gather_inputs(db, settings, work)
    except Unavailable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc
    if force:
        builder(request).invalidate(inputs)
        audit.record(
            db,
            "readaloud.regenerate",
            actor_user_id=auth.user.id,
            target=f"work:{work.id}",
            ip=client_ip(request),
        )
        db.commit()
    builder(request).start(inputs)
    return read_aloud_status(
        db, settings, work, builder(request), detailed=can_edit_library(auth.user)
    )
