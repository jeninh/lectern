"""Covers, downloads, the web reader's EPUB, and audio streaming.

Shared by the SPA (session cookie) and OPDS clients (HTTP Basic app password). Every route
checks visibility through the work (``lectern.access``) and answers 404 for works the caller
may not see. Whole-file transfers pass the hourly download caps (``security.downloads``);
streaming for playback does not.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from lectern.access import can_see_work, visible_works
from lectern.http_range import file_response
from lectern.ingest.library import safe_component
from lectern.ingest.sentences import primary_epub
from lectern.jobs import MEDIA_TYPE, find_variant, variant_sha256
from lectern.models import AudioTrack, BookFile, User, Work
from lectern.readaloud.cache import Unavailable, gather_inputs
from lectern.readaloud.overlay import OverlayError
from lectern.security import audit, downloads
from lectern.security.deps import DbDep, LibraryUser, SettingsDep
from lectern.security.sessions import client_ip

router = APIRouter()

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


def resolve_cover(db: Session, settings, work_id: int, user: User) -> Path:
    work = db.get(Work, work_id)
    if work is None or not work.has_cover or not can_see_work(db, user, work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No cover")
    root = settings.library_dir.resolve()
    path = (root / work.dir_path / "cover.jpg").resolve()
    if not _inside(root, path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No cover")
    return path


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
    db: Session,
    settings,
    book: BookFile,
    path: Path,
    user: User,
    device: downloads.Device,
    ip: str | None,
    via: str,
) -> FileResponse:
    downloads.admit(db, settings, user, device, f"file:{book.id}", ip)
    work = db.get(Work, book.work_id)
    audit.record(db, "download", actor_user_id=user.id, target=f"file:{book.id}", ip=ip, detail=via)
    db.commit()
    filename = f"{work.author} - {work.title}.epub".replace("/", "-")[:200]
    return FileResponse(path, media_type=book.media_type, filename=filename)


@router.get("/library/works/{work_id}/cover.jpg", include_in_schema=False)
def work_cover(auth: LibraryUser, db: DbDep, settings: SettingsDep, work_id: int):
    resp = FileResponse(resolve_cover(db, settings, work_id, auth.user), media_type="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@router.get("/library/files/{file_id}/download", include_in_schema=False)
def work_download(
    request: Request, auth: LibraryUser, db: DbDep, settings: SettingsDep, file_id: int
):
    book, path = resolve_file(db, settings, file_id, auth.user)
    via = "web" if auth.device.credential.startswith("session:") else "opds"
    return download_response(
        db, settings, book, path, auth.user, auth.device, client_ip(request), via
    )


@router.get("/library/works/{work_id}/book.epub", include_in_schema=False)
def reader_copy(
    request: Request, auth: LibraryUser, db: DbDep, settings: SettingsDep, work_id: int
):
    """The EPUB the web reader opens. Cached by the browser (ETag = sha256), so reopening a
    book is a 304 and only the first transfer counts as a download."""
    if not can_see_work(db, auth.user, work_id):
        raise _not_found()
    book = primary_epub(db, work_id)
    if book is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no_ebook")
    book, path = resolve_file(db, settings, book.id, auth.user)
    etag = f'"{book.sha256}"'
    inm = request.headers.get("if-none-match")
    if inm and etag in [t.strip() for t in inm.split(",")]:
        return Response(
            status_code=304, headers={"ETag": etag, "Cache-Control": "private, no-cache"}
        )
    ip = client_ip(request)
    downloads.admit(db, settings, auth.user, auth.device, f"file:{book.id}", ip)
    audit.record(
        db, "download", actor_user_id=auth.user.id, target=f"file:{book.id}", ip=ip, detail="reader"
    )
    db.commit()
    resp = FileResponse(path, media_type=book.media_type)
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "private, no-cache"
    return resp


@router.get("/library/tracks/{track_id}/stream", include_in_schema=False)
def stream_track(
    request: Request,
    auth: LibraryUser,
    db: DbDep,
    settings: SettingsDep,
    track_id: int,
    variant: str | None = None,
):
    track, work, path = resolve_track(db, settings, track_id, auth.user)
    if variant is None:
        return file_response(request, path, track.media_type, track.sha256)
    if variant != "low":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown_variant")
    found = find_variant(settings.library_dir, work, track.sha256)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "variant_unavailable")
    vpath, codec = found
    return file_response(request, vpath, MEDIA_TYPE[codec], variant_sha256(vpath))


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
