"""Merge one work into another: what confirming an ingest match suggestion does.

Files (EPUBs, audio tracks and their data-saver renditions, the cover if the target has
none) move into the target's directory; shelf memberships follow; the source work is deleted.
Positions: a user's canonical position on the source moves over only if they have none on the
target and it is an audio position (seconds stay meaningful when the target had no audio).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern.ingest.audio import audio_changed, relayout
from lectern.ingest.sentences import ensure_text_sentences
from lectern.jobs import EXT
from lectern.models import (
    AudioTrack,
    BookFile,
    FileKind,
    ProgressSource,
    ShelfItem,
    Work,
    WorkProgress,
)


def _free_name(work_dir: Path, rel: str, sha256: str) -> str:
    if not (work_dir / rel).exists():
        return rel
    p = Path(rel)
    return (p.parent / f"{p.stem} {sha256[:8]}{p.suffix}").as_posix()


def merge_work(db: Session, library: Path, source: Work, target: Work) -> None:
    """Caller commits. Raises ValueError for a merge into itself."""
    if source.id == target.id:
        raise ValueError("cannot merge a work into itself")
    src_dir, dst_dir = library / source.dir_path, library / target.dir_path
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
            shutil.move(str(src_dir / f.rel_path), str(dst_dir / rel))
        f.work_id, f.rel_path = target.id, rel

    moved_audio = False
    for t in db.scalars(select(AudioTrack).where(AudioTrack.work_id == source.id)):
        rel = _free_name(dst_dir, t.rel_path, t.sha256)
        (dst_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        if (src_dir / t.rel_path).exists():
            shutil.move(str(src_dir / t.rel_path), str(dst_dir / rel))
        for ext in EXT.values():
            v = src_dir / "derived" / "audio" / f"{t.sha256}.{ext}"
            if v.exists():
                (dst_dir / "derived" / "audio").mkdir(parents=True, exist_ok=True)
                shutil.move(str(v), str(dst_dir / "derived" / "audio" / v.name))
                side = v.with_name(v.name + ".sha256")
                if side.exists():
                    shutil.move(str(side), str(dst_dir / "derived" / "audio" / side.name))
        t.work_id, t.rel_path, t.position = target.id, rel, t.position + 500_000
        moved_audio = True
    db.flush()
    if moved_audio:
        relayout(db, target.id)
        audio_changed(db, target.id)

    if not target.has_cover and source.has_cover and (src_dir / "cover.jpg").exists():
        shutil.move(str(src_dir / "cover.jpg"), str(dst_dir / "cover.jpg"))
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
    db.flush()
    db.expire(target, ["files"])
    db.expire(source, ["files"])
    db.delete(source)
    db.flush()
    ensure_text_sentences(db, library, target.id)
    if moved_progress:
        from lectern.progress import SentenceCache, audio_timeline, from_audio

        wt, tl = SentenceCache.get(db, target.id), audio_timeline(db, target.id)
        for p in moved_progress:
            pos, _ = from_audio(wt, tl, float(p.raw))
            p.sentence_n, p.sentence_offset = (pos.n, pos.offset) if pos else (0, 0)
    shutil.rmtree(src_dir, ignore_errors=True)
