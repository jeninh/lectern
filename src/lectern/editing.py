"""Editing a work: one validated, versioned save, and undo of that save.

``save`` applies a :class:`WorkPatch` (metadata, shelves, primary EPUB, playback defaults,
track order/names/chapter titles, removed formats, cover) as one unit:

- the caller sends the ``version`` it edited; a different current version is
  ``409 version_conflict`` and nothing changes (no silent last-writer-wins);
- every filesystem step (track renames, the folder rename an author/title change implies,
  formats moved to the trash, the cover) is journalled, and :class:`Journal.rollback` undoes
  them if the database transaction fails, so disk and database never disagree;
- a :class:`~lectern.models.WorkRevision` records the before and after of every aspect the
  save changed; ``undo`` restores the before state while the work is still at that version.

File contents are never rewritten: EPUBs and audio keep their bytes, SHA-256, and KOReader
partial MD5 through renames and folder moves, so sync and alignments stay valid. Changing the
audio order, removing a track or EPUB, or picking another primary EPUB invalidates the
alignment through the normal staleness rules (``lectern.align.jobs.refresh_work``).

Every function expects :data:`lectern.fsops.LIBRARY_LOCK` held and leaves the commit to the
caller. HTTP-free on purpose: the web API and any later automation client share it.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lectern.access import shelf_perms, visible_path
from lectern.config import Settings
from lectern.fsops import checked, free_dir, move_dir, move_file, prune_empty_parents
from lectern.ingest.library import safe_component
from lectern.models import (
    Alignment,
    AudioTrack,
    BookFile,
    FileKind,
    Shelf,
    ShelfItem,
    TrashedFile,
    User,
    Work,
    WorkRevision,
    utcnow,
)
from lectern.trash import restore_file, trash_file
from lectern.workmeta import (
    Metadata,
    NoControls,
    PlaybackDefault,
    apply_metadata,
    desired_dir_parts,
    folder_matches,
    metadata_of,
    write_book_json,
)

log = logging.getLogger("lectern.editing")

COVER_MAX_BYTES = 10 * 1024 * 1024
# Works whose folder rename had to wait for a background build; retried by the job runner.
# In memory on purpose: after a restart the next save of the work renames it.
PENDING_FOLDERS: set[int] = set()
STAGED_COVER_TTL_S = 24 * 3600
_TOKEN = re.compile(r"^\d+\.[0-9a-f]{24}$")
_BACKUP_NAME = re.compile(r"^([0-9a-f]{16}|import-[\w-]+)\.jpg$")


class EditError(Exception):
    """A refused edit. ``code`` is the API's machine-readable ``detail``."""

    def __init__(self, http_status: int, code: str, **extra) -> None:
        super().__init__(code)
        self.status, self.code, self.extra = http_status, code, extra


# --- journal ---------------------------------------------------------------------------------


@dataclass
class Journal:
    """Inverse filesystem steps, replayed newest first if the transaction fails."""

    steps: list[Callable[[], None]] = field(default_factory=list)

    def add(self, fn: Callable[[], None]) -> None:
        self.steps.append(fn)

    def moved_file(self, src: Path, dst: Path) -> None:
        self.add(lambda: move_file(dst, src))

    def moved_dir(self, src: Path, dst: Path) -> None:
        self.add(lambda: move_dir(dst, src))

    def rollback(self) -> None:
        for fn in reversed(self.steps):
            try:
                fn()
            except Exception:  # noqa: BLE001 - keep undoing the rest
                log.exception("rollback step failed")
        self.steps.clear()


# --- schema ----------------------------------------------------------------------------------

Title = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256), NoControls
]
ChapterTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200), NoControls
]


class TrackEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    title: Title | None = None
    chapters: list[ChapterTitle] | None = Field(default=None, max_length=5000)


class CoverChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["upload", "embedded", "remove", "generate", "revision"]
    token: str | None = Field(default=None, max_length=64)
    # "revision": put back the cover image saved in that revision of this work (from history).
    revision_id: int | None = Field(default=None, ge=1, le=2**62)
    # "generate": None = the style the title/author hash picks; seed = palette shuffle.
    style: Literal["classic", "modern", "minimal"] | None = None
    seed: int = Field(default=0, ge=0, le=1_000_000)
    font: Literal["display", "text", "noto"] = "display"


class WorkPatch(BaseModel):
    """One save from the editor. Absent fields stay as they are; ``tracks`` is the complete
    play order (every current track exactly once); ``shelves`` is the complete set among the
    shelves the editor may change."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    metadata: Metadata | None = None
    shelves: list[int] | None = Field(default=None, max_length=1000)
    primary_file_id: int | None = None
    playback_default: PlaybackDefault = None
    readalong_enabled: bool | None = None
    data_saver_enabled: bool | None = None
    tracks: list[TrackEdit] | None = Field(default=None, max_length=5000)
    remove_file_ids: list[int] = Field(default_factory=list, max_length=200)
    remove_track_ids: list[int] = Field(default_factory=list, max_length=5000)
    cover: CoverChange | None = None


@dataclass
class SaveResult:
    revision: WorkRevision | None
    folder: dict
    changed: list[str]


# --- helpers ---------------------------------------------------------------------------------

_SLOT = re.compile(r"^D\d+-T\d+\s+")


def track_title(t: AudioTrack) -> str:
    stem = Path(t.rel_path).stem
    return t.title or _SLOT.sub("", stem) or stem


def track_rel(index: int, title: str, suffix: str) -> str:
    return f"audio/D01-T{index + 1:03d} {safe_component(title, 'Track')[:80]}{suffix.lower()}"


def ordered_tracks(db: Session, work_id: int) -> list[AudioTrack]:
    return list(
        db.scalars(
            select(AudioTrack).where(AudioTrack.work_id == work_id).order_by(AudioTrack.position)
        )
    )


def _tracks_state(tracks: list[AudioTrack]) -> list[dict]:
    return [
        {"id": t.id, "title": t.title, "rel_path": t.rel_path, "chapters": t.chapters or []}
        for t in tracks
    ]


def editable_shelves(db: Session, user: User) -> list[Shelf]:
    """Manual shelves ``user`` may add works to and remove them from, in folder order."""
    shelves, perms = shelf_perms(db, user)
    out = [shelves[sid] for sid, p in perms.items() if p != "view" and shelves[sid].rule is None]
    return sorted(out, key=lambda s: [x.name.casefold() for x in visible_path(s, shelves, perms)])


def bump_version(db: Session, work: Work, expected: int) -> int:
    """``UPDATE ... WHERE version = expected``: a concurrent writer makes this a conflict."""
    db.flush()
    res = db.execute(
        update(Work)
        .where(Work.id == work.id, Work.version == expected)
        .values(version=expected + 1, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        raise EditError(409, "version_conflict")
    db.expire(work, ["version", "updated_at"])
    return expected + 1


def place_tracks(work_dir: Path, plan: list[tuple[AudioTrack, str]], journal: Journal, db) -> None:
    """Rename track files to new relative paths in two phases (through hidden temporary names)
    so swaps and rotations never collide, on disk or in ``uq_audio_track_path``."""
    pending = [(t, new) for t, new in plan if t.rel_path != new]
    if not pending:
        return
    staged: list[tuple[AudioTrack, str, str]] = []
    for t, new in pending:
        src = checked(work_dir, t.rel_path)
        tmp = f"audio/.lectern-tmp-{secrets.token_hex(6)}{Path(t.rel_path).suffix.lower()}"
        if src.is_file():
            move_file(src, work_dir / tmp, sha256=t.sha256)
            journal.moved_file(src, work_dir / tmp)
        staged.append((t, tmp, new))
        t.rel_path = tmp
    db.flush()
    for t, tmp, new in staged:
        dst = checked(work_dir, new)
        if (work_dir / tmp).is_file():
            move_file(work_dir / tmp, dst, sha256=t.sha256)
            journal.moved_file(work_dir / tmp, dst)
        t.rel_path = new
    db.flush()
    for d in sorted({(work_dir / old).parent for _, old in pending}, reverse=True):
        if d != work_dir:
            prune_empty_parents(d, work_dir)


def reconcile_folder(
    db: Session,
    settings: Settings,
    work: Work,
    journal: Journal,
    busy: Callable[[int], bool],
    *,
    prefer: str | None = None,
) -> dict:
    """Rename the work's folder to ``<first author>/<title>`` when it no longer matches.

    A folder a background build is writing into is left alone for now (``pending``); the
    hourly sweep and the next save try again."""
    if work.trashed_at is not None:
        return {"moved": False, "dir_path": work.dir_path}
    if prefer is None and folder_matches(work):
        PENDING_FOLDERS.discard(work.id)
        return {"moved": False, "dir_path": work.dir_path}
    if busy(work.id):
        PENDING_FOLDERS.add(work.id)
        return {"moved": False, "pending": True, "dir_path": work.dir_path}
    PENDING_FOLDERS.discard(work.id)
    library = settings.library_dir
    old = work.dir_path
    if prefer and len(Path(prefer).parts) == 2 and not (library / prefer).exists():
        rel = prefer
    else:
        author, title = desired_dir_parts(work)
        rel = free_dir(library, author, title, keep=old)
    if rel == old:
        return {"moved": False, "dir_path": old}
    src, dst = library / old, library / rel
    if src.is_dir():
        digests = {f.rel_path: f.sha256 for f in work.files}
        digests |= {t.rel_path: t.sha256 for t in ordered_tracks(db, work.id)}
        move_dir(src, dst, digests=digests)
        journal.moved_dir(src, dst)
        prune_empty_parents(src.parent, library)
    else:
        dst.mkdir(parents=True, exist_ok=True)
    work.dir_path = rel
    db.flush()
    return {"moved": True, "from": old, "dir_path": rel}


# --- covers ------------------------------------------------------------------------------------


def staged_covers_dir(settings: Settings) -> Path:
    d = settings.data_dir / "staged-covers"
    d.mkdir(parents=True, exist_ok=True)
    return d


STAGED_PER_USER_MAX = 100


def _sidecar(settings: Settings, token: str) -> Path:
    return staged_covers_dir(settings) / f"{token}.json"


def stage_cover(
    settings: Settings,
    user_id: int,
    data: bytes,
    *,
    formats: tuple[str, ...] | None = None,
    origin: dict | None = None,
) -> str:
    """Validate an image and keep it as JPEG until a save uses it (or a day passes). Returns a
    token bound to the user. ``origin`` (``{"source": "page"|"image", "work_id", "file_id",
    "page"|"index"}``) is written beside it as ``<token>.json`` so the save can record where
    the cover came from; only the server passes it. Uploads accept JPEG, PNG, and WebP only; a
    server-side source passes ``formats`` (EPUB images may be GIF)."""
    from lectern.ingest.cover import UPLOAD_FORMATS, CoverRejected, save_cover_jpeg

    if len(data) > COVER_MAX_BYTES:
        raise EditError(413, "cover_too_large")
    _evict_oldest(settings, user_id)
    token = f"{user_id}.{secrets.token_hex(12)}"
    sidecar = _sidecar(settings, token)
    if origin is not None:
        sidecar.write_text(json.dumps(origin, separators=(",", ":")), "utf-8")
    try:
        # Allowlisted Pillow decoders only: no exotic formats reach Pillow from untrusted bytes.
        save_cover_jpeg(
            data, staged_covers_dir(settings) / f"{token}.jpg", formats=formats or UPLOAD_FORMATS
        )
    except CoverRejected as exc:
        sidecar.unlink(missing_ok=True)
        # A decompression bomb is "too large" (413); anything else Pillow refuses is not an image.
        raise EditError(413 if exc.code == "cover_too_large" else 422, exc.code) from None
    return token


def _evict_oldest(settings: Settings, user_id: int) -> None:
    """Keep at most STAGED_PER_USER_MAX staged covers per user so staging cannot fill the disk."""
    mine = []
    for p in staged_covers_dir(settings).glob(f"{int(user_id)}.*.jpg"):
        try:
            mine.append((p.stat().st_mtime, p))
        except OSError:
            continue
    for _mtime, p in sorted(mine)[: max(0, len(mine) - STAGED_PER_USER_MAX + 1)]:
        p.unlink(missing_ok=True)
        p.with_suffix(".json").unlink(missing_ok=True)


def staged_origin(settings: Settings, user_id: int, token: str, work_id: int) -> dict | None:
    """Where a staged cover came from, or None (an upload, or a cover staged for another work).
    Re-validated here: the sidecar is data on disk, not trusted structure."""
    try:
        raw = json.loads(_sidecar(settings, token).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("work_id") != work_id:
        return None
    ints = {k: raw.get(k) for k in ("file_id", "page", "index")}
    if any(v is not None and (type(v) is not int or v < 0) for v in ints.values()):
        return None
    if raw.get("source") == "page" and ints["page"] and ints["file_id"]:
        return {"source": "page", "file_id": ints["file_id"], "page": ints["page"]}
    if raw.get("source") == "image" and ints["index"] is not None and ints["file_id"]:
        return {"source": "image", "file_id": ints["file_id"], "index": ints["index"]}
    return None


def staged_cover_path(settings: Settings, user_id: int, token: str) -> Path:
    if not _TOKEN.fullmatch(token or "") or not token.startswith(f"{user_id}."):
        raise EditError(404, "cover_not_found")
    p = staged_covers_dir(settings) / f"{token}.jpg"
    if not p.is_file():
        raise EditError(404, "cover_not_found")
    return p


def _embedded_cover(db: Session, settings: Settings, work: Work, dest: Path) -> bool:
    from lectern.ingest import formats
    from lectern.ingest.audio import AudioError, _save_cover, probe
    from lectern.ingest.epub import EpubError, extract_cover, read_epub_metadata
    from lectern.ingest.library import save_first_page_cover
    from lectern.ingest.sentences import primary_epub

    work_dir = settings.library_dir / work.dir_path
    books = [primary_epub(db, work.id)] + [f for f in work.files if f.kind == FileKind.EBOOK]
    books = [b for b in books if b is not None]
    for book in [b for b in books if b.media_type == formats.EPUB]:
        path = work_dir / book.rel_path
        try:
            meta = read_epub_metadata(path)
            if meta.cover_href and extract_cover(path, meta.cover_href, dest):
                return True
        except (EpubError, OSError, KeyError):
            continue
    for book in [b for b in books if b.media_type in formats.PAGED]:  # first page as cover
        try:
            if save_first_page_cover(
                formats.probe(work_dir / book.rel_path, book.media_type).cover, dest
            ):
                return True
        except (formats.FormatError, formats.ProbeBusy, OSError):
            continue
    for t in ordered_tracks(db, work.id)[:3]:
        try:
            info = probe(work_dir / t.rel_path, settings.ffprobe_path)
        except (AudioError, OSError):
            continue
        if info.cover and _save_cover(info.cover, dest):
            return True
    return False


def embedded_cover_available(db: Session, settings: Settings, work: Work) -> bool:
    from lectern.ingest.epub import EpubError, read_epub_metadata
    from lectern.ingest.formats import PAGED

    work_dir = settings.library_dir / work.dir_path
    if any(f.media_type in PAGED for f in work.files):
        return True  # a PDF's or comic's first page
    for f in work.files:
        if f.kind == FileKind.EBOOK:
            try:
                if read_epub_metadata(work_dir / f.rel_path).cover_href:
                    return True
            except (EpubError, OSError):
                continue
    return bool(ordered_tracks(db, work.id))


def revision_backup(settings: Settings, work_id: int, rel: object) -> Path | None:
    """The backup image a revision's ``before.cover.backup`` names, or None. The value is data
    from the database, so it is trusted only when it resolves to a regular file directly
    inside ``data/revisions/<work_id>/`` with a name this server generates; nothing else is
    ever opened."""
    if not isinstance(rel, str) or "\x00" in rel:
        return None
    base = (settings.data_dir / "revisions" / str(work_id)).resolve()
    try:
        path = (settings.data_dir / rel).resolve()
        if path.parent != base or not _BACKUP_NAME.fullmatch(path.name) or not path.is_file():
            return None
    except (OSError, ValueError):
        return None
    return path


def revision_cover(db: Session, settings: Settings, work_id: int, revision_id: int | None):
    """``(state, backup path)`` of the cover image revision ``revision_id`` of this work
    replaced, or 404 ``cover_not_found`` (other work's revision, no image, file gone)."""
    rev = db.get(WorkRevision, revision_id) if revision_id else None
    state = ((rev.before or {}).get("cover") if rev and rev.work_id == work_id else None) or {}
    path = revision_backup(settings, work_id, state.get("backup")) if state else None
    if path is None or not state.get("has_cover"):
        raise EditError(404, "cover_not_found")
    return state, path


def _cover_state(work: Work) -> dict:
    return {
        "has_cover": work.has_cover,
        "source": work.cover_source,
        "tint": work.cover_tint,
        "style": work.cover_style,
        "seed": work.cover_seed or 0,
        "font": work.cover_font,
    }


def apply_cover(
    db: Session, settings: Settings, work: Work, change: CoverChange, user_id: int, journal: Journal
) -> tuple[dict, dict]:
    from lectern.ingest.cover import dominant_tint

    work_dir = settings.library_dir / work.dir_path
    work_dir.mkdir(parents=True, exist_ok=True)
    cover = work_dir / "cover.jpg"
    before = _cover_state(work)
    backups = settings.data_dir / "revisions" / str(work.id)
    if cover.is_file():
        backups.mkdir(parents=True, exist_ok=True)
        backup = backups / f"{secrets.token_hex(8)}.jpg"
        shutil.copy2(cover, backup)
        before["backup"] = backup.relative_to(settings.data_dir).as_posix()
        journal.add(lambda: shutil.copy2(backup, cover))
    else:
        journal.add(lambda: cover.unlink(missing_ok=True))
    new = work_dir / f".cover-{secrets.token_hex(4)}.jpg"
    origin: dict | None = None
    try:
        if change.action == "upload":
            src = staged_cover_path(settings, user_id, change.token or "")
            origin = staged_origin(settings, user_id, change.token or "", work.id)
            shutil.copyfile(src, new)
            src.unlink(missing_ok=True)
            _sidecar(settings, change.token or "").unlink(missing_ok=True)
        elif change.action == "revision":
            old, src = revision_cover(db, settings, work.id, change.revision_id)
            shutil.copyfile(src, new)
            origin = {"source": old.get("source") or "upload"}
        elif change.action == "embedded":
            if not _embedded_cover(db, settings, work, new):
                raise EditError(422, "no_embedded_cover")
        if change.action in ("remove", "generate"):
            # Without art the generated cover shows; "generate" also picks its look.
            cover.unlink(missing_ok=True)
            work.has_cover, work.cover_tint, work.cover_source = False, None, None
            if change.action == "generate":
                work.cover_style, work.cover_seed = change.style, change.seed
                work.cover_font = None if change.font == "display" else change.font
        else:
            new.replace(cover)
            work.has_cover, work.cover_source = True, change.action
            if origin is not None:  # staged from a page or image of this work; or reverted
                work.cover_source = origin["source"]
            work.cover_tint = dominant_tint(cover)
    finally:
        new.unlink(missing_ok=True)
    after = _cover_state(work)
    if origin is not None and change.action != "revision":
        after["origin"] = origin
    return before, after


def _restore_cover(settings: Settings, work: Work, state: dict) -> None:
    cover = settings.library_dir / work.dir_path / "cover.jpg"
    backup = state.get("backup")
    if state.get("has_cover") and backup and (settings.data_dir / backup).is_file():
        shutil.copy2(settings.data_dir / backup, cover)
    elif not state.get("has_cover"):
        cover.unlink(missing_ok=True)
    work.has_cover = bool(state.get("has_cover")) and cover.is_file()
    work.cover_source = state.get("source") if work.has_cover else None
    work.cover_tint = state.get("tint") if work.has_cover else None
    if "style" in state:  # revisions saved before generated covers lack these
        work.cover_style, work.cover_seed = state.get("style"), state.get("seed") or 0
        work.cover_font = state.get("font")


# --- text and audio invalidation ---------------------------------------------------------------


def _inputs_changed(
    db: Session, settings: Settings, work: Work, *, text: bool, audio: bool
) -> None:
    """The EPUB the text comes from or the audio changed: rebuild the text-only sentence list
    (if not aligned) and let the staleness rules cancel the in-flight job, mark a finished
    alignment stale, and queue a fresh one with the same model (``refresh_work``)."""
    from lectern.align import jobs as align_jobs
    from lectern.ingest.audio import relayout

    if audio:
        relayout(db, work.id)
    if text and db.scalar(select(Alignment.id).where(Alignment.work_id == work.id)) is None:
        from lectern.ingest.epub import EpubError
        from lectern.ingest.sentences import epub_sentences, primary_epub, replace_sentences

        book = primary_epub(db, work.id)
        if book is not None:
            try:
                entries = epub_sentences(settings.library_dir / work.dir_path / book.rel_path)
            except (EpubError, OSError, ValueError) as exc:
                log.warning("could not segment work %d: %s", work.id, exc)
            else:
                replace_sentences(db, work.id, entries)
    align_jobs.refresh_work(db, settings.library_dir, work, auto_enqueue=False)


# --- save ----------------------------------------------------------------------------------------


def save(
    db: Session,
    settings: Settings,
    user: User,
    work: Work,
    patch: WorkPatch,
    journal: Journal,
    *,
    busy: Callable[[int], bool] = lambda _id: False,
    kind: str = "edit",
    batch_id: str | None = None,
) -> SaveResult:
    if work.trashed_at is not None:
        raise EditError(404, "not_found")
    if work.version != patch.version:
        raise EditError(409, "version_conflict")
    sent = patch.model_fields_set
    before: dict = {}
    after: dict = {}
    text_changed = audio_changed = False
    library = settings.library_dir
    work_dir = library / work.dir_path

    def record(aspect: str, old, new) -> None:
        if old != new:
            before[aspect], after[aspect] = old, new

    old_primary = work.primary_file_id
    old_tracks = ordered_tracks(db, work.id)
    old_track_state = _tracks_state(old_tracks)

    if patch.metadata is not None:
        old = metadata_of(work)
        apply_metadata(work, patch.metadata)
        record("metadata", old, metadata_of(work))

    files = {f.id: f for f in work.files}
    tracks = {t.id: t for t in old_tracks}
    removed: list[int] = []
    for fid in dict.fromkeys(patch.remove_file_ids):
        if fid not in files:
            raise EditError(404, "file_not_found", file_id=fid)
        if work.primary_file_id == fid:
            work.primary_file_id = None
        tf = trash_file(db, settings, work, files[fid], user.id)
        journal.moved_file(work_dir / tf.rel_path, library / tf.trash_path)
        removed.append(tf.id)
        text_changed = True
    for tid in dict.fromkeys(patch.remove_track_ids):
        if tid not in tracks:
            raise EditError(404, "track_not_found", track_id=tid)
        tf = trash_file(db, settings, work, tracks.pop(tid), user.id)
        journal.moved_file(work_dir / tf.rel_path, library / tf.trash_path)
        removed.append(tf.id)
        audio_changed = True
    if removed:
        before["removed"], after["removed"] = [], removed
        db.expire(work, ["files"])

    if "primary_file_id" in sent:
        if patch.primary_file_id is not None:
            book = db.get(BookFile, patch.primary_file_id)
            if (
                book is None
                or book.work_id != work.id
                or book.kind != FileKind.EBOOK
                or book.media_type != "application/epub+zip"
            ):
                raise EditError(422, "primary_not_an_epub_of_this_book")
        work.primary_file_id = patch.primary_file_id
    if old_primary != work.primary_file_id:
        record("primary_file_id", old_primary, work.primary_file_id)
        text_changed = True
    if "playback_default" in sent:
        record("playback_default", work.playback_default, patch.playback_default)
        work.playback_default = patch.playback_default
    if patch.readalong_enabled is not None:
        record("readalong_enabled", work.readalong_enabled, patch.readalong_enabled)
        work.readalong_enabled = patch.readalong_enabled
    if patch.data_saver_enabled is not None:
        record("data_saver_enabled", work.data_saver_enabled, patch.data_saver_enabled)
        work.data_saver_enabled = patch.data_saver_enabled
        if not patch.data_saver_enabled:
            from lectern.jobs import active_job, cancel_transcode

            if (job := active_job(db, work.id)) is not None:
                cancel_transcode(db, job)

    if patch.tracks is not None:
        current = [t for t in old_tracks if t.id in tracks]
        if sorted(e.id for e in patch.tracks) != sorted(t.id for t in current):
            raise EditError(422, "tracks_mismatch")
        plan: list[tuple[AudioTrack, str]] = []
        renamed = False
        for i, e in enumerate(patch.tracks):
            t = tracks[e.id]
            title = e.title or track_title(t)
            if e.title is not None and e.title != track_title(t):
                t.title, renamed = e.title, True
            if e.chapters is not None:
                chapters = list(t.chapters or [])
                if len(e.chapters) != len(chapters):
                    raise EditError(422, "chapters_mismatch", track_id=t.id)
                t.chapters = [
                    {**c, "title": name} for c, name in zip(chapters, e.chapters, strict=True)
                ]
            plan.append((t, track_rel(i, title, Path(t.rel_path).suffix)))
        new_order = [e.id for e in patch.tracks]
        if new_order != [t.id for t in current] or renamed:
            place_tracks(work_dir, plan, journal, db)
            audio_changed = audio_changed or new_order != [t.id for t in current]
    new_track_state = _tracks_state([t for t in ordered_tracks(db, work.id)])
    if patch.tracks is not None:
        by_id = {t["id"]: t for t in new_track_state}
        new_track_state = [by_id[e.id] for e in patch.tracks]
    record("tracks", old_track_state, new_track_state)

    if patch.shelves is not None:
        editable = {s.id: s for s in editable_shelves(db, user)}
        wanted = set(patch.shelves)
        if wanted - set(editable):
            raise EditError(403, "shelf_not_editable")
        on = set(
            db.scalars(
                select(ShelfItem.shelf_id).where(
                    ShelfItem.work_id == work.id, ShelfItem.shelf_id.in_(list(editable))
                )
            )
        )
        _set_shelves(db, work.id, on, wanted)
        record("shelves", sorted(on), sorted(wanted))

    if patch.cover is not None:
        b, a = apply_cover(db, settings, work, patch.cover, user.id, journal)
        before["cover"], after["cover"] = b, a

    if not after:
        return SaveResult(None, {"moved": False, "dir_path": work.dir_path}, [])

    version_before = patch.version
    bump_version(db, work, version_before)
    folder = reconcile_folder(db, settings, work, journal, busy)
    if folder.get("moved"):
        record("dir_path", folder["from"], folder["dir_path"])
    if text_changed or audio_changed:
        _inputs_changed(db, settings, work, text=text_changed, audio=audio_changed)
    rev = WorkRevision(
        work_id=work.id,
        actor_user_id=user.id,
        kind=kind,
        batch_id=batch_id,
        version_before=version_before,
        version_after=version_before + 1,
        before=before,
        after=after,
    )
    db.add(rev)
    db.flush()
    write_book_json(db, library, work)
    return SaveResult(rev, folder, sorted(after))


def _set_shelves(db: Session, work_id: int, on: set[int], wanted: set[int]) -> None:
    from sqlalchemy import func

    for sid in on - wanted:
        item = db.get(ShelfItem, (sid, work_id))
        if item is not None:
            db.delete(item)
    for sid in wanted - on:
        last = db.scalar(select(func.max(ShelfItem.position)).where(ShelfItem.shelf_id == sid))
        db.add(ShelfItem(shelf_id=sid, work_id=work_id, position=(last or 0) + 1))
    db.flush()


# --- undo ----------------------------------------------------------------------------------------


def undo(
    db: Session,
    settings: Settings,
    user: User,
    work: Work,
    rev: WorkRevision,
    journal: Journal,
    *,
    busy: Callable[[int], bool] = lambda _id: False,
) -> dict:
    """Put back what ``rev`` changed. Only while nothing else changed the work since."""
    from lectern import trash

    if rev.work_id != work.id or rev.undone_at is not None or rev.kind == "undo":
        raise EditError(409, "undo_unavailable")
    if work.version != rev.version_after:
        raise EditError(409, "undo_conflict")
    b, a = rev.before, rev.after
    library = settings.library_dir
    text = audio = False

    if rev.kind == "trash":
        try:
            trash.restore_work(db, settings, work, journal=journal)
        except FileExistsError:  # a folder appeared where it was going back to
            raise EditError(409, "undo_conflict") from None
        rev.undone_at = utcnow()
        db.flush()
        return {"restored": True}
    if work.trashed_at is not None:
        raise EditError(409, "undo_unavailable")
    work_dir = library / work.dir_path

    if "metadata" in b:
        apply_metadata(work, b["metadata"])
    id_map: dict[int, int] = {}
    for tf_id in a.get("removed", []):
        tf = db.get(TrashedFile, tf_id)
        if tf is None or tf.restored_at is not None:
            raise EditError(409, "undo_unavailable")
        old_id = tf.row.get("id")
        temp = None
        if tf.kind == FileKind.AUDIO:
            temp = f"audio/.lectern-restore-{secrets.token_hex(6)}{Path(tf.rel_path).suffix}"
        try:
            obj = restore_file(db, settings, tf, rel_path=temp, journal=journal)
        except (FileExistsError, LookupError):
            # The same file was added again (or the removal was restored by hand) since.
            raise EditError(409, "undo_conflict") from None
        id_map[old_id] = obj.id
        if tf.kind == FileKind.AUDIO:
            audio = True
        else:
            text = True
    db.expire(work, ["files"])
    if "primary_file_id" in b:
        old = b["primary_file_id"]
        work.primary_file_id = id_map.get(old, old)
        text = True
    if "playback_default" in b:
        work.playback_default = b["playback_default"]
    if "readalong_enabled" in b:
        work.readalong_enabled = b["readalong_enabled"]
    if "data_saver_enabled" in b:
        work.data_saver_enabled = b["data_saver_enabled"]
    if "tracks" in b:
        current = {t.id: t for t in ordered_tracks(db, work.id)}
        plan = []
        for st in b["tracks"]:
            t = current.get(id_map.get(st["id"], st["id"]))
            if t is None:
                raise EditError(409, "undo_unavailable")
            t.title, t.chapters = st["title"], st["chapters"]
            plan.append((t, st["rel_path"]))
        place_tracks(work_dir, plan, journal, db)
        audio = True
    if "shelves" in b:
        editable = {s.id for s in editable_shelves(db, user)}
        on = set(
            db.scalars(
                select(ShelfItem.shelf_id).where(
                    ShelfItem.work_id == work.id, ShelfItem.shelf_id.in_(list(editable))
                )
            )
        )
        _set_shelves(db, work.id, on, set(b["shelves"]) & editable)
    if "cover" in b:
        _restore_cover(settings, work, b["cover"])
    bump_version(db, work, rev.version_after)
    reconcile_folder(db, settings, work, journal, busy, prefer=b.get("dir_path"))
    if text or audio:
        _inputs_changed(db, settings, work, text=text, audio=audio)
    rev.undone_at = utcnow()
    db.flush()
    write_book_json(db, library, work)
    return {"restored": True}


# --- sweep -----------------------------------------------------------------------------------------


def sweep(db: Session, settings: Settings, busy: Callable[[int], bool] = lambda _id: False) -> int:
    """Forget staged covers nobody saved, and finish folder renames that had to wait for a
    build. Only works an edit left pending are touched: an upgrade never renames folders on
    its own. Caller holds the library lock."""
    import time

    old = time.time() - STAGED_COVER_TTL_S
    for pattern in ("*.jpg", "*.json"):  # a staged cover and its origin sidecar
        for p in staged_covers_dir(settings).glob(pattern):
            try:
                if p.stat().st_mtime < old:
                    p.unlink(missing_ok=True)
            except OSError:
                continue
    moved = 0
    for work_id in sorted(PENDING_FOLDERS):
        work = db.get(Work, work_id)
        if work is None:
            PENDING_FOLDERS.discard(work_id)
            continue
        journal = Journal()
        try:
            res = reconcile_folder(db, settings, work, journal, busy)
            if res.get("moved"):
                write_book_json(db, settings.library_dir, work)
            db.commit()
            moved += int(bool(res.get("moved")))
        except Exception:  # noqa: BLE001
            db.rollback()
            journal.rollback()
            log.exception("folder rename for work %d failed", work_id)
    return moved
