"""Trash: recoverable removal of works and of single formats, purged after 30 days.

A trashed work keeps its rows (positions, shelves, alignment) and its folder moves to
``<library>/_trash/works/<id>-<random>/``; it is invisible everywhere (``lectern.access``)
until restored to a free ``<Author>/<Title>`` folder. A removed format (one EPUB or one audio
track) moves to ``<library>/_trash/files/<random>/<name>`` and its row is kept as JSON in
``trashed_files`` so it can come back with the same id. Nothing is deleted before
``TRASH_RETENTION`` except by the owner's explicit "Delete forever".

All functions expect the caller to hold :data:`lectern.fsops.LIBRARY_LOCK` and to commit.
Functions that move files take a ``journal`` (:class:`lectern.editing.Journal`) so the caller
can put the files back when the commit fails.
"""

from __future__ import annotations

import logging
import secrets
import shutil
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern.config import Settings
from lectern.fsops import (
    TRASH_DIR,
    checked,
    free_dir,
    inside,
    move_dir,
    move_file,
    prune_empty_parents,
)
from lectern.models import (
    JOB_ACTIVE_STATUSES,
    AudioTrack,
    BookFile,
    FileKind,
    Job,
    JobStatus,
    TrashedFile,
    Work,
    WorkRevision,
    utcnow,
)
from lectern.workmeta import desired_dir_parts, write_book_json

if TYPE_CHECKING:
    from lectern.editing import Journal

log = logging.getLogger("lectern.trash")

TRASH_RETENTION = timedelta(days=30)


class Busy(Exception):
    """A background build is writing into the work's folder right now."""


def _cancel_jobs(db: Session, work_id: int) -> None:
    from lectern.align import jobs as align_jobs

    for job in db.scalars(
        select(Job).where(Job.work_id == work_id, Job.status.in_(JOB_ACTIVE_STATUSES))
    ):
        if job.kind == align_jobs.KIND_ALIGN:
            align_jobs.cancel_job(db, job)
        else:
            job.status, job.finished_at = JobStatus.CANCELLED, utcnow()
            job.lease_token_hash, job.lease_expires_at = None, None


def _digests(db: Session, work_id: int) -> dict[str, str]:
    out = {
        f.rel_path: f.sha256
        for f in db.scalars(select(BookFile).where(BookFile.work_id == work_id))
    }
    out |= {
        t.rel_path: t.sha256
        for t in db.scalars(select(AudioTrack).where(AudioTrack.work_id == work_id))
    }
    return out


def trash_work(
    db: Session,
    settings: Settings,
    work: Work,
    actor_id: int | None,
    *,
    busy: Callable[[int], bool] = lambda _id: False,
    journal: Journal | None = None,
) -> None:
    if work.trashed_at is not None:
        return
    if busy(work.id):
        raise Busy()
    library = settings.library_dir
    src = library / work.dir_path
    rel = f"{TRASH_DIR}/works/{work.id}-{secrets.token_hex(4)}"
    _move_folder(db, library, work.id, src, library / rel, journal)
    prune_empty_parents(src.parent, library)
    _cancel_jobs(db, work.id)
    work.trash_from, work.dir_path = work.dir_path, rel
    work.trashed_at, work.trashed_by = utcnow(), actor_id
    work.version = (work.version or 1) + 1
    db.flush()


def _move_folder(
    db: Session, library: Path, work_id: int, src: Path, dst: Path, journal: Journal | None
) -> None:
    if src.is_dir():
        move_dir(src, dst, digests=_digests(db, work_id))
        if journal is not None:
            journal.moved_dir(src, dst)
    else:
        dst.mkdir(parents=True, exist_ok=True)
        if journal is not None:
            journal.add(lambda: shutil.rmtree(dst) if inside(library, dst) else None)


def restore_work(
    db: Session, settings: Settings, work: Work, *, journal: Journal | None = None
) -> None:
    if work.trashed_at is None:
        return
    library = settings.library_dir
    author, title = desired_dir_parts(work)
    prior = Path(work.trash_from) if work.trash_from else None
    if prior is not None and len(prior.parts) == 2 and not (library / prior).exists():
        rel = prior.as_posix()
    else:
        rel = free_dir(library, author, title)
    _move_folder(db, library, work.id, library / work.dir_path, library / rel, journal)
    work.dir_path, work.trash_from = rel, None
    work.trashed_at = work.trashed_by = None
    work.version = (work.version or 1) + 1
    db.flush()
    write_book_json(db, library, work)


def purge_work(db: Session, settings: Settings, work: Work) -> None:
    """Delete for good: rows (cascading to positions, shelves, alignment) and the folder."""
    library = settings.library_dir
    folder = library / work.dir_path
    for tf in db.scalars(select(TrashedFile).where(TrashedFile.work_id == work.id)):
        _remove_trashed_file(library, tf)
    db.delete(work)
    db.flush()
    # ``inside`` resolves both sides: a relative or symlinked library must purge too.
    if folder.is_dir() and inside(library / TRASH_DIR, folder):
        shutil.rmtree(folder, ignore_errors=True)
    from lectern.sentence_map import map_path

    map_path(settings.data_dir, work.id).unlink(missing_ok=True)
    _remove_revision_backups(settings, work.id)


# --- single formats ----------------------------------------------------------------------


def _row_of(obj: BookFile | AudioTrack) -> dict:
    cols = obj.__table__.columns.keys()
    out = {}
    for c in cols:
        v = getattr(obj, c)
        out[c] = (
            v.isoformat() if hasattr(v, "isoformat") else (v.value if hasattr(v, "value") else v)
        )
    return out


def trash_file(
    db: Session, settings: Settings, work: Work, obj: BookFile | AudioTrack, actor_id: int | None
) -> TrashedFile:
    library = settings.library_dir
    src = checked(library / work.dir_path, obj.rel_path)
    rel = f"{TRASH_DIR}/files/{secrets.token_hex(6)}/{Path(obj.rel_path).name}"
    if src.is_file():
        move_file(src, library / rel, sha256=obj.sha256)
        prune_empty_parents(src.parent, library / work.dir_path)
    tf = TrashedFile(
        work_id=work.id,
        kind=FileKind.EBOOK if isinstance(obj, BookFile) else FileKind.AUDIO,
        rel_path=obj.rel_path,
        trash_path=rel,
        sha256=obj.sha256,
        size=obj.size,
        row=_row_of(obj),
        trashed_by=actor_id,
    )
    db.add(tf)
    db.delete(obj)
    db.flush()
    return tf


def restore_file(
    db: Session,
    settings: Settings,
    tf: TrashedFile,
    *,
    rel_path: str | None = None,
    journal: Journal | None = None,
) -> BookFile | AudioTrack:
    """Put a removed format back in its work, under its old name when that is free.
    Raises ``LookupError`` (gone, or restored already) and ``FileExistsError`` (an identical
    file is back in the library; restoring would make two rows for one SHA-256)."""
    from datetime import datetime

    from lectern.fsops import unique_name

    work = db.get(Work, tf.work_id)
    if work is None or tf.restored_at is not None:
        raise LookupError("not restorable")
    library = settings.library_dir
    work_dir = library / work.dir_path
    model = BookFile if tf.kind == FileKind.EBOOK else AudioTrack
    if model is BookFile and db.scalar(select(BookFile.id).where(BookFile.sha256 == tf.sha256)):
        raise FileExistsError("an identical file is in the library again")
    if model is AudioTrack and db.scalar(
        select(AudioTrack.id).where(AudioTrack.work_id == work.id, AudioTrack.sha256 == tf.sha256)
    ):
        raise FileExistsError("an identical track is in this book again")
    taken = {r for (r,) in db.execute(select(model.rel_path).where(model.work_id == work.id)).all()}
    rel = unique_name(work_dir, rel_path or tf.rel_path, taken)
    checked(work_dir, rel)
    move_file(library / tf.trash_path, work_dir / rel, sha256=tf.sha256)
    if journal is not None:
        journal.moved_file(library / tf.trash_path, work_dir / rel)
    prune_empty_parents((library / tf.trash_path).parent, library / TRASH_DIR)
    row = dict(tf.row)
    row["rel_path"], row["work_id"] = rel, work.id
    if db.get(model, row.get("id")) is not None:
        row.pop("id", None)
    if isinstance(row.get("added_at"), str):
        row["added_at"] = datetime.fromisoformat(row["added_at"])
    if model is BookFile:
        row["kind"] = FileKind(row["kind"])
    else:
        row["position"] = 900_000 + int(row.get("position") or 0)  # the caller renumbers
    obj = model(**{k: v for k, v in row.items() if k in model.__table__.columns.keys()})
    db.add(obj)
    tf.restored_at = utcnow()
    db.flush()
    return obj


def _remove_trashed_file(library: Path, tf: TrashedFile) -> None:
    p = library / tf.trash_path
    if inside(library / TRASH_DIR, p):
        p.unlink(missing_ok=True)
        prune_empty_parents(p.parent, library / TRASH_DIR)


def _remove_revision_backups(settings: Settings, work_id: int) -> None:
    shutil.rmtree(settings.data_dir / "revisions" / str(work_id), ignore_errors=True)


def purge_expired(db: Session, settings: Settings, now=None) -> dict[str, int]:
    """The background cleanup: works and formats trashed more than 30 days ago, and undo
    history of the same age. Caller holds the library lock."""
    now = now or utcnow()
    cutoff = now - TRASH_RETENTION
    counts = {"works": 0, "files": 0, "revisions": 0}
    for work in db.scalars(select(Work).where(Work.trashed_at < cutoff)).all():
        purge_work(db, settings, work)
        counts["works"] += 1
    for tf in db.scalars(
        select(TrashedFile).where(
            TrashedFile.trashed_at < cutoff, TrashedFile.restored_at.is_(None)
        )
    ).all():
        _remove_trashed_file(settings.library_dir, tf)
        db.delete(tf)
        counts["files"] += 1
    for tf in db.scalars(select(TrashedFile).where(TrashedFile.restored_at < cutoff)).all():
        db.delete(tf)
    for rev in db.scalars(select(WorkRevision).where(WorkRevision.created_at < cutoff)).all():
        for side in ("before", "after"):
            backup = (getattr(rev, side) or {}).get("cover", {}) or {}
            if backup.get("backup"):
                (settings.data_dir / backup["backup"]).unlink(missing_ok=True)
        db.delete(rev)
        counts["revisions"] += 1
    db.flush()
    if any(counts.values()):
        log.info("trash cleanup: %s", counts)
    return counts
