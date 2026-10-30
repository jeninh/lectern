"""Audiobook files: read tags, chapters, codec and duration, and place tracks on a work.

Metadata comes from mutagen (tags, cover art, ID3/MP4 chapters, duration) and, when
``ffprobe`` is installed, from ffprobe (exact duration, codec, QuickTime and Nero chapters).
Either alone is enough; ffprobe wins where both answer.

Every audio file of a work is one ``audio_tracks`` row. Tracks are ordered by disc, track
number, and file name (natural order), and laid end to end on the work's single timeline:
``offset_s`` is the sum of the earlier durations. A multi-file MP3 book therefore becomes one
timeline however its files arrive (one folder, loose files, or one browser upload per file).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import mutagen
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lectern.ingest.cover import dominant_tint
from lectern.ingest.epub import normalize_isbn
from lectern.ingest.library import new_work, safe_component
from lectern.ingest.matching import MatchResult, clean_title, match_work
from lectern.models import (
    JOB_ACTIVE_STATUSES,
    Alignment,
    AlignmentStatus,
    AudioTrack,
    Job,
    MatchSuggestion,
    Work,
    utcnow,
)

log = logging.getLogger("lectern.ingest")

AUDIO_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4b": "audio/mp4",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
}
_CODECS = {"mp4a": "aac", "mp4a.40.2": "aac", "mp4a.40.5": "aac", "alac": "alac"}


class AudioError(ValueError):
    pass


@dataclass
class AudioInfo:
    title: str | None = None
    album: str | None = None
    artist: str | None = None
    album_artist: str | None = None
    isbn: str | None = None
    track_no: int | None = None
    disc_no: int | None = None
    year: str | None = None
    duration_s: float = 0.0
    codec: str = "unknown"
    media_type: str = "application/octet-stream"
    chapters: list[dict] = field(default_factory=list)
    cover: bytes | None = None
    # Review-before-filing hints (migration 0007); none of them is required.
    narrator: str | None = None
    series: str | None = None
    series_index: float | None = None
    language: str | None = None
    description: str | None = None
    composer: str | None = None

    @property
    def book_title(self) -> str | None:
        """For multi-file books the album names the book; a single file is titled by itself."""
        return clean_title(self.album or self.title or "") or None

    @property
    def author(self) -> str | None:
        return self.album_artist or self.artist


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_TYPES


def _first(tags, *keys) -> str | None:
    for k in keys:
        try:
            v = tags.get(k) if tags is not None else None
        except (KeyError, ValueError):
            v = None
        if v:
            v = v[0] if isinstance(v, list) else v
            v = str(v).strip()
            if v:
                return v
    return None


def _number(v: str | None) -> int | None:
    if not v:
        return None
    m = re.match(r"\s*(\d+)", v)
    return int(m.group(1)) if m else None


def _find_isbn(*values: str | None) -> str | None:
    for v in values:
        if not v:
            continue
        for m in re.finditer(r"(?:97[89][- ]?)?(?:\d[- ]?){9}[\dXx]", v):
            isbn = normalize_isbn(m.group(0))
            if isbn:
                return isbn
    return None


def _mutagen_info(path: Path, info: AudioInfo) -> None:
    try:
        easy = mutagen.File(path, easy=True)
    except mutagen.MutagenError as exc:
        raise AudioError(f"unreadable audio: {exc}") from exc
    if easy is None:
        raise AudioError("not an audio file")
    tags = easy.tags
    info.title = _first(tags, "title")
    info.album = _first(tags, "album")
    info.artist = _first(tags, "artist")
    info.album_artist = _first(tags, "albumartist")
    info.track_no = _number(_first(tags, "tracknumber"))
    info.disc_no = _number(_first(tags, "discnumber"))
    info.year = _first(tags, "date")
    info.duration_s = float(getattr(easy.info, "length", 0.0) or 0.0)
    name = type(easy).__name__.lower()
    codec = getattr(easy.info, "codec", None)
    info.codec = (
        _CODECS.get(codec, codec)
        if codec
        else "mp3"
        if "mp3" in name
        else "opus"
        if "opus" in name
        else "vorbis"
        if "vorbis" in name
        else "flac"
        if "flac" in name
        else info.codec
    )

    raw = mutagen.File(path)  # full tags: ISBN frames, cover art, chapters
    rtags = getattr(raw, "tags", None)
    texts: list[str] = []
    if rtags is not None:
        for key in list(rtags.keys()):
            k = str(key).lower()
            if any(w in k for w in ("isbn", "comment", "comm", "©cmt", "desc", "ldes")):
                v = rtags[key]
                texts.append(str(v.text if hasattr(v, "text") else v))
    info.isbn = _find_isbn(*texts)
    info.composer = _first(tags, "composer")
    info.language = (_first(tags, "language") or "")[:16] or None
    info.description = _first(tags, "description")
    _extra_tags(rtags, info)
    info.cover = _cover_bytes(raw)
    chapters = _mutagen_chapters(raw)
    if chapters:
        info.chapters = chapters


_NARRATOR_KEYS = (
    "narrator",
    "©nrt",
    "----:com.apple.itunes:narrator",
    "txxx:narrator",
    "txxx:narratedby",
)
_SERIES_KEYS = (
    "series",
    "©mvn",
    "mvnm",
    "----:com.apple.itunes:series",
    "txxx:series",
    "txxx:mvnm",
)
_PART_KEYS = ("series-part", "series_part", "©mvi", "mvin", "----:com.apple.itunes:series-part",
              "txxx:series-part", "txxx:mvin")  # fmt: skip


def _tag_text(v) -> str:
    v = getattr(v, "text", v)
    if isinstance(v, list):
        v = v[0] if v else ""
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    if isinstance(v, tuple):  # MP4 movement index is (n, total)
        v = v[0]
    return str(v).strip()


def _extra_tags(rtags, info: AudioInfo) -> None:
    """Narrator and series from the tag spellings audiobook tools use (ID3 TXXX, iTunes
    freeform atoms, Audible's ``©nrt`` and movement atoms)."""
    if rtags is None:
        return
    found: dict[str, str] = {}
    try:
        items = list(rtags.items())
    except Exception:  # noqa: BLE001 - some tag containers do not iterate cleanly
        return
    for key, value in items:
        k = str(key).lower()
        text = _tag_text(value)
        if text and k not in found:
            found[k] = text
    narrator = next((found[k] for k in _NARRATOR_KEYS if k in found), None)
    info.narrator = narrator[:128] if narrator else None
    series = next((found[k] for k in _SERIES_KEYS if k in found), None)
    info.series = series[:256] if series else None
    part = next((found[k] for k in _PART_KEYS if k in found), None)
    if part:
        try:
            info.series_index = float(part.split("/")[0])
        except ValueError:
            info.series_index = None


def _cover_bytes(raw) -> bytes | None:
    tags = getattr(raw, "tags", None)
    try:
        if tags is not None and hasattr(tags, "getall"):  # ID3
            pics = tags.getall("APIC")
            if pics:
                return bytes(pics[0].data)
        if tags is not None and "covr" in tags:  # MP4
            return bytes(tags["covr"][0])
        pictures = getattr(raw, "pictures", None)  # FLAC
        if pictures:
            return bytes(pictures[0].data)
    except (KeyError, IndexError, TypeError):
        return None
    return None


def _mutagen_chapters(raw) -> list[dict]:
    tags = getattr(raw, "tags", None)
    out: list[dict] = []
    if tags is not None and hasattr(tags, "getall"):
        for ch in sorted(tags.getall("CHAP"), key=lambda c: c.start_time):
            title = ""
            sub = getattr(ch, "sub_frames", None)
            if sub is not None and "TIT2" in sub:
                title = str(sub["TIT2"].text[0])
            out.append(
                {"title": title or f"Chapter {len(out) + 1}", "start_s": ch.start_time / 1000}
            )
    mp4_chapters = getattr(raw, "chapters", None)
    if not out and mp4_chapters:
        for ch in mp4_chapters:
            out.append({"title": ch.title or f"Chapter {len(out) + 1}", "start_s": float(ch.start)})
    return out


def _ffprobe(path: Path, ffprobe: str) -> dict | None:
    if shutil.which(ffprobe) is None:
        return None
    try:
        res = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                "-show_chapters",
                str(path),
            ],  # fmt: skip
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout)
    except ValueError:
        return None


def probe(path: Path, ffprobe: str = "ffprobe") -> AudioInfo:
    info = AudioInfo(media_type=AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream"))
    _mutagen_info(path, info)
    data = _ffprobe(path, ffprobe)
    if data:
        audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
        if audio is None:
            raise AudioError("no audio stream")
        info.codec = audio.get("codec_name") or info.codec
        try:
            info.duration_s = float(data.get("format", {}).get("duration") or info.duration_s)
        except ValueError:
            pass
        chapters = [
            {
                "title": (c.get("tags") or {}).get("title") or f"Chapter {i + 1}",
                "start_s": round(float(c.get("start_time", 0.0)), 3),
            }
            for i, c in enumerate(data.get("chapters") or [])
        ]
        if chapters:
            info.chapters = chapters
        ftags = {
            str(k).lower(): str(v) for k, v in (data.get("format", {}).get("tags") or {}).items()
        }
        info.narrator = info.narrator or (ftags.get("narrator") or "")[:128] or None
        info.series = info.series or (ftags.get("series") or ftags.get("mvnm") or "")[:256] or None
        if info.series_index is None:
            part = ftags.get("series-part") or ftags.get("mvin")
            try:
                info.series_index = float(part.split("/")[0]) if part else None
            except ValueError:
                info.series_index = None
    if info.duration_s <= 0:
        raise AudioError("could not determine duration")
    info.chapters = [
        c for c in info.chapters if 0 <= c["start_s"] < info.duration_s
    ] or info.chapters[:0]
    return info


# --- placing a file on a work -------------------------------------------------------------


_NATURAL = re.compile(r"(\d+)")


def _natural(s: str) -> list:
    return [int(p) if p.isdigit() else p.lower() for p in _NATURAL.split(s)]


def track_rel_path(info: AudioInfo, source: Path) -> str:
    """``audio/D01-T003 Title.mp3``: sorting rel_paths naturally reproduces play order."""
    name = safe_component(info.title or source.stem, "Track")[:80]
    disc = info.disc_no or 1
    track = info.track_no or 0
    return f"audio/D{disc:02d}-T{track:03d} {name}{source.suffix.lower()}"


@dataclass
class AudioIngest:
    work: Work
    track: AudioTrack
    created: bool
    match: MatchResult


def ingest_audio(
    db: Session,
    library: Path,
    source: Path,
    info: AudioInfo,
    sha256: str,
) -> AudioIngest:
    """Move ``source`` into the library as a track of the matching (or a new) work."""
    if db.scalar(select(AudioTrack.id).where(AudioTrack.sha256 == sha256)) is not None:
        raise FileExistsError("identical audio file already in library")
    title = info.book_title or clean_title(source.stem) or "Untitled"
    authors = [info.author] if info.author else []
    match = match_work(db, title, authors, info.isbn, prefer_audio=True)
    created = False
    if match.certain:
        work = match.work
        assert work is not None
        work.version = (work.version or 1) + 1  # an open editor must not overwrite this
        if work.isbn is None and info.isbn:
            work.isbn = info.isbn
    else:
        work = new_work(
            db,
            library,
            title=title,
            author=info.author or "Unknown Author",
            isbn=info.isbn,
            published=info.year,
        )
        work.series = info.series
        work.series_index = info.series_index if info.series else None
        work.narrators = [info.narrator] if info.narrator else []
        created = True
        if match.work is not None:
            db.add(
                MatchSuggestion(
                    work_id=work.id,
                    candidate_work_id=match.work.id,
                    score=match.score,
                    reason=match.reason,
                )
            )

    existing = db.scalars(select(AudioTrack).where(AudioTrack.work_id == work.id)).all()
    if existing and (len(info.chapters) > 1 or any(len(t.chapters or []) > 1 for t in existing)):
        raise FileExistsError(f"{work.title!r} already has an audiobook")
    rel = track_rel_path(info, source)
    slot = rel.split(" ", 1)[0]  # "audio/D01-T003"
    if info.track_no and any(t.rel_path.split(" ", 1)[0] == slot for t in existing):
        raise FileExistsError(f"{work.title!r} already has track {info.track_no}")
    work_dir = library / work.dir_path
    dest = work_dir / rel
    if dest.exists() or any(t.rel_path == rel for t in existing):
        rel = f"{rel.rsplit('.', 1)[0]} {sha256[:8]}{source.suffix.lower()}"
        dest = work_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))

    if not work.has_cover and info.cover:
        if _save_cover(info.cover, work_dir / "cover.jpg"):
            work.has_cover = True
            work.cover_tint = dominant_tint(work_dir / "cover.jpg")

    chapters = info.chapters or [{"title": info.title or source.stem, "start_s": 0.0}]
    track = AudioTrack(
        work_id=work.id,
        position=100_000 + len(existing),  # renumbered below
        rel_path=rel,
        media_type=info.media_type,
        codec=info.codec[:32],
        duration_s=info.duration_s,
        offset_s=0.0,
        size=dest.stat().st_size,
        sha256=sha256,
        chapters=chapters,
    )
    db.add(track)
    db.flush()
    relayout(db, work.id)
    audio_changed(db, work.id)
    work.updated_at = utcnow()
    return AudioIngest(work, track, created, match)


def _save_cover(data: bytes, dest: Path) -> bool:
    """Attached artwork as the work's cover; a broken or oversized picture means no cover."""
    from lectern.ingest.cover import EMBEDDED_FORMATS, CoverRejected, save_cover_jpeg

    try:
        save_cover_jpeg(data, dest, formats=EMBEDDED_FORMATS)
        return True
    except (CoverRejected, OSError):
        return False


def relayout(db: Session, work_id: int) -> None:
    """Renumber a work's tracks in natural ``rel_path`` order and recompute offsets."""
    db.flush()
    tracks = db.scalars(select(AudioTrack).where(AudioTrack.work_id == work_id)).all()
    ordered = sorted(tracks, key=lambda t: _natural(t.rel_path))
    # Two passes so the (work_id, position) unique constraint never sees a duplicate.
    db.execute(
        update(AudioTrack)
        .where(AudioTrack.work_id == work_id)
        .values(position=AudioTrack.position + 1_000_000)
    )
    db.flush()
    for t in ordered:
        db.expire(t, ["position"])
    offset = 0.0
    for i, t in enumerate(ordered):
        t.position = i
        t.offset_s = round(offset, 3)
        offset += t.duration_s
    db.flush()


def audio_changed(db: Session, work_id: int) -> None:
    """The work's audio changed: an alignment computed from the old tracks is stale and any
    in-flight align job is cancelled (docs/contracts.md, staleness)."""
    from lectern.align import jobs as align_jobs

    for job in db.scalars(
        select(Job).where(
            Job.work_id == work_id, Job.kind == "align", Job.status.in_(JOB_ACTIVE_STATUSES)
        )
    ):
        # Conditional on the row: a worker that succeeded while the audio was changing keeps
        # its completed job and cleared lease. Its now-stale alignment is handled below.
        align_jobs.cancel_job(db, job)
    al = db.scalar(select(Alignment).where(Alignment.work_id == work_id))
    if al is not None:
        db.refresh(al)  # a worker may have committed its result after this session read it
        if al.status == AlignmentStatus.SUCCEEDED:
            al.status = AlignmentStatus.STALE
