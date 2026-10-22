"""Covers, downloads, the web reader's EPUB, and audio streaming.

Shared by the SPA (session cookie) and OPDS clients (HTTP Basic app password). Every route
checks visibility through the work (``lectern.access``) and answers 404 for works the caller
may not see. Whole-file transfers pass the hourly download caps (``security.downloads``);
streaming for playback does not.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from lectern import covergen, device_queue
from lectern.access import can_see_work, visible_works
from lectern.http_range import etag_matches, file_response
from lectern.ingest.library import safe_component
from lectern.ingest.sentences import primary_epub
from lectern.jobs import MEDIA_TYPE, find_variant, variant_sha256
from lectern.models import AudioTrack, BookFile, FileKind, User, Work
from lectern.readaloud.cache import Unavailable, gather_inputs
from lectern.readaloud.overlay import OverlayError
from lectern.security import audit, downloads
from lectern.security.deps import DbDep, LibraryUser, SettingsDep
from lectern.security.sessions import client_ip

router = APIRouter()
log = logging.getLogger("lectern.library")

PAGE_SIZE = 50


def search_works(
    db: Session, user: User, q: str | None, page: int, page_size: int = PAGE_SIZE
) -> tuple[list[Work], int]:
    stmt = select(Work).options(selectinload(Work.files)).where(visible_works(user))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Work.title.ilike(like), Work.author.ilike(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Work.sort_title).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return rows, total


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


def _inside(root: Path, path: Path) -> bool:
    return root in path.parents and path.is_file()


def resolve_file(db: Session, settings, file_id: int, user: User) -> tuple[BookFile, Path]:
    book = db.get(BookFile, file_id)
    if book is None or not can_see_work(db, user, book.work_id):
        raise _not_found()
    work = db.get(Work, book.work_id)
    root = settings.library_dir.resolve()
    path = (root / work.dir_path / book.rel_path).resolve()
    if not _inside(root, path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File missing")
    return book, path


def resolve_cover(db: Session, settings, work_id: int, user: User) -> Path | bytes:
    """The work's cover art, or (when it has none) its generated cover, cached under
    ``derived/``. Cover art always wins: a generated cover never replaces ``cover.jpg``."""
    work = db.get(Work, work_id)
    if work is None or not can_see_work(db, user, work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No cover")
    root = settings.library_dir.resolve()
    work_dir = (root / work.dir_path).resolve()
    if root not in work_dir.parents:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No cover")
    path = (work_dir / "cover.jpg").resolve()
    if work.has_cover and _inside(root, path) and path.is_file():
        return path
    return covergen.cached_cover(work_dir, covergen.design_of(work))


def cover_response(found: Path | bytes) -> Response:
    if isinstance(found, bytes):
        return Response(found, media_type="image/jpeg")
    return FileResponse(found, media_type="image/jpeg")


def resolve_track(
    db: Session, settings, track_id: int, user: User
) -> tuple[AudioTrack, Work, Path]:
    track = db.get(AudioTrack, track_id)
    if track is None or not can_see_work(db, user, track.work_id):
        raise _not_found()
    work = db.get(Work, track.work_id)
    root = settings.library_dir.resolve()
    path = (root / work.dir_path / track.rel_path).resolve()
    if not _inside(root, path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File missing")
    return track, work, path


def download_response(
    request: Request,
    db: Session,
    settings,
    book: BookFile,
    path: Path,
    user: User,
    device: downloads.Device,
    ip: str | None,
    via: str,
) -> Response:
    """A whole-file download: counted against the caps and audited, served with ranges and
    the file's SHA-256 as its ETag. ``If-None-Match`` with that ETag is a ``304`` that is
    neither counted nor a delivery. When an OPDS device (app password) receives the whole EPUB
    in one response, the work's pending item in that device's queue becomes delivered
    (lectern.device_queue); a probe, a partial range, or an interrupted transfer does not."""
    etag = f'"{book.sha256}"'
    inm = request.headers.get("if-none-match")
    if inm and etag_matches(inm, etag):  # the same test file_response applies
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-store"})
    downloads.admit(db, settings, user, device, f"file:{book.id}", ip)
    work = db.get(Work, book.work_id)
    audit.record(db, "download", actor_user_id=user.id, target=f"file:{book.id}", ip=ip, detail=via)
    db.commit()
    ext = Path(book.rel_path).suffix.lower() or ".epub"
    filename = f"{work.author} - {work.title}"[:190].replace("/", "-") + ext
    quoted = quote(filename)
    disposition = (
        f"attachment; filename*=utf-8''{quoted}"
        if quoted != filename
        else f'attachment; filename="{filename}"'
    )
    on_complete = None
    pw_id = device.app_password_id
    item = (
        device_queue.pending_item(db, pw_id, book.work_id)
        if pw_id is not None and book.kind == FileKind.EBOOK
        else None
    )
    if item is not None:
        file_id = book.id

        def on_complete() -> None:
            # Every byte was sent; a database hiccup must not turn that into a broken body.
            try:
                device_queue.mark_delivered(pw_id, file_id, ip, item)
            except Exception:  # noqa: BLE001
                log.exception("could not mark file %d delivered to device %d", file_id, pw_id)

    return file_response(
        request,
        path,
        book.media_type,
        book.sha256,
        cache_control="no-store",
        headers={"Content-Disposition": disposition},
        on_complete=on_complete,
    )


@router.get("/library/works/{work_id}/cover.jpg", include_in_schema=False)
def work_cover(auth: LibraryUser, db: DbDep, settings: SettingsDep, work_id: int):
    resp = cover_response(resolve_cover(db, settings, work_id, auth.user))
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@router.get("/library/files/{file_id}/download", include_in_schema=False)
def work_download(
    request: Request, auth: LibraryUser, db: DbDep, settings: SettingsDep, file_id: int
):
    book, path = resolve_file(db, settings, file_id, auth.user)
    via = "web" if auth.device.credential.startswith("session:") else "opds"
    return download_response(
        request, db, settings, book, path, auth.user, auth.device, client_ip(request), via
    )


@router.get("/library/works/{work_id}/book.epub", include_in_schema=False)
def reader_copy(
    request: Request,
    auth: LibraryUser,
    db: DbDep,
    settings: SettingsDep,
    work_id: int,
    offline: bool = False,
):
    """The EPUB the web reader opens. Cached by the browser (ETag = sha256), so reopening a
    book is a 304 and only the first transfer counts as a download. ``offline=1`` is the
    installed app keeping a copy (docs/offline.md): counted the same, audited as such."""
    if not can_see_work(db, auth.user, work_id):
        raise _not_found()
    book = primary_epub(db, work_id)
    if book is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no_ebook")
    book, path = resolve_file(db, settings, book.id, auth.user)
    return _reader_response(
        request, auth, db, settings, book, path, "offline" if offline else "reader"
    )


def _reader_response(request: Request, auth, db, settings, book: BookFile, path: Path, detail: str):
    """A book file for an in-browser reader: cached (ETag = sha256), so reopening is a 304 and
    only the first transfer counts as a download."""
    etag = f'"{book.sha256}"'
    inm = request.headers.get("if-none-match")
    if inm and etag in [t.strip() for t in inm.split(",")]:
        return Response(
            status_code=304, headers={"ETag": etag, "Cache-Control": "private, no-cache"}
        )
    ip = client_ip(request)
    downloads.admit(db, settings, auth.user, auth.device, f"file:{book.id}", ip)
    audit.record(
        db, "download", actor_user_id=auth.user.id, target=f"file:{book.id}", ip=ip, detail=detail
    )
    db.commit()
    resp = FileResponse(path, media_type=book.media_type, content_disposition_type="inline")
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "private, no-cache"
    return resp


@router.get("/library/files/{file_id}/view", include_in_schema=False)
def view_pdf(
    request: Request,
    auth: LibraryUser,
    db: DbDep,
    settings: SettingsDep,
    file_id: int,
    offline: bool = False,
):
    """The PDF the web viewer (pdf.js) opens. pdf.js fetches it in many Range requests, so
    opening it counts as one download per device per hour (``admit_once``), not one per range;
    reopening is a 304 (ETag = sha256)."""
    book, path = resolve_file(db, settings, file_id, auth.user)
    if book.media_type != "application/pdf":
        raise _not_found()
    etag = f'"{book.sha256}"'
    cache = {"ETag": etag, "Cache-Control": "private, no-cache"}
    inm = request.headers.get("if-none-match")
    if inm and etag in [t.strip() for t in inm.split(",")]:
        return Response(status_code=304, headers=cache)
    downloads.admit_once(
        db,
        settings,
        auth.user,
        auth.device,
        f"file:{book.id}",
        client_ip(request),
        "offline pdf" if offline else "pdf reader",
    )
    return FileResponse(
        path, media_type=book.media_type, content_disposition_type="inline", headers=cache
    )


@router.get("/library/tracks/{track_id}/stream", include_in_schema=False)
def stream_track(
    request: Request,
    auth: LibraryUser,
    db: DbDep,
    settings: SettingsDep,
    track_id: int,
    variant: str | None = None,
):
    """Range-capable playback, metered per work: once ``stream_meter_bytes`` of a work's audio
    (any tracks, any ranges) were actually sent to this credential in an hour, the audiobook
    counts once as a download (caps, audit, alerts). Declared lengths are never charged."""
    track, work, path = resolve_track(db, settings, track_id, auth.user)
    meter = downloads.StreamMeter(
        settings, auth.user.id, auth.device, f"stream:work:{work.id}", client_ip(request)
    )
    meter.gate(db, auth.user)
    if variant is None:
        return file_response(request, path, track.media_type, track.sha256, on_sent=meter.sent)
    if variant != "low":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown_variant")
    found = find_variant(settings.library_dir, work, track.sha256)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "variant_unavailable")
    vpath, codec = found
    return file_response(
        request, vpath, MEDIA_TYPE[codec], variant_sha256(vpath), on_sent=meter.sent
    )


@router.get("/library/tracks/{track_id}/download", include_in_schema=False)
def download_track(
    request: Request, auth: LibraryUser, db: DbDep, settings: SettingsDep, track_id: int
):
    """The original audio file as a download (counted against the caps, audited)."""
    track, work, path = resolve_track(db, settings, track_id, auth.user)
    ip = client_ip(request)
    downloads.admit(db, settings, auth.user, auth.device, f"track:{track.id}", ip)
    audit.record(
        db,
        "download",
        actor_user_id=auth.user.id,
        target=f"track:{track.id}",
        ip=ip,
        detail="audio",
    )
    db.commit()
    name = safe_component(f"{work.title} - {track.position + 1:02d}", "track")
    return FileResponse(path, media_type=track.media_type, filename=f"{name}{path.suffix}")


@router.get("/library/works/{work_id}/read-aloud.epub", include_in_schema=False)
def read_aloud_download(
    request: Request, auth: LibraryUser, db: DbDep, settings: SettingsDep, work_id: int
) -> FileResponse:
    """The work's EPUB with Media Overlays (milestone 4). Built on first request if not cached
    (docs/contracts.md); the plain ``book.epub`` is never touched. Same visibility and hourly
    download caps as any other whole-file transfer."""
    work = db.get(Work, work_id)
    if work is None or not can_see_work(db, auth.user, work_id):
        raise _not_found()
    try:
        inputs = gather_inputs(db, settings, work)
        request.app.state.readaloud.ensure(inputs)
    except Unavailable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc
    except OverlayError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    ip = client_ip(request)
    downloads.admit(db, settings, auth.user, auth.device, f"read-aloud:{work.id}", ip)
    audit.record(
        db,
        "download",
        actor_user_id=auth.user.id,
        target=f"read-aloud:{work.id}",
        ip=ip,
        detail="web read-aloud",
    )
    db.commit()
    filename = f"{work.author} - {work.title} (read-aloud).epub".replace("/", "-")[:200]
    return FileResponse(inputs.output, media_type="application/epub+zip", filename=filename)
