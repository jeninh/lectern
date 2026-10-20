"""Merge one work into another: what confirming an ingest match suggestion does.

Files (EPUBs, audio tracks and their data-saver renditions, the cover if the target has
none) move into the target's directory; shelf memberships follow; the source work is deleted.
Positions: a user's canonical position on the source moves over only if they have none on the
target and it is an audio position (seconds stay meaningful when the target had no audio).
Formats the source had removed (still in the trash) now belong to the target, so they stay
restorable; the source's edit history goes with it (its revisions describe a work that no
longer exists).

Every file move is journalled; nothing is deleted before the caller commits. The caller runs
the returned ``finish`` after a successful commit to remove the emptied source folder, the
source's revision backups, and its sentence map.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern.config import Settings
from lectern.fsops import move_file
from lectern.ingest.audio import audio_changed, relayout
from lectern.ingest.sentences import ensure_text_sentences
from lectern.jobs import EXT
from lectern.models import (
    AudioTrack,
    BookFile,
    FileKind,
    ProgressSource,
    ShelfItem,
    TrashedFile,
    Work,
    WorkProgress,
    WorkRevision,
)

if TYPE_CHECKING:
    from lectern.editing import Journal


def _free_name(work_dir: Path, rel: str, sha256: str) -> str:
    if not (work_dir / rel).exists():
        return rel
    p = Path(rel)
    return (p.parent / f"{p.stem} {sha256[:8]}{p.suffix}").as_posix()


def merge_work(
    db: Session, settings: Settings, source: Work, target: Work, journal: Journal
) -> Callable[[], None]:
    """Caller holds the library lock, commits, rolls ``journal`` back on failure, and calls
    the returned ``finish`` once committed. Raises ValueError for a merge into itself."""
    if source.id == target.id:
        raise ValueError("cannot merge a work into itself")
    library = settings.library_dir
    src_dir, dst_dir = library / source.dir_path, library / target.dir_path

    def move(src: Path, dst: Path, sha256: str | None = None) -> None:
        move_file(src, dst, sha256=sha256)
        journal.moved_file(src, dst)

    dst_dir.mkdir(parents=True, exist_ok=True)
    target_had_audio = (
        db.scalar(select(AudioTrack.id).where(AudioTrack.work_id == target.id).limit(1)) is not None
    )
    has_ebook = any(f.kind == FileKind.EBOOK for f in target.files)

    for f in db.scalars(select(BookFile).where(BookFile.work_id == source.id)):
        rel = f.rel_path
        if f.kind == FileKind.EBOOK and has_ebook:
            rel = f"book-{f.sha256[:8]}.epub"
        rel = _free_name(dst_dir, rel, f.sha256)
        if (src_dir / f.rel_path).exists():
            move(src_dir / f.rel_path, dst_dir / rel, f.sha256)
        f.work_id, f.rel_path = target.id, rel

    moved_audio = False
    for t in db.scalars(select(AudioTrack).where(AudioTrack.work_id == source.id)):
        rel = _free_name(dst_dir, t.rel_path, t.sha256)
        if (src_dir / t.rel_path).exists():
            move(src_dir / t.rel_path, dst_dir / rel, t.sha256)
        for ext in EXT.values():
            v = src_dir / "derived" / "audio" / f"{t.sha256}.{ext}"
            out = dst_dir / "derived" / "audio" / v.name
            if v.exists() and not out.exists():
                move(v, out)
                side = v.with_name(v.name + ".sha256")
                if side.exists() and not out.with_name(side.name).exists():
                    move(side, out.with_name(side.name))
        t.work_id, t.rel_path, t.position = target.id, rel, t.position + 500_000
        moved_audio = True
    db.flush()
    if moved_audio:
        relayout(db, target.id)
        audio_changed(db, target.id)

    if not target.has_cover and source.has_cover and (src_dir / "cover.jpg").exists():
        move(src_dir / "cover.jpg", dst_dir / "cover.jpg")
        target.has_cover, target.cover_tint = True, source.cover_tint
    if target.isbn is None and source.isbn:
        isbn, source.isbn = source.isbn, None
        db.flush()
        target.isbn = isbn

    on_target = {
        r.user_id for r in db.scalars(select(WorkProgress).where(WorkProgress.work_id == target.id))
    }
    moved_progress = []
    for p in db.scalars(select(WorkProgress).where(WorkProgress.work_id == source.id)):
        if p.user_id not in on_target and p.source == ProgressSource.AUDIO and not target_had_audio:
            p.work_id = target.id
            moved_progress.append(p)

    shelves = {
        s for s in db.scalars(select(ShelfItem.shelf_id).where(ShelfItem.work_id == target.id))
    }
    for item in db.scalars(select(ShelfItem).where(ShelfItem.work_id == source.id)):
        if item.shelf_id not in shelves:
            db.add(ShelfItem(shelf_id=item.shelf_id, work_id=target.id, position=item.position))
    for tf in db.scalars(select(TrashedFile).where(TrashedFile.work_id == source.id)):
        tf.work_id = target.id  # files stay where they are in _trash/files/
    for rev in db.scalars(select(WorkRevision).where(WorkRevision.work_id == source.id)):
        db.delete(rev)
    db.flush()
    db.expire(target, ["files"])
    db.expire(source, ["files"])
    source_id = source.id
    db.delete(source)
    db.flush()
    ensure_text_sentences(db, library, target.id)
    if moved_progress:
        from lectern.progress import SentenceCache, audio_timeline, from_audio

        wt, tl = SentenceCache.get(db, target.id), audio_timeline(db, target.id)
        for p in moved_progress:
            pos, _ = from_audio(wt, tl, float(p.raw))
            p.sentence_n, p.sentence_offset = (pos.n, pos.offset) if pos else (0, 0)

    def finish() -> None:
        from lectern.fsops import inside
        from lectern.sentence_map import map_path
        from lectern.trash import _remove_revision_backups

        if inside(library, src_dir):
            shutil.rmtree(src_dir, ignore_errors=True)
        _remove_revision_backups(settings, source_id)
        map_path(settings.data_dir, source_id).unlink(missing_ok=True)

    return finish
