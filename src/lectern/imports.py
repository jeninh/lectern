"""Review before filing: uploads become *imports* that a person accepts, edits, or redirects.

Lifecycle (``imports.status``)::

    uploading --complete--> ready --file--> filed
        |                     |
        +------discard--------+--> discarded        (every file a duplicate: failed)

1. ``create`` an import (idempotent per user and ``Idempotency-Key``).
2. Upload each file with the resumable upload API, passing ``import_id``. Finalize verifies
   the checksum and the format, then :func:`stage_upload` moves the file into
   ``<library>/_staging/<import>/``, reads its metadata, and flags duplicates of files already
   in the library (or in the trash).
3. ``complete``: detect title, authors, series, language, ISBN, narrators, and cover across
   the files, and propose a work to join (with a confidence) or a new one. With the user's
   ``auto_file_imports`` preference on, a *certain* join is filed straight away.
4. ``file``: with the reviewed metadata and target, move the files into the work's folder
   (one rename on the library's filesystem), register them, write ``book.json``, rebuild
   sentences, and let alignment notice. Repeating it answers with the same result.

Only the import's uploader can see or change it. Owners and members only (readers never
reach these functions; the routes check the role). All functions leave the commit to the
caller; ``file`` expects :data:`lectern.fsops.LIBRARY_LOCK` held.
"""

from __future__ import annotations

import logging
import re
import shutil
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from rapidfuzz import fuzz
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lectern.config import Settings
from lectern.covergen import cover_fields, tint_of
from lectern.fsops import STAGING_DIR, checked, move_file, unique_name
from lectern.ingest import formats
from lectern.ingest.audio import AudioError, _natural, _save_cover, probe
from lectern.ingest.cover import dominant_tint
from lectern.ingest.epub import extract_cover
from lectern.ingest.hashing import partial_md5, sha256_file
from lectern.ingest.library import (
    book_file_name,
    new_work,
    safe_component,
    save_first_page_cover,
)
from lectern.ingest.matching import clean_title, rank_candidates, sort_title
from lectern.models import (
    AudioTrack,
    BookFile,
    FileKind,
    Import,
    ImportFile,
    ImportStatus,
    TrashedFile,
    UploadSession,
    UploadStatus,
    User,
    Work,
    utcnow,
)
from lectern.uploads import media_suffix
from lectern.workmeta import (
    BOOK_JSON,
    Metadata,
    apply_metadata,
    strip_controls,
    write_book_json,
)

log = logging.getLogger("lectern.imports")

IMPORT_TTL = timedelta(days=7)
FINISHED_RETENTION = timedelta(days=30)
MAX_FILES = 1000
MAX_OPEN_IMPORTS = 200
HIGH, MEDIUM, LOW = "high", "medium", "low"
MEDIUM_SCORE = 85
OPEN = (ImportStatus.UPLOADING, ImportStatus.READY)


class ImportError_(Exception):  # noqa: N801 - "ImportError" is a builtin
    def __init__(self, http_status: int, code: str, **extra) -> None:
        super().__init__(code)
        self.status, self.code, self.extra = http_status, code, extra


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["new", "join"]
    work_id: int | None = None


class DraftPatch(BaseModel):
    """Edits to an import under review. ``version`` is the import version seen."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    metadata: Metadata | None = None
    target: Target | None = None
    use_cover: bool | None = None


class Decision(BaseModel):
    """What ``file`` does. Omitted fields keep the draft's values (which default to what was
    detected and proposed). ``audio="append"`` adds tracks to a work that already has an
    audiobook; without it that is refused, so two audiobooks never mix by accident."""

    model_config = ConfigDict(extra="forbid")

    version: int | None = Field(default=None, ge=1)
    metadata: Metadata | None = None
    target: Target | None = None
    use_cover: bool | None = None
    audio: Literal["refuse", "append"] = "refuse"


def staging_dir(settings: Settings, imp: Import) -> Path:
    return settings.library_dir / STAGING_DIR / imp.public_id


def get_owned(db: Session, user: User, public_id: str) -> Import:
    imp = db.scalar(select(Import).where(Import.public_id == public_id))
    if imp is None or imp.user_id != user.id:
        raise ImportError_(404, "not_found")
    return imp


def create(db: Session, user: User, label: str, idempotency_key: str | None) -> tuple[Import, bool]:
    from lectern.uploads import new_public_id

    if idempotency_key:
        prior = db.scalar(
            select(Import).where(
                Import.user_id == user.id,
                Import.idempotency_key == idempotency_key,
                Import.created_at >= utcnow() - timedelta(hours=24),
            )
        )
        if prior is not None:
            return prior, False
    open_count = db.scalar(
        select(func.count())
        .select_from(Import)
        .where(Import.user_id == user.id, Import.status.in_(OPEN))
    )
    if (open_count or 0) >= MAX_OPEN_IMPORTS:
        raise ImportError_(429, "too_many_imports")
    imp = Import(
        public_id=new_public_id(),
        user_id=user.id,
        label=label.strip()[:256],
        status=ImportStatus.UPLOADING,
        idempotency_key=idempotency_key,
        expires_at=utcnow() + IMPORT_TTL,
    )
    db.add(imp)
    db.flush()
    return imp, True


def _touch(imp: Import) -> None:
    imp.version += 1
    imp.expires_at = utcnow() + IMPORT_TTL


# --- staging ---------------------------------------------------------------------------------


def _probe_book(path: Path, media_type: str) -> tuple[dict, bytes | None]:
    """EPUB, PDF, or CBZ: the metadata to detect from, and a first-page cover for formats
    without an embedded one."""
    probed = formats.probe(path, media_type)
    meta = probed.meta
    return {
        "format": formats.label_of(media_type),
        "pages": probed.pages,
        "title": meta.title if meta.title != path.stem else None,
        "authors": meta.authors,
        "language": meta.language,
        "isbn": meta.isbn,
        "description": meta.description,
        "publisher": meta.publisher,
        "published": meta.published,
        "series": meta.series,
        "series_index": meta.series_index,
        "narrators": meta.narrators,
        "tags": meta.tags,
        "cover_href": meta.cover_href,
    }, probed.cover


def _probe_audio(path: Path, ffprobe: str) -> tuple[dict, bytes | None]:
    info = probe(path, ffprobe)
    return (
        {
            "title": info.title,
            "album": info.album,
            "artist": info.artist,
            "album_artist": info.album_artist,
            "isbn": info.isbn,
            "track_no": info.track_no,
            "disc_no": info.disc_no,
            "year": info.year,
            "duration_s": info.duration_s,
            "codec": info.codec,
            "media_type": info.media_type,
            "chapters": info.chapters,
            "narrator": info.narrator,
            "composer": info.composer,
            "series": info.series,
            "series_index": info.series_index,
            "language": info.language,
            "description": info.description,
            "has_cover": info.cover is not None,
        },
        info.cover,
    )


def _duplicate_of(db: Session, sha256: str) -> int | None:
    """The work already holding this exact file: as a format, a track, or a removed format
    still in the trash (filing it anew would make that removal impossible to undo)."""
    hit = db.scalar(select(BookFile.work_id).where(BookFile.sha256 == sha256))
    if hit is None:
        hit = db.scalar(select(AudioTrack.work_id).where(AudioTrack.sha256 == sha256).limit(1))
    if hit is None:
        hit = db.scalar(
            select(TrashedFile.work_id)
            .where(
                TrashedFile.sha256 == sha256,
                TrashedFile.restored_at.is_(None),
                TrashedFile.work_id.is_not(None),
            )
            .limit(1)
        )
    return hit


def stage_upload(
    db: Session, settings: Settings, imp: Import, upload: UploadSession, part: Path
) -> ImportFile:
    """Move a verified upload into the import's staging folder and read it. A file whose
    checksum is already in this import is the same file again: the existing entry answers."""
    if imp.status not in OPEN:
        raise ImportError_(409, "import_closed", status=imp.status.value)
    existing = db.scalar(
        select(ImportFile).where(ImportFile.import_id == imp.id, ImportFile.sha256 == upload.sha256)
    )
    if existing is not None:
        part.unlink(missing_ok=True)
        return existing
    if len(imp.files) >= MAX_FILES:
        raise ImportError_(413, "too_many_files")
    book = formats.is_book(upload.media_type)
    kind = FileKind.EBOOK if book else FileKind.AUDIO
    folder = staging_dir(settings, imp)
    folder.mkdir(parents=True, exist_ok=True)
    stem = safe_component(Path(upload.filename).stem, "file")[:80]
    suffix = media_suffix(upload.filename, upload.media_type)
    rel = unique_name(folder, f"{len(imp.files) + 1:04d} {stem}{suffix}")
    dest = checked(folder, rel)
    move_file(part, dest)
    media_type, size, sha256 = upload.media_type, upload.size, upload.sha256
    cover = None
    try:
        if formats.is_kindle(media_type):
            # Converted here and staged as the EPUB; the original is not kept (docs/kindle.md).
            source_label = formats.label_of(media_type)
            epub_rel = unique_name(folder, f"{Path(rel).stem}.epub")
            epub = checked(folder, epub_rel)
            formats.to_epub(dest, media_type, epub)
            dest.unlink(missing_ok=True)
            rel, dest, media_type = epub_rel, epub, formats.EPUB
            size, sha256 = dest.stat().st_size, sha256_file(dest)
            existing = db.scalar(
                select(ImportFile).where(
                    ImportFile.import_id == imp.id, ImportFile.sha256 == sha256
                )
            )
            if existing is not None:  # the same book again (conversion is deterministic)
                dest.unlink(missing_ok=True)
                return existing
        else:
            source_label = None
        if book:
            meta, cover = _probe_book(dest, media_type)
            if source_label:
                meta["converted_from"] = source_label
            pmd5 = partial_md5(dest)  # KOReader's document id, for progress sync
        else:
            meta, cover = _probe_audio(dest, settings.ffprobe_path)
            pmd5 = None
    except formats.ProbeBusy as exc:
        # Not the file's fault: put the bytes back so the same upload can be finalized again.
        move_file(dest, part)
        raise ImportError_(503, exc.code) from exc
    except formats.FormatError as exc:
        dest.unlink(missing_ok=True)
        raise ImportError_(422, exc.code) from exc
    except (AudioError, OSError) as exc:
        dest.unlink(missing_ok=True)
        code = formats.wrong_format_code(media_type) if book else "not_audio"
        raise ImportError_(422, code) from exc
    dup = _duplicate_of(db, sha256)
    row = ImportFile(
        import_id=imp.id,
        kind=kind,
        filename=upload.filename[:600],
        rel_path=rel,
        media_type=media_type,
        size=size,
        sha256=sha256,
        partial_md5=pmd5,
        status="duplicate" if dup is not None else "ok",
        duplicate_work_id=dup,
        probe=meta,
    )
    db.add(row)
    _stage_cover(folder, dest, kind, meta, cover)
    _touch(imp)
    db.flush()
    db.refresh(imp)
    return row


def _stage_cover(folder: Path, path: Path, kind: FileKind, meta: dict, cover: bytes | None) -> None:
    """The first EPUB's cover wins; then the first page of a PDF or comic; then audio art."""
    target = folder / "cover.jpg"
    marker = folder / ".cover-from-epub"
    page_marker = folder / ".cover-from-page"
    if kind == FileKind.EBOOK and meta.get("cover_href") and not marker.exists():
        try:
            if extract_cover(path, meta["cover_href"], target):
                marker.touch()
        except (OSError, KeyError):
            pass
    elif kind == FileKind.EBOOK and cover and not marker.exists() and not page_marker.exists():
        if save_first_page_cover(cover, target):
            page_marker.touch()
    elif kind == FileKind.AUDIO and cover and not target.exists():
        _save_cover(cover, target)


# --- detection and proposal -----------------------------------------------------------------


def _common(values) -> str | None:
    vals = [v.strip() for v in values if v and v.strip()]
    return Counter(vals).most_common(1)[0][0] if vals else None


def detect(imp: Import) -> dict:
    """One metadata proposal from all fileable files: an EPUB's OPF wins over audio tags."""
    ok = [f for f in imp.files if f.status == "ok"] or list(imp.files)
    # An EPUB's OPF is the richest source, then a PDF's Info/XMP or a comic's ComicInfo.
    epubs = sorted(
        (f.probe or {} for f in ok if f.kind == FileKind.EBOOK),
        key=lambda p: p.get("format", "EPUB") != "EPUB",
    )
    audio = [f.probe or {} for f in ok if f.kind == FileKind.AUDIO]
    e = epubs[0] if epubs else {}
    album = _common(clean_title(a.get("album") or "") for a in audio)
    single_title = clean_title(audio[0].get("title") or "") if len(audio) == 1 else None
    fallback = clean_title(Path(imp.label).stem) if imp.label else None
    title = e.get("title") or album or single_title or fallback or "Untitled"
    authors = list(e.get("authors") or [])
    if not authors:
        a = _common((x.get("album_artist") or x.get("artist")) for x in audio)
        authors = [a] if a else []
    narrators = list(e.get("narrators") or [])
    for x in audio:
        n = x.get("narrator") or (
            x.get("composer") if x.get("composer") and x.get("composer") not in authors else None
        )
        if n:
            for part in re.split(r"\s*(?:,|;| & | and )\s*", n):
                if part and part not in narrators and part not in authors:
                    narrators.append(part[:128])
    series = e.get("series") or _common(x.get("series") for x in audio)
    index = e.get("series_index")
    if index is None:
        index = next((x["series_index"] for x in audio if x.get("series_index") is not None), None)
    year = _common(x.get("year") for x in audio)
    m = re.match(r"\d{4}(-\d{2}(-\d{2})?)?", e.get("published") or year or "")
    published = m.group(0) if m else None
    raw = {
        "title": title[:512],
        "sort_title": None,
        "authors": [{"name": a[:256]} for a in authors[:20]],
        "series": series,
        "series_index": index if series else None,
        "description": e.get("description") or _common(x.get("description") for x in audio),
        "language": e.get("language") or _common(x.get("language") for x in audio),
        "isbn": e.get("isbn") or next((x["isbn"] for x in audio if x.get("isbn")), None),
        "publisher": e.get("publisher"),
        "published": published,
        "tags": list(e.get("tags") or [])[:50],
        "narrators": narrators[:20],
    }
    return _valid_metadata(raw)


def _valid_metadata(raw: dict) -> dict:
    """Drop detected values the schema would refuse (a malformed language tag, a broken
    ISBN) instead of refusing the whole upload."""
    from pydantic import ValidationError

    data = strip_controls(dict(raw))  # read from files: drop what XML cannot carry
    for _ in range(len(data) + 1):
        try:
            return Metadata.model_validate(data).model_dump()
        except ValidationError as exc:
            bad = {str(err["loc"][0]) for err in exc.errors() if err.get("loc")}
            if not bad or "title" in bad:
                data["title"] = (data.get("title") or "Untitled")[:512] or "Untitled"
            for k in bad - {"title"}:
                data[k] = [] if k in ("authors", "tags", "narrators") else None
    return Metadata.model_validate({"title": "Untitled"}).model_dump()


def _work_summary(w: Work, db: Session) -> dict:
    has_audio = (
        db.scalar(select(AudioTrack.id).where(AudioTrack.work_id == w.id).limit(1)) is not None
    )
    return {
        "id": w.id,
        "title": w.title,
        "author": w.author,
        "series": w.series,
        "series_index": w.series_index,
        "isbn": w.isbn,
        "cover_url": cover_fields(w)["cover_url"],
        "cover_tint": tint_of(w),
        "has_ebook": any(f.kind == FileKind.EBOOK for f in w.files),
        "formats": sorted({formats.label_of(f.media_type) for f in w.files}),
        "has_audio": has_audio,
    }


def propose(db: Session, imp: Import) -> dict:
    meta = imp.metadata_ or imp.detected or {"title": imp.label or "Untitled", "authors": []}
    has_audio = any(f.kind == FileKind.AUDIO and f.status == "ok" for f in imp.files)
    names = [a["name"] for a in meta.get("authors") or []]
    ranked = rank_candidates(db, meta["title"], names, meta.get("isbn"), prefer_audio=has_audio)
    cands = [
        {
            "work": _work_summary(r.work, db),
            "score": r.score,
            "certain": r.certain,
            "reason": r.reason,
        }
        for r in ranked
        if r.work is not None
    ]
    best = ranked[0] if ranked else None
    if best is not None and best.certain:
        action, conf, work_id = "join", HIGH, best.work.id
        reason = f"Same book as one you have ({best.reason})"
    elif best is not None and best.score >= MEDIUM_SCORE:
        action, conf, work_id = "join", MEDIUM, best.work.id
        reason = f"Probably a book you have ({best.reason})"
    elif best is not None:
        action, conf, work_id = "new", LOW, None
        reason = f"Possibly “{best.work.title}” ({best.reason}); filed as a new book unless you choose it"
    else:
        action, conf, work_id = "new", HIGH, None
        reason = "No similar book in your library"
    return {
        "action": action,
        "work_id": work_id,
        "confidence": conf,
        "score": round(best.score, 1) if best else None,
        "reason": reason,
        "candidates": cands,
        "siblings": _siblings(db, imp, meta["title"]),
    }


def _siblings(db: Session, imp: Import, title: str) -> list[dict]:
    """Other open imports of the same person that look like the same book (an EPUB and its
    audiobook dropped together arrive as two imports): offered as "combine"."""
    key = sort_title(clean_title(title))
    out = []
    for other in db.scalars(
        select(Import).where(
            Import.user_id == imp.user_id, Import.status.in_(OPEN), Import.id != imp.id
        )
    ):
        m = other.metadata_ or other.detected or {}
        if key and fuzz.token_sort_ratio(key, sort_title(clean_title(m.get("title") or ""))) >= 90:
            out.append({"id": other.public_id, "label": other.label, "title": m.get("title")})
    return out[:5]


def analyze(db: Session, imp: Import, *, keep_edits: bool = True) -> None:
    detected = detect(imp)
    edited = keep_edits and imp.metadata_ is not None and imp.metadata_ != imp.detected
    imp.detected = detected
    if not edited:
        imp.metadata_ = detected
    imp.proposal = propose(db, imp)
    db.flush()


def complete(db: Session, settings: Settings, user: User, imp: Import) -> Import:
    """All files are in: analyse and wait for review (or file a certain match right away)."""
    if imp.status not in OPEN:
        return imp
    active = db.scalar(
        select(func.count())
        .select_from(UploadSession)
        .where(UploadSession.import_id == imp.id, UploadSession.status == UploadStatus.ACTIVE)
    )
    if active:
        raise ImportError_(409, "uploads_in_progress", active=active)
    if not imp.files:
        raise ImportError_(409, "no_files")
    analyze(db, imp)
    if not any(f.status == "ok" for f in imp.files):
        imp.status, imp.error = ImportStatus.FAILED, "all_duplicates"
    else:
        imp.status = ImportStatus.READY
    _touch(imp)
    db.flush()
    return imp


def auto_file_eligible(db: Session, user: User, imp: Import) -> bool:
    p = imp.proposal or {}
    if not user.auto_file_imports or imp.status != ImportStatus.READY or imp.target is not None:
        return False
    if p.get("action") != "join" or p.get("confidence") != HIGH or not p.get("work_id"):
        return False
    target = db.get(Work, p["work_id"])
    has_audio = any(f.kind == FileKind.AUDIO and f.status == "ok" for f in imp.files)
    if target is None or target.trashed_at is not None:
        return False
    return not (has_audio and _work_has_audio(db, target.id))


def _work_has_audio(db: Session, work_id: int) -> bool:
    return (
        db.scalar(select(AudioTrack.id).where(AudioTrack.work_id == work_id).limit(1)) is not None
    )


def update_draft(db: Session, imp: Import, patch: DraftPatch) -> Import:
    if imp.status not in OPEN:
        raise ImportError_(409, "import_closed", status=imp.status.value)
    if patch.version != imp.version:
        raise ImportError_(409, "version_conflict")
    sent = patch.model_fields_set
    if patch.metadata is not None:
        imp.metadata_ = patch.metadata.model_dump()
    if "target" in sent:
        imp.target = patch.target.model_dump() if patch.target else None
    if "use_cover" in sent:
        imp.use_cover = patch.use_cover
    imp.proposal = propose(db, imp)
    _touch(imp)
    db.flush()
    return imp


# --- filing ----------------------------------------------------------------------------------------


def _resolve_target(db: Session, imp: Import, decision: Decision) -> Work | None:
    target = decision.target.model_dump() if decision.target else imp.target
    if target is None:
        p = imp.proposal or propose(db, imp)
        target = {"action": p["action"], "work_id": p.get("work_id")}
    if target["action"] == "new":
        return None
    work = db.get(Work, target.get("work_id") or 0)
    if work is None or work.trashed_at is not None:
        raise ImportError_(409, "target_gone")
    return work


_FILLABLE = ("isbn", "language", "description", "publisher", "published", "series", "series_index")


def _fill_empty(work: Work, meta: Metadata) -> list[str]:
    """Joining never overwrites what the existing book says; it fills what it lacks."""
    filled = []
    data = meta.model_dump()
    for k in _FILLABLE:
        if getattr(work, k) in (None, "") and data.get(k) not in (None, ""):
            setattr(work, k, data[k])
            filled.append(k)
    if not work.narrators and data.get("narrators"):
        work.narrators, filled = data["narrators"], [*filled, "narrators"]
    if not work.tags and data.get("tags"):
        work.tags, filled = data["tags"], [*filled, "tags"]
    return filled


def file(
    db: Session,
    settings: Settings,
    user: User,
    imp: Import,
    decision: Decision,
    journal,
    *,
    auto: bool = False,
) -> tuple[Work, dict]:
    """File the import. Idempotent: a filed import returns its work again."""
    from lectern.align import jobs as align_jobs
    from lectern.editing import bump_version
    from lectern.ingest.audio import relayout
    from lectern.ingest.sentences import ensure_text_sentences

    if imp.status == ImportStatus.FILED:
        work = db.get(Work, imp.work_id)
        if work is None:
            raise ImportError_(410, "work_gone")
        return work, {"already_filed": True}
    if imp.status != ImportStatus.READY:
        raise ImportError_(409, "not_ready", status=imp.status.value)
    if decision.version is not None and decision.version != imp.version:
        raise ImportError_(409, "version_conflict")
    meta = decision.metadata or Metadata.model_validate(imp.metadata_ or imp.detected)
    library = settings.library_dir
    folder = staging_dir(settings, imp)

    # A file filed elsewhere since the upload is a duplicate now.
    for f in imp.files:
        if f.status == "ok":
            dup = _duplicate_of(db, f.sha256)
            if dup is not None:
                f.status, f.duplicate_work_id = "duplicate", dup
    ok = [f for f in imp.files if f.status == "ok"]
    if not ok:
        raise ImportError_(409, "nothing_to_file")
    books = [f for f in ok if f.kind == FileKind.EBOOK]
    audio = sorted(
        (f for f in ok if f.kind == FileKind.AUDIO),
        key=lambda f: (
            (f.probe or {}).get("disc_no") or 1,
            (f.probe or {}).get("track_no") or 0,
            _natural(f.filename),
        ),
    )

    work = _resolve_target(db, imp, decision)
    created = work is None
    report: dict = {"created": created, "filled": []}
    if work is not None:
        if audio and _work_has_audio(db, work.id) and decision.audio != "append":
            raise ImportError_(409, "target_has_audio")
        bump_version(db, work, work.version)
        report["filled"] = _fill_empty(work, meta)
    else:
        names = [a.name for a in meta.authors] or ["Unknown Author"]
        work = new_work(db, library, title=meta.title, author=names[0])
        journal.add(_rmdir_if_empty(library / work.dir_path))
        apply_metadata(work, meta)
    work_dir = library / work.dir_path
    work_dir.mkdir(parents=True, exist_ok=True)

    filed: list[BookFile] = list(work.files)
    for f in books:
        name = unique_name(work_dir, book_file_name(filed, f.media_type, f.sha256))
        src, dst = checked(folder, f.rel_path), checked(work_dir, name)
        move_file(src, dst, sha256=f.sha256)
        journal.moved_file(src, dst)
        row = BookFile(
            work_id=work.id,
            kind=FileKind.EBOOK,
            rel_path=name,
            media_type=f.media_type,
            size=f.size,
            sha256=f.sha256,
            partial_md5=f.partial_md5,
        )
        filed.append(row)
        db.add(row)
    if audio:
        existing = db.scalars(select(AudioTrack).where(AudioTrack.work_id == work.id)).all()
        discs = [int(m.group(1)) for t in existing if (m := re.match(r"audio/D(\d+)-", t.rel_path))]
        disc = (max(discs) + 1) if existing and discs else 1
        taken = {t.rel_path for t in existing}
        for i, f in enumerate(audio):
            p = f.probe or {}
            title = (p.get("title") or Path(f.filename).stem)[:256]
            rel = f"audio/D{disc:02d}-T{i + 1:03d} {safe_component(title, 'Track')[:80]}{Path(f.rel_path).suffix}"
            rel = unique_name(work_dir, rel, taken)
            taken.add(rel)
            src, dst = checked(folder, f.rel_path), checked(work_dir, rel)
            move_file(src, dst, sha256=f.sha256)
            journal.moved_file(src, dst)
            db.add(
                AudioTrack(
                    work_id=work.id,
                    position=800_000 + len(existing) + i,
                    rel_path=rel,
                    media_type=p.get("media_type") or f.media_type,
                    codec=(p.get("codec") or "unknown")[:32],
                    duration_s=float(p.get("duration_s") or 0.0),
                    offset_s=0.0,
                    size=f.size,
                    sha256=f.sha256,
                    chapters=p.get("chapters") or [{"title": title, "start_s": 0.0}],
                    title=p.get("title"),
                )
            )
        db.flush()
        relayout(db, work.id)  # refresh_work below handles alignment staleness

    use_cover = decision.use_cover if decision.use_cover is not None else imp.use_cover
    if use_cover is None:
        use_cover = created or not work.has_cover
    staged_cover = folder / "cover.jpg"
    if use_cover and staged_cover.is_file():
        cover = work_dir / "cover.jpg"
        if cover.exists():
            backup = settings.data_dir / "revisions" / str(work.id)
            backup.mkdir(parents=True, exist_ok=True)
            saved = backup / f"import-{imp.public_id}.jpg"
            shutil.copy2(cover, saved)
            journal.add(lambda: shutil.copy2(saved, cover))
        else:
            journal.add(lambda: cover.unlink(missing_ok=True))
        shutil.copyfile(staged_cover, cover)
        work.has_cover, work.cover_source = True, "embedded"
        work.cover_tint = dominant_tint(cover)
        report["cover"] = True

    db.flush()
    db.expire(work, ["files"])
    ensure_text_sentences(db, library, work.id)
    align_jobs.refresh_work(db, library, work)
    if audio and settings.audio_low_auto:
        from lectern.jobs import auto_enqueue

        auto_enqueue(db, settings, work)
    imp.status, imp.work_id, imp.filed_at, imp.auto_filed = (
        ImportStatus.FILED,
        work.id,
        utcnow(),
        auto,
    )
    imp.target = {"action": "new" if created else "join", "work_id": work.id}
    _touch(imp)
    db.flush()
    journal.add(_restore_file(work_dir / BOOK_JSON))
    write_book_json(db, library, work)
    report["skipped_duplicates"] = [f.filename for f in imp.files if f.status == "duplicate"]
    return work, report


def _restore_file(path: Path):
    """Undo for rewriting ``path``: put the old bytes back, or remove it if it was new. Filing
    can still be refused after this (an AI key's ``file`` scope is checked last), and the
    sidecar must then describe the book as it was, not files that were moved back out."""
    old = path.read_bytes() if path.is_file() else None

    def undo() -> None:
        if old is None:
            path.unlink(missing_ok=True)
        else:
            tmp = path.with_name(path.name + ".restore")
            tmp.write_bytes(old)
            tmp.replace(path)

    return undo


def _rmdir_if_empty(d: Path):
    def undo() -> None:
        try:
            d.rmdir()
            d.parent.rmdir()
        except OSError:
            pass

    return undo


def cleanup_staging(settings: Settings, imp: Import) -> None:
    """After filing or discarding (outside the transaction: only leftovers remain)."""
    folder = staging_dir(settings, imp)
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)


def discard(db: Session, settings: Settings, imp: Import) -> None:
    if imp.status == ImportStatus.FILED:
        raise ImportError_(409, "already_filed")
    for s in db.scalars(
        select(UploadSession).where(
            UploadSession.import_id == imp.id, UploadSession.status == UploadStatus.ACTIVE
        )
    ):
        from lectern.uploads import part_path

        s.status = UploadStatus.CANCELLED
        part_path(settings, s.public_id).unlink(missing_ok=True)
    imp.status = ImportStatus.DISCARDED
    _touch(imp)
    db.flush()


def remove_file(db: Session, settings: Settings, imp: Import, file_id: int) -> None:
    if imp.status not in OPEN:
        raise ImportError_(409, "import_closed", status=imp.status.value)
    f = db.get(ImportFile, file_id)
    if f is None or f.import_id != imp.id:
        raise ImportError_(404, "file_not_found")
    checked(staging_dir(settings, imp), f.rel_path).unlink(missing_ok=True)
    imp.files.remove(f)
    db.flush()
    if imp.status == ImportStatus.READY and imp.files:
        analyze(db, imp)
    _touch(imp)


def combine(db: Session, settings: Settings, imp: Import, other: Import) -> None:
    """Move every file of ``other`` (the same person's) into ``imp``; ``other`` is discarded."""
    if imp.id == other.id or imp.user_id != other.user_id:
        raise ImportError_(400, "cannot_combine")
    if imp.status not in OPEN or other.status not in OPEN:
        raise ImportError_(409, "import_closed")
    src_dir, dst_dir = staging_dir(settings, other), staging_dir(settings, imp)
    dst_dir.mkdir(parents=True, exist_ok=True)
    have = {f.sha256 for f in imp.files}
    for f in list(other.files):
        src = checked(src_dir, f.rel_path)
        if f.sha256 in have:
            src.unlink(missing_ok=True)
            db.delete(f)
            continue
        rel = unique_name(dst_dir, f.rel_path)
        if src.is_file():
            move_file(src, checked(dst_dir, rel))
        f.import_, f.rel_path = imp, rel  # re-parent (not remove: that would delete the row)
        have.add(f.sha256)
    db.flush()
    if (src_dir / "cover.jpg").is_file() and not (dst_dir / "cover.jpg").exists():
        move_file(src_dir / "cover.jpg", dst_dir / "cover.jpg")
    db.execute(
        UploadSession.__table__.update()
        .where(UploadSession.import_id == other.id)
        .values(import_id=imp.id)
    )
    other.status = ImportStatus.DISCARDED
    _touch(other)
    db.flush()
    if imp.status == ImportStatus.READY:
        analyze(db, imp)
    _touch(imp)
    db.flush()
    shutil.rmtree(src_dir, ignore_errors=True)


# --- JSON and housekeeping ----------------------------------------------------------------------


def import_json(db: Session, settings: Settings, imp: Import, *, refresh: bool = True) -> dict:
    if refresh and imp.status == ImportStatus.READY:
        imp.proposal = propose(db, imp)  # other filings may have changed the best match
    dup_ids = {f.duplicate_work_id for f in imp.files if f.duplicate_work_id}
    dups = (
        {w.id: w for w in db.scalars(select(Work).where(Work.id.in_(dup_ids)))} if dup_ids else {}
    )
    active = db.scalar(
        select(func.count())
        .select_from(UploadSession)
        .where(UploadSession.import_id == imp.id, UploadSession.status == UploadStatus.ACTIVE)
    )
    work = db.get(Work, imp.work_id) if imp.work_id else None
    from lectern.routes.api.serializers import iso

    has_cover = imp.status in OPEN and (staging_dir(settings, imp) / "cover.jpg").is_file()
    return {
        "id": imp.public_id,
        "label": imp.label,
        "status": imp.status.value,
        "error": imp.error,
        "version": imp.version,
        "created_at": iso(imp.created_at),
        "updated_at": iso(imp.updated_at),
        "expires_at": iso(imp.expires_at),
        "filed_at": iso(imp.filed_at),
        "auto_filed": imp.auto_filed,
        "files": [
            {
                "id": f.id,
                "filename": f.filename,
                "kind": f.kind.value,
                "media_type": f.media_type,
                "size": f.size,
                "sha256": f.sha256,
                "status": f.status,
                "duplicate_of": (
                    {
                        "work_id": f.duplicate_work_id,
                        "title": dups[f.duplicate_work_id].title,
                        "in_trash": dups[f.duplicate_work_id].trashed_at is not None,
                    }
                    if f.duplicate_work_id in dups
                    else None
                ),
                "duration_s": (f.probe or {}).get("duration_s"),
                "track_no": (f.probe or {}).get("track_no"),
                "disc_no": (f.probe or {}).get("disc_no"),
                "chapters": len((f.probe or {}).get("chapters") or []),
                "pages": (f.probe or {}).get("pages"),
                "converted_from": (f.probe or {}).get("converted_from"),
            }
            for f in imp.files
        ],
        "uploads_active": active or 0,
        "detected": imp.detected,
        "metadata": imp.metadata_,
        "proposal": imp.proposal,
        "target": imp.target,
        "use_cover": imp.use_cover,
        "cover_url": f"/api/imports/{imp.public_id}/cover.jpg?v={imp.version}"
        if has_cover
        else None,
        "work": {"id": work.id, "title": work.title, "author": work.author} if work else None,
    }


def sweep(db: Session, settings: Settings) -> dict[str, int]:
    """Expire abandoned imports (their staged files go), forget old finished ones, and remove
    staging folders no import owns."""
    now = utcnow()
    counts = {"expired": 0, "forgotten": 0, "orphans": 0}
    for imp in db.scalars(select(Import).where(Import.status.in_(OPEN), Import.expires_at < now)):
        imp.status, imp.error = ImportStatus.DISCARDED, "expired"
        cleanup_staging(settings, imp)
        counts["expired"] += 1
    for imp in db.scalars(
        select(Import).where(~Import.status.in_(OPEN), Import.updated_at < now - FINISHED_RETENTION)
    ):
        db.delete(imp)
        counts["forgotten"] += 1
    db.flush()
    root = settings.library_dir / STAGING_DIR
    if root.is_dir():
        live = set(db.scalars(select(Import.public_id).where(Import.status.in_(OPEN))))
        for d in root.iterdir():
            if d.is_dir() and d.name not in live:
                try:
                    young = (now.timestamp() - d.stat().st_mtime) < 600
                except OSError:
                    continue
                if not young:
                    shutil.rmtree(d, ignore_errors=True)
                    counts["orphans"] += 1
    if any(counts.values()):
        log.info("import sweep: %s", counts)
    return counts
