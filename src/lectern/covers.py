"""Cover sources taken from the work's own files: EPUB images, PDF pages, comic pages.

Everything here is untrusted input and is handled that way:

- the client names a file by id and an image or page by number, never by path; the file must
  belong to the work in the URL, and its resolved path must stay inside the library;
- EPUB images come from the OPF manifest listing (:func:`lectern.ingest.epub.list_images`),
  never SVG, at most ``MAX_COVER_BYTES`` (10 MiB) each, so that is all that is buffered; PDF
  pages are rendered by the sandboxed child under ``PARSER_SLOTS`` and read at most
  ``COVER_MAX_BYTES + 1``; a comic page is read by the comic reader, which buffers up to
  ``MAX_ENTRY_BYTES`` (64 MiB) of one entry before ``stage_cover`` refuses it as 413 (the
  10 MiB limit applies to what is kept, not to what the reader buffers);
- every image byte, whatever its source, is decoded by ``save_cover_jpeg`` with the allowlisted
  ``EMBEDDED_FORMATS`` before it is kept (staged as JPEG), and a crop is applied to that JPEG
  and re-validated the same way;
- staged covers are bound to the user by their token (``editing.staged_cover_path``).

HTTP-free: failures are :class:`lectern.editing.EditError`; ``routes.api.covers`` translates.
"""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from lectern import editing
from lectern.config import Settings
from lectern.editing import EditError
from lectern.ingest import formats
from lectern.ingest import pdf as pdf_ingest
from lectern.ingest.comic import ComicCache, read_page
from lectern.ingest.cover import (
    COVER_MAX_BYTES,
    EMBEDDED_FORMATS,
    CoverRejected,
    CropRejected,
    crop_jpeg,
    thumbnail_jpeg,
)
from lectern.models import BookFile, Work

THUMB_SIDE = 360


class Crop(BaseModel):
    """A normalised rectangle (fractions of the image). Range is checked by ``crop_jpeg``, so
    every bad rectangle is the same ``422 bad_crop``."""

    model_config = ConfigDict(extra="forbid")

    x: float
    y: float
    w: float
    h: float


class StageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["staged", "epub_image", "page"]
    token: str | None = Field(default=None, max_length=64)
    file_id: int | None = Field(default=None, ge=1, le=2**62)
    index: int | None = Field(default=None, ge=-(2**62), le=2**62)
    page: int | None = Field(default=None, ge=-(2**62), le=2**62)
    crop: Crop | None = None

    @model_validator(mode="after")
    def _fields_match_source(self):
        needed = {
            "staged": {"token"},
            "epub_image": {"file_id", "index"},
            "page": {"file_id", "page"},
        }[self.source]
        given = {k for k in ("token", "file_id", "index", "page") if getattr(self, k) is not None}
        if given != needed:
            raise ValueError(f"source {self.source!r} takes exactly {sorted(needed)}")
        return self


def source_path(db: Session, settings: Settings, work: Work, file_id: int, kinds) -> tuple:
    """The file ``file_id`` of ``work`` and its path. 404 when the file is another work's (or
    absent, or outside the library); 422 ``wrong_format`` when its type is not in ``kinds``."""
    book = db.get(BookFile, file_id)
    if book is None or book.work_id != work.id:
        raise EditError(404, "file_not_found")
    if book.media_type not in kinds:
        raise EditError(422, "wrong_format")
    root = settings.library_dir.resolve()
    try:
        path = (root / work.dir_path / book.rel_path).resolve()
        ok = root in path.parents and path.is_file()
    except OSError:
        ok = False
    if not ok:
        raise EditError(404, "file_not_found")
    return book, path


def epub_image(db: Session, settings: Settings, work: Work, file_id: int, index: int) -> bytes:
    from lectern.ingest import epub

    _book, path = source_path(db, settings, work, file_id, {formats.EPUB})
    try:
        data = epub.read_image(path, index)
    except (epub.EpubError, OSError, zipfile.BadZipFile):
        data = None
    if data is None:
        raise EditError(404, "image_not_found")
    return data


def _pdf_page(settings: Settings, path: Path, page: int) -> bytes:
    from lectern.pdf2epub import probe

    base = settings.data_dir / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="cover-page-", dir=base))
    try:
        try:  # the sandboxed child, inside PARSER_SLOTS
            done = probe.render(path, [[page - 1, 0, 0, 0, 0, "page.jpg", "photo"]], tmp)
        except formats.ProbeBusy:
            raise EditError(503, "pdf_busy") from None
        except formats.FormatError as exc:
            raise EditError(422, exc.code) from None
        out = tmp / "page.jpg"
        if "page.jpg" not in done or not out.is_file() or out.is_symlink():
            raise EditError(422, "bad_page")
        with out.open("rb") as fh:
            return fh.read(COVER_MAX_BYTES + 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def page_image(db: Session, settings: Settings, work: Work, file_id: int, page: int) -> bytes:
    """Page ``page`` (1-based) of a PDF or comic of ``work`` as image bytes."""
    book, path = source_path(db, settings, work, file_id, formats.PAGED)
    if book.media_type == formats.CBZ:
        try:
            comic = ComicCache.get(path, book.sha256)
            if not 1 <= page <= len(comic.pages):
                raise EditError(422, "bad_page")
            return read_page(path, comic.pages[page - 1])[0]
        except formats.FormatError as exc:
            raise EditError(422, exc.code) from None
        except OSError:
            raise EditError(404, "file_not_found") from None
    # The recorded count first, before any child; a PDF not counted yet is bounded by the
    # parser's page limit and the child skips a page it does not have.
    limit = book.pages if book.pages is not None else pdf_ingest.MAX_PAGES
    if not 1 <= page <= limit:
        raise EditError(422, "bad_page")
    return _pdf_page(settings, path, page)


def _size(path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path, formats=["JPEG"]) as img:
        return img.size


def stage_source(
    db: Session, settings: Settings, user_id: int, work: Work, req: StageRequest
) -> dict:
    """Stage the requested source (optionally cropped) as a new token of ``user_id``."""
    origin: dict | None = None
    scratch: str | None = None  # an intermediate token to drop once the final one exists
    if req.source == "staged":
        base = editing.staged_cover_path(settings, user_id, req.token or "")  # 404 unless theirs
        origin = editing.staged_origin(settings, user_id, req.token or "", work.id)
    else:
        if req.source == "epub_image":
            data = epub_image(db, settings, work, req.file_id, req.index)
            origin = {"source": "image", "file_id": req.file_id, "index": req.index}
        else:
            data = page_image(db, settings, work, req.file_id, req.page)
            origin = {"source": "page", "file_id": req.file_id, "page": req.page}
        origin["work_id"] = work.id
        token = editing.stage_cover(
            settings, user_id, data, formats=EMBEDDED_FORMATS, origin=origin
        )
        if req.crop is None:
            return _result(settings, user_id, token)
        scratch, base = token, editing.staged_cover_path(settings, user_id, token)
    if origin is not None:
        origin["work_id"] = work.id
    try:
        try:
            if req.crop is not None:
                c = req.crop
                data = crop_jpeg(base, c.x, c.y, c.w, c.h)
            else:
                data = base.read_bytes()
        except CropRejected:
            raise EditError(422, "bad_crop") from None
        except OSError:
            raise EditError(404, "cover_not_found") from None
        token = editing.stage_cover(
            settings, user_id, data, formats=EMBEDDED_FORMATS, origin=origin
        )
    finally:
        if scratch is not None:
            base.unlink(missing_ok=True)
            base.with_suffix(".json").unlink(missing_ok=True)
    return _result(settings, user_id, token)


def _result(settings: Settings, user_id: int, token: str) -> dict:
    path = editing.staged_cover_path(settings, user_id, token)
    width, height = _size(path)
    return {
        "token": token,
        "preview_url": f"/api/staged-covers/{token}.jpg",
        "width": width,
        "height": height,
    }


def thumbnail(db: Session, settings: Settings, work: Work, file_id: int, index: int) -> bytes:
    """A small JPEG of EPUB image ``index``, decoded reduced and a few at a time
    (:func:`lectern.ingest.cover.thumbnail_jpeg`), never at full size."""
    data = epub_image(db, settings, work, file_id, index)
    try:
        return thumbnail_jpeg(data, THUMB_SIDE, formats=EMBEDDED_FORMATS)
    except CoverRejected as exc:
        raise EditError(413 if exc.code == "cover_too_large" else 422, exc.code) from None
