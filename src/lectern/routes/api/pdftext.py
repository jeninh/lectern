"""PDF text and read-along on PDF pages (docs/pdf-read-along.md).

- ``GET /api/files/{id}/text``: what Lectern read from a PDF's text layer (text PDF, scanned
  and needing OCR, still reading, or failed) and whether read-along works on its pages.
- ``GET /api/files/{id}/sentences``: the PDF's sentences as highlight boxes on its pages, each
  with the work sentence it is (``work_n``), so the viewer can follow the work's audio timeline
  (``GET /api/works/{id}/sentences``) on the pages. No text is sent: only page numbers and
  rectangles, so this is not a download.
- ``POST /api/files/{id}/text/retry``: read a PDF whose reading failed again (owners, members).

Every route answers ``404`` for a file of a work the caller may not see.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from lectern.access import can_see_work
from lectern.align.tracks import work_tracks
from lectern.ingest import formats
from lectern.models import Alignment, AlignmentStatus, BookFile, PdfSentence, Role, Work
from lectern.pdftext.positions import pdf_map
from lectern.pdftext.store import (
    FAILED,
    PENDING,
    pdf_text_of,
    status_json,
    work_text,
)
from lectern.security.deps import Auth, AuthContext, DbDep, SettingsDep, require_role

router = APIRouter()
Editor = Annotated[AuthContext, Depends(require_role(Role.OWNER, Role.MEMBER))]
PAGE_MAX = 2000


def _pdf(db, user, file_id: int) -> tuple[BookFile, Work]:
    book = db.get(BookFile, file_id)
    if book is None or not can_see_work(db, user, book.work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if book.media_type != formats.PDF:
        raise HTTPException(status.HTTP_409_CONFLICT, "not_pdf")
    work = db.get(Work, book.work_id)
    if work is None or work.trashed_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return book, work


def text_state(db, settings, book: BookFile, work: Work) -> dict:
    """The PDF's text status plus how read-along works on it (also part of the page info)."""
    out = {"file_id": book.id, "work_id": work.id, **status_json(pdf_text_of(db, book.id), book)}
    found = work_text(db, settings.library_dir, work)
    m = pdf_map(db, settings.library_dir, book) if out["status"] == "ready" else None
    alignment = db.scalar(select(Alignment).where(Alignment.work_id == work.id))
    aligned = alignment is not None and alignment.status == AlignmentStatus.SUCCEEDED
    out.update(
        text_source=found[0].kind if found else None,
        # this PDF is the work's text, or is shown in step with the work's EPUB
        map=m.mode if m else None,
        readalong=bool(m and aligned and work.readalong_enabled and work_tracks(db, work.id)),
    )
    return out


@router.get("/files/{file_id}/text")
def pdf_text(auth: Auth, db: DbDep, settings: SettingsDep, file_id: int) -> dict:
    book, work = _pdf(db, auth.user, file_id)
    return text_state(db, settings, book, work)


@router.get("/files/{file_id}/sentences")
def pdf_sentences(
    auth: Auth,
    db: DbDep,
    settings: SettingsDep,
    file_id: int,
    from_: Annotated[int, Query(alias="from", ge=0)] = 0,
    to: Annotated[int | None, Query(ge=0)] = None,
) -> dict:
    """``{"count", "map", "pages", "sentences": [{"n", "work_n", "page", "boxes"}]}``;
    ``boxes`` are ``[page, x0, y0, x1, y1]`` in PDF user space (0-based pages), ``work_n``
    the work sentence (``null`` when the EPUB has no such text). ``pages`` are the page sizes
    the boxes refer to. ``409 no_map`` when the PDF's text is not ready or not current."""
    book, _work = _pdf(db, auth.user, file_id)
    m = pdf_map(db, settings.library_dir, book)
    if m is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "no_map")
    end = min(m.row.sentence_count, (to if to is not None else from_ + PAGE_MAX))
    end = min(end, from_ + PAGE_MAX)
    rows = db.execute(
        select(PdfSentence.n, PdfSentence.page_start, PdfSentence.boxes)
        .where(PdfSentence.file_id == book.id, PdfSentence.n >= from_, PdfSentence.n < end)
        .order_by(PdfSentence.n)
    ).all()
    return {
        "file_id": book.id,
        "count": m.row.sentence_count,
        "map": m.mode,
        "sentences": [
            {"n": n, "work_n": m.work_n[n], "page": page, "boxes": boxes} for n, page, boxes in rows
        ],
    }


@router.post("/files/{file_id}/text/retry")
def retry_pdf_text(auth: Editor, db: DbDep, settings: SettingsDep, file_id: int) -> dict:
    """Read a PDF again after a failure (the next library sweep, within a minute)."""
    book, work = _pdf(db, auth.user, file_id)
    row = pdf_text_of(db, book.id)
    if row is None or row.status != FAILED:
        raise HTTPException(status.HTTP_409_CONFLICT, "nothing_to_retry")
    row.status, row.attempts, row.error = PENDING, 0, None
    db.commit()
    return text_state(db, settings, book, work)
