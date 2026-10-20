"""Audio tracks of a work: find the files in its directory and keep ``audio_tracks`` current.

A work's audiobook is whatever audio files sit in its library directory (``<library>/<Author>/
<Title>/``), in natural filename order; a disc/chapter numbering in the names gives the play
order. Track metadata (duration, codec, chapters) comes from mutagen; the content hash and the
derived per-work ``audio_fingerprint`` are what alignments and jobs are validated against.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import mutagen
from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern.ingest.hashing import sha256_file
from lectern.ingest.sentences import primary_epub
from lectern.models import AudioTrack, BookFile, Work
from lectern.sentence_map import audio_fingerprint

log = logging.getLogger("lectern.audio")

AUDIO_SUFFIXES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".mp4": "audio/mp4",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
}
_NUM = re.compile(r"(\d+)")
_TEMP_POSITION = 1_000_000


def natural_key(name: str) -> list[object]:
    return [int(p) if p.isdigit() else p.casefold() for p in _NUM.split(name)]


def audio_files(work_dir: Path) -> list[Path]:
    """Audio files anywhere under the work directory, in play order (natural order of the
    relative path, so ``CD1/02`` plays before ``CD2/01``). ``derived/`` (read-aloud EPUBs,
    low-bitrate renditions) and hidden entries are not part of the audiobook."""
    if not work_dir.is_dir():
        return []
    files = []
    for p in work_dir.rglob("*"):
        rel = p.relative_to(work_dir)
        if rel.parts[0] == "derived" or any(part.startswith(".") for part in rel.parts):
            continue
        if p.suffix.lower() in AUDIO_SUFFIXES and p.is_file() and not p.is_symlink():
            files.append(p)
    return sorted(files, key=lambda p: natural_key(p.relative_to(work_dir).as_posix()))


def probe(path: Path) -> tuple[float, str, list[dict] | None]:
    """(duration_s, codec, chapters) via mutagen; a file mutagen cannot read has duration 0."""
    try:
        audio = mutagen.File(path)
    except Exception:  # noqa: BLE001 - mutagen raises many types for damaged files
        audio = None
    if audio is None or audio.info is None:
        return 0.0, path.suffix.lstrip(".").lower() or "unknown", None
    duration = float(getattr(audio.info, "length", 0.0) or 0.0)
    kind = type(audio).__name__.lower()
    codec = {
        "mp3": "mp3",
        "easymp3": "mp3",
        "mp4": "aac",
        "easymp4": "aac",
        "oggvorbis": "vorbis",
        "oggopus": "opus",
        "flac": "flac",
    }.get(kind, kind)
    if codec == "aac" and "alac" in str(getattr(audio.info, "codec", "")).lower():
        codec = "alac"
    chapters = None
    raw_chapters = getattr(audio, "chapters", None)
    if raw_chapters:
        try:
            chapters = [
                {"title": str(c.title or "")[:200], "start_s": round(float(c.start), 3)}
                for c in raw_chapters
            ]
        except (AttributeError, TypeError, ValueError):
            chapters = None
    return duration, codec, chapters


def work_tracks(db: Session, work_id: int) -> list[AudioTrack]:
    return list(
        db.scalars(
            select(AudioTrack).where(AudioTrack.work_id == work_id).order_by(AudioTrack.position)
        )
    )


def fingerprint_of(tracks: list[AudioTrack]) -> str:
    return audio_fingerprint(t.sha256 for t in tracks)


def sync_tracks(db: Session, library: Path, work: Work) -> bool:
    """Make ``audio_tracks`` match the files on disk. Returns True when anything changed.

    Unchanged files (same relative path and size) keep their row and hash; everything else is
    re-probed and re-hashed. Positions and offsets are recomputed from the resulting order.
    The caller commits (and calls ``lectern.align.jobs.refresh_work`` when this returns True).
    """
    work_dir = library / work.dir_path
    existing = {t.rel_path: t for t in work_tracks(db, work.id)}
    rows: list[AudioTrack] = []
    seen: set[str] = set()
    changed = False
    for path in audio_files(work_dir):
        rel = path.relative_to(work_dir).as_posix()
        size = path.stat().st_size
        row = existing.pop(rel, None)
        if row is not None and row.size == size and row.sha256 not in seen:
            rows.append(row)
            seen.add(row.sha256)
            continue
        sha = sha256_file(path)
        if sha in seen:  # a copy of an earlier track; one work cannot play it twice
            log.warning("work %d: %s duplicates an earlier track, skipped", work.id, rel)
            if row is not None:
                db.delete(row)
                changed = True
            continue
        seen.add(sha)
        duration, codec, chapters = probe(path)
        if row is None:
            row = AudioTrack(
                work_id=work.id,
                position=_TEMP_POSITION + len(rows),
                rel_path=rel,
                media_type=AUDIO_SUFFIXES[path.suffix.lower()],
                codec=codec,
                duration_s=duration,
                offset_s=0.0,
                size=size,
                sha256=sha,
                chapters=chapters,
            )
            db.add(row)
        else:
            row.codec, row.duration_s, row.size, row.sha256, row.chapters = (
                codec,
                duration,
                size,
                sha,
                chapters,
            )
        rows.append(row)
        changed = True
    for gone in existing.values():
        db.delete(gone)
        changed = True
    if changed or any(t.position != i for i, t in enumerate(rows)):
        # Two passes so the (work_id, position) unique constraint never trips mid-renumber;
        # temporary positions sit above any real one (the schema forbids negatives).
        db.flush()
        for i, t in enumerate(rows):
            t.position = _TEMP_POSITION + i
        db.flush()
        offset = 0.0
        for i, t in enumerate(rows):
            t.position = i
            t.offset_s = round(offset, 3)
            offset += t.duration_s
        db.flush()
        changed = True
    return changed


def work_epub(db: Session, library: Path, work: Work) -> tuple[BookFile, Path] | None:
    """The work's primary EPUB and its path, if present on disk."""
    book = primary_epub(db, work.id)  # the same edition the reader opens
    if book is None:
        return None
    path = (library / work.dir_path / book.rel_path).resolve()
    root = library.resolve()
    if root not in path.parents or not path.is_file():
        return None
    return book, path
