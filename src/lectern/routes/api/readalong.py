"""Read-along and read-aloud API (milestone 4): sentences, progress, spine text, EPUB export.

Shapes follow docs/contracts.md. Every route checks visibility with
:func:`lectern.access.can_see_work` and answers ``404`` for a work the caller may not see.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select

from lectern.access import can_see_work
from lectern.ingest.epub import EpubError, read_spine
from lectern.models import (
    Alignment,
    AlignmentStatus,
    BookFile,
    FileKind,
    ProgressSource,
    Sentence,
    User,
    Work,
    WorkProgress,
    utcnow,
)
from lectern.positions import SentenceIndex, SentencePos, TextPos
from lectern.readaloud.cache import ReadAloudBuilder, Unavailable, gather_inputs
from lectern.readaloud.text import SpineText
from lectern.routes.api.serializers import iso
from lectern.security.deps import Auth, AuthCsrf, DbDep, SettingsDep

router = APIRouter()

MAX_SENTENCES = 2000


def visible_work(db, user: User, work_id: int) -> Work:
    work = db.get(Work, work_id)
    if work is None or not can_see_work(db, user, work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return work


def alignment_json(a: Alignment | None) -> dict | None:
    if a is None:
        return None
    return {"status": a.status.value, "model": a.model, "coverage": a.coverage}


def sentence_json(s: Sentence) -> dict:
    return {
        "n": s.n,
        "spine_index": s.spine_index,
        "char_start": s.char_start,
        "char_end": s.char_end,
        "audio_start_s": s.audio_start_s,
        "audio_end_s": s.audio_end_s,
        "matched": s.matched,
    }


def current_alignment(db, work_id: int) -> Alignment | None:
    return db.scalar(select(Alignment).where(Alignment.work_id == work_id))


def _rows(db, work_id: int) -> list[Sentence]:
    return list(
        db.scalars(select(Sentence).where(Sentence.work_id == work_id).order_by(Sentence.n))
    )


# --- sentences -------------------------------------------------------------------------------


@router.get("/works/{work_id}/sentences")
def sentences(
    auth: Auth,
    db: DbDep,
    work_id: int,
    from_: int | None = Query(default=None, alias="from", ge=0),
    to: int | None = Query(default=None, ge=0),
    t: float | None = Query(default=None, ge=0),
    around: int = Query(default=10, ge=0, le=50),
    spine: int | None = Query(default=None, ge=0),
    char: int | None = Query(default=None, ge=0),
) -> dict:
    visible_work(db, auth.user, work_id)
    modes = [from_ is not None, t is not None, spine is not None]
    if sum(modes) != 1 or (spine is not None) != (char is not None):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "use exactly one of from, t, or spine+char"
        )
    count = db.scalar(select(func.count()).where(Sentence.work_id == work_id)) or 0
    base = select(Sentence).where(Sentence.work_id == work_id).order_by(Sentence.n)
    if from_ is not None:
        end = from_ + 500 if to is None else to
        if end < from_ or end - from_ > MAX_SENTENCES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"at most {MAX_SENTENCES} per call")
        rows = db.scalars(base.where(Sentence.n >= from_, Sentence.n < end)).all()
    else:
        index = SentenceIndex(_rows(db, work_id))
        pos = index.at_time(t) if t is not None else index.at_text(TextPos(spine, char))  # type: ignore[arg-type]
        if pos is None:
            rows = []
        elif t is not None:
            rows = db.scalars(
                base.where(Sentence.n >= pos.n - around, Sentence.n <= pos.n + around)
            ).all()
        else:
            rows = db.scalars(base.where(Sentence.n >= pos.n, Sentence.n <= pos.n + 100)).all()
    return {
        "work_id": work_id,
        "count": count,
        "alignment": alignment_json(current_alignment(db, work_id)),
        "sentences": [sentence_json(s) for s in rows],
    }


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


# --- progress --------------------------------------------------------------------------------


def _progress_json(work_id: int, row: WorkProgress | None, index: SentenceIndex) -> dict:
    out: dict = {
        "work_id": work_id,
        "position": None,
        "source": None,
        "raw": None,
        "device": None,
        "received_at": None,
        "text": None,
        "audio_s": None,
        "percent": None,
    }
    if row is None:
        return out
    n = min(row.sentence_n, len(index) - 1) if len(index) else None
    pos = SentencePos(n, row.sentence_offset) if n is not None else None
    text = index.to_text(pos) if pos is not None else None
    out.update(
        position={"sentence_n": row.sentence_n, "offset": row.sentence_offset},
        source=row.source.value,
        raw=row.raw,
        device=row.device,
        received_at=iso(row.received_at),
        text={"spine_index": text.spine_index, "char": text.char} if text else None,
        audio_s=index.to_time(pos) if pos is not None else None,
        percent=(row.sentence_n / len(index)) if len(index) else None,
    )
    return out


@router.get("/works/{work_id}/progress")
def get_progress(auth: Auth, db: DbDep, work_id: int) -> dict:
    visible_work(db, auth.user, work_id)
    row = db.scalar(
        select(WorkProgress).where(
            WorkProgress.user_id == auth.user.id, WorkProgress.work_id == work_id
        )
    )
    return _progress_json(work_id, row, SentenceIndex(_rows(db, work_id)))


class ProgressIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["web", "audio"]
    device: str = Field(default="", max_length=128)
    sentence_n: int | None = Field(default=None, ge=0)
    offset: int | None = Field(default=None, ge=0)
    audio_s: float | None = Field(default=None, ge=0)
    spine_index: int | None = Field(default=None, ge=0)
    char: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _one_form(self) -> ProgressIn:
        forms = [
            self.sentence_n is not None,
            self.audio_s is not None,
            self.spine_index is not None or self.char is not None,
        ]
        if sum(forms) != 1:
            raise ValueError("exactly one position form")
        if self.offset is not None and self.sentence_n is None:
            raise ValueError("offset goes with sentence_n")
        if (self.spine_index is None) != (self.char is None):
            raise ValueError("spine_index and char go together")
        return self


@router.put("/works/{work_id}/progress")
def put_progress(auth: AuthCsrf, db: DbDep, work_id: int, body: ProgressIn) -> dict:
    """Last write wins; ``received_at`` is the server clock (docs/contracts.md)."""
    visible_work(db, auth.user, work_id)
    index = SentenceIndex(_rows(db, work_id))
    if not len(index):
        raise HTTPException(status.HTTP_409_CONFLICT, "no_sentences")
    if body.sentence_n is not None:
        if body.sentence_n >= len(index):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "sentence_n out of range")
        s = index.sentence(body.sentence_n)
        pos = SentencePos(body.sentence_n, min(body.offset or 0, s.char_end - s.char_start))
        raw = f"{pos.n}:{pos.offset}"
    elif body.audio_s is not None:
        found = index.at_time(body.audio_s)
        if found is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "no_alignment")
        pos, raw = found, repr(round(body.audio_s, 3))
    else:
        pos = index.at_text(TextPos(body.spine_index, body.char))  # type: ignore[arg-type]
        raw = f"{body.spine_index}:{body.char}"
    row = db.scalar(
        select(WorkProgress).where(
            WorkProgress.user_id == auth.user.id, WorkProgress.work_id == work_id
        )
    )
    if row is None:
        row = WorkProgress(user_id=auth.user.id, work_id=work_id)
        db.add(row)
    row.sentence_n, row.sentence_offset = pos.n, pos.offset
    row.source = ProgressSource(body.source)
    row.raw = raw
    row.device = body.device or "Web"
    row.received_at = utcnow()
    db.commit()
    return _progress_json(work_id, row, index)


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
