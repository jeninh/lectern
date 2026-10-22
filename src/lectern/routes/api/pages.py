"""Page-based books (PDF and CBZ comics): page lists, comic page images, and page progress.

Progress for these formats is a page number, which is exactly what KOReader syncs for them, so
it lives in KOReader's own record: ``reading_progress`` keyed by the file's partial MD5 (the
document id KOReader computes from the same bytes). The web viewer reads and writes that row
too, so a page turned in the browser is where KOReader opens next, and the other way round,
with the newer write winning as between two KOReader devices.

A text PDF with a current sentence map (:mod:`lectern.pdftext.positions`) also takes part in
the work's canonical position (``work_progress``): a page saved here moves it to the first
sentence on that page (unless it is already on that page), and the page info offers the page
of a newer position from the audio player, the EPUB reader, or KOReader (``position``), which
the viewer turns into its "continue there?" prompt. Comics, scans, and PDFs whose text is not
read yet keep page numbers only.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from lectern.access import can_see_work
from lectern.ingest import formats
from lectern.ingest.comic import ComicCache, read_page
from lectern.models import BookFile, ReadingProgress, Work
from lectern.routes.api.offline import OfflineReplay, conflict
from lectern.routes.api.serializers import iso
from lectern.security import downloads
from lectern.security.deps import Auth, AuthCsrf, DbDep, SettingsDep
from lectern.security.sessions import client_ip

router = APIRouter()
WEB_DEVICE_ID = "lectern-web"


def _paged_file(db, settings, user, file_id: int):
    book = db.get(BookFile, file_id)
    if book is None or not can_see_work(db, user, book.work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if book.media_type not in formats.PAGED:
        raise HTTPException(status.HTTP_409_CONFLICT, "not_paged")
    work = db.get(Work, book.work_id)
    root = settings.library_dir.resolve()
    path = (root / work.dir_path / book.rel_path).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File missing")
    return book, work, path


def _progress(db, user_id: int, book: BookFile) -> dict | None:
    row = db.scalar(
        select(ReadingProgress).where(
            ReadingProgress.user_id == user_id, ReadingProgress.document == book.partial_md5
        )
    )
    if row is None:
        return None
    page = int(row.progress) if row.progress.isdigit() else None
    return {
        "page": page,
        "percentage": row.percentage,
        "device": row.device or ("Web reader" if row.device_id == WEB_DEVICE_ID else "KOReader"),
        "from_web": row.device_id == WEB_DEVICE_ID,
        "updated_at": iso(row.updated_at),
    }


@router.get("/files/{file_id}/pages")
def pages(auth: Auth, db: DbDep, settings: SettingsDep, file_id: int) -> dict:
    """What the page viewer needs: format, page list (comics; a PDF's count comes from the
    PDF itself), reading direction, and the synced page."""
    book, work, path = _paged_file(db, settings, auth.user, file_id)
    out = {
        "file_id": book.id,
        "work_id": work.id,
        "title": work.title,
        "media_type": book.media_type,
        "format": formats.label_of(book.media_type),
        "pages": None,
        "rtl": False,
        "url": f"/library/files/{book.id}/view",
        "progress": _progress(db, auth.user.id, book),
    }
    if book.media_type == formats.CBZ:
        try:
            comic = ComicCache.get(path, book.sha256)
        except formats.FormatError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code) from None
        out.update(pages=len(comic.pages), rtl=comic.rtl, url=None)
    else:
        from lectern.routes.api.pdftext import text_state

        out["text"] = text_state(db, settings, book, work)
        out["position"] = _canonical_page(db, settings, auth.user.id, book, out["progress"])
    return out


def _canonical_page(db, settings, user_id: int, book: BookFile, page_progress: dict | None):
    """The page of the work's canonical position when it is newer than this PDF's page record
    and on another page: where the audio, the EPUB reader, or KOReader left off."""
    from lectern.pdftext.positions import pdf_map
    from lectern.progress import get_progress

    m = pdf_map(db, settings.library_dir, book)
    wp = get_progress(db, user_id, book.work_id) if m else None
    if m is None or wp is None:
        return None
    if (
        page_progress
        and page_progress["updated_at"]
        and page_progress["updated_at"] >= iso(wp.updated_at)
    ):
        return None
    page = m.page_of(wp.sentence_n)
    if page is None or (page_progress and page_progress.get("page") == page + 1):
        return None
    return {
        "page": page + 1,
        "sentence_n": wp.sentence_n,
        "source": wp.source.value,
        "device": wp.device or None,
        "updated_at": iso(wp.updated_at),
    }


@router.get("/files/{file_id}/pages/{n}")
def comic_page(
    request: Request, auth: Auth, db: DbDep, settings: SettingsDep, file_id: int, n: int
) -> Response:
    """Page ``n`` (1-based) of a comic, as the image stored in the archive. Reading a comic
    counts as one download per browser per hour, charged on the first page."""
    book, _work, path = _paged_file(db, settings, auth.user, file_id)
    if book.media_type != formats.CBZ:
        raise HTTPException(status.HTTP_409_CONFLICT, "not_a_comic")
    etag = f'"{book.sha256}-{n}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    try:
        comic = ComicCache.get(path, book.sha256)
    except formats.FormatError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code) from None
    if not 1 <= n <= len(comic.pages):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such page")
    # Charge (or refuse) before decompressing, so a capped browser cannot make the server unpack.
    downloads.admit_once(
        db,
        settings,
        auth.user,
        downloads.Device.session(auth.session.id),
        f"file:{book.id}",
        client_ip(request),
        "comic reader",
    )
    try:
        data, media_type = read_page(path, comic.pages[n - 1])
    except formats.FormatError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code) from None
    return Response(
        data,
        media_type=media_type,
        headers={
            "ETag": etag,
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )


class PageProgressIn(BaseModel):
    page: int = Field(ge=1, le=100_000)
    pages: int = Field(ge=1, le=100_000)
    device: str = Field(default="", max_length=128)
    # A page saved while offline, sent later from the device's queue (docs/offline.md).
    offline: OfflineReplay | None = None


@router.put("/files/{file_id}/progress")
def put_page(auth: AuthCsrf, db: DbDep, settings: SettingsDep, file_id: int, body: PageProgressIn):
    """Save the page the web viewer is on, as KOReader would (a page number)."""
    book, _work, _path = _paged_file(db, settings, auth.user, file_id)
    if not book.partial_md5:
        raise HTTPException(status.HTTP_409_CONFLICT, "no_document_id")
    page = min(body.page, body.pages)
    row = db.scalar(
        select(ReadingProgress).where(
            ReadingProgress.user_id == auth.user.id, ReadingProgress.document == book.partial_md5
        )
    )
    if body.offline is not None:
        wins, recorded = body.offline.judge(row.updated_at if row else None)
        if not wins:
            raise conflict(_progress(db, auth.user.id, book), recorded)
    now = int(time.time())
    if row is None:
        row = ReadingProgress(user_id=auth.user.id, document=book.partial_md5, timestamp=now)
        db.add(row)
    row.progress = str(page)
    row.percentage = round(page / body.pages, 4)
    row.device = (body.device or "Web reader")[:128]
    row.device_id = WEB_DEVICE_ID
    row.timestamp = now
    row.sync_key_id = None
    _project_page(db, settings, auth.user.id, book, page, row.device)
    db.commit()
    return _progress(db, auth.user.id, book)


def _project_page(db, settings, user_id: int, book: BookFile, page: int, device: str) -> None:
    """Move the work's canonical position to the first sentence on ``page`` (1-based) of a
    text PDF, unless it is on that page already (reading a page must not rewind the audio to
    the page's first sentence)."""
    from lectern.models import ProgressSource
    from lectern.pdftext.positions import pdf_map
    from lectern.positions import SentencePos
    from lectern.progress import get_progress, set_progress

    m = pdf_map(db, settings.library_dir, book)
    if m is None:
        return
    n = m.sentence_at_page(page - 1)
    if n is None:
        return
    wp = get_progress(db, user_id, book.work_id)
    if wp is not None and m.page_of(wp.sentence_n) == page - 1:
        return
    set_progress(
        db,
        user_id=user_id,
        work_id=book.work_id,
        pos=SentencePos(n, 0),
        source=ProgressSource.WEB,
        raw=f"page:{page}",
        device=device or "Web reader",
    )
