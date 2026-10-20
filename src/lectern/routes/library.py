"""Cover and download endpoints shared by the SPA (cookie auth) and OPDS (Basic auth)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from lectern.models import BookFile, User, Work
from lectern.readaloud.cache import Unavailable, gather_inputs
from lectern.readaloud.overlay import OverlayError
from lectern.routes.api.readalong import visible_work
from lectern.security import audit
from lectern.security.deps import Auth, DbDep, SettingsDep
from lectern.security.sessions import client_ip

router = APIRouter()

PAGE_SIZE = 50


def search_works(
    db: Session, q: str | None, page: int, page_size: int = PAGE_SIZE
) -> tuple[list[Work], int]:
    stmt = select(Work).options(selectinload(Work.files))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Work.title.ilike(like), Work.author.ilike(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Work.sort_title).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return rows, total


def resolve_file(db: Session, settings, file_id: int) -> tuple[BookFile, Path]:
    book = db.get(BookFile, file_id)
    if book is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    work = db.get(Work, book.work_id)
    root = settings.library_dir.resolve()
    path = (root / work.dir_path / book.rel_path).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File missing")
    return book, path


def resolve_cover(db: Session, settings, work_id: int) -> Path:
    work = db.get(Work, work_id)
    if work is None or not work.has_cover:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No cover")
    root = settings.library_dir.resolve()
    path = (root / work.dir_path / "cover.jpg").resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No cover")
    return path


def download_response(
    db: Session, book: BookFile, path: Path, user: User, ip: str | None, via: str
) -> FileResponse:
    work = db.get(Work, book.work_id)
    audit.record(db, "download", actor_user_id=user.id, target=f"file:{book.id}", ip=ip, detail=via)
    db.commit()
    filename = f"{work.author} - {work.title}.epub".replace("/", "-")[:200]
    return FileResponse(path, media_type=book.media_type, filename=filename)


@router.get("/library/works/{work_id}/cover.jpg", include_in_schema=False)
def work_cover(auth: Auth, db: DbDep, settings: SettingsDep, work_id: int):
    resp = FileResponse(resolve_cover(db, settings, work_id), media_type="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@router.get("/library/files/{file_id}/download", include_in_schema=False)
def work_download(request: Request, auth: Auth, db: DbDep, settings: SettingsDep, file_id: int):
    book, path = resolve_file(db, settings, file_id)
    return download_response(db, book, path, auth.user, client_ip(request), "web")


@router.get("/library/works/{work_id}/read-aloud.epub", include_in_schema=False)
def read_aloud_download(
    request: Request, auth: Auth, db: DbDep, settings: SettingsDep, work_id: int
) -> FileResponse:
    """The work's EPUB with Media Overlays. Built on first request if not cached
    (docs/contracts.md); the plain ``book.epub`` is never touched."""
    work = visible_work(db, auth.user, work_id)
    try:
        inputs = gather_inputs(db, settings, work)
        request.app.state.readaloud.ensure(inputs)
    except Unavailable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc
    except OverlayError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    audit.record(
        db,
        "download",
        actor_user_id=auth.user.id,
        target=f"read-aloud:{work.id}",
        ip=client_ip(request),
        detail="web read-aloud",
    )
    db.commit()
    filename = f"{work.author} - {work.title} (read-aloud).epub".replace("/", "-")[:200]
    return FileResponse(inputs.output, media_type="application/epub+zip", filename=filename)
