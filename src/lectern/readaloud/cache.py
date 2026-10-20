"""Inputs, cache, and generation of a work's read-aloud EPUB.

Layout (next to the work, so it travels and is deleted with it)::

    <library>/<Author>/<Title>/book.epub                  original, never modified
    <library>/<Author>/<Title>/derived/read-aloud.epub    generated
    <library>/<Author>/<Title>/derived/read-aloud.json    sidecar: cache key + output facts

The cache key is the generator version plus the fingerprints of every input: the EPUB's
SHA-256, the audio fingerprint, and a SHA-256 over the sentence list. A file is served only if
the sidecar's key equals the current key and the file has the recorded size. Generation writes
to a temporary file in ``derived/``, renames it over the old file (``os.replace``; open readers
keep the old inode), then replaces the sidecar the same way. Nothing in ``derived/`` is ever
rewritten in place.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern.config import Settings
from lectern.ingest.hashing import sha256_file
from lectern.models import (
    Alignment,
    AlignmentStatus,
    AudioTrack,
    BookFile,
    FileKind,
    Sentence,
    Work,
)
from lectern.readaloud.overlay import OverlayError, OverlayTrack, build_read_aloud_epub
from lectern.sentence_map import audio_fingerprint

log = logging.getLogger("lectern.readaloud")

# Bump when the output format changes so every cached file is rebuilt.
GENERATOR_VERSION = 1
DERIVED_DIR = "derived"
OUTPUT_NAME = "read-aloud.epub"
SIDECAR_NAME = "read-aloud.json"


class Unavailable(Exception):
    """The work cannot have a read-aloud EPUB right now. ``code`` is machine-readable."""

    MESSAGES = {
        "no_alignment": "This book has no finished text-to-audio alignment yet.",
        "alignment_stale": "The EPUB or audio changed since the alignment; it needs re-aligning.",
        "no_audio": "This book has no audiobook tracks.",
        "no_ebook": "This book has no EPUB.",
        "missing_file": "A file of this book is missing from the library folder.",
    }

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = self.MESSAGES[code]


@dataclass(frozen=True)
class SentenceRow:
    """Detached copy of a ``sentences`` row, safe to use off the request thread."""

    n: int
    spine_index: int
    char_start: int
    char_end: int
    audio_start_s: float | None
    audio_end_s: float | None
    matched: bool


@dataclass(frozen=True)
class Inputs:
    work_id: int
    title: str
    author: str
    epub_path: Path
    epub_sha256: str
    audio_fingerprint: str
    tracks: tuple[OverlayTrack, ...]
    sentences: tuple[SentenceRow, ...]
    derived: Path
    coverage: float | None

    @property
    def key(self) -> dict:
        digest = hashlib.sha256()
        for s in self.sentences:
            digest.update(
                f"{s.n},{s.spine_index},{s.char_start},{s.char_end},"
                f"{s.audio_start_s!r},{s.audio_end_s!r},{int(s.matched)}\n".encode()
            )
        return {
            "generator": GENERATOR_VERSION,
            "epub_sha256": self.epub_sha256,
            "audio_fingerprint": self.audio_fingerprint,
            "sentences_sha256": digest.hexdigest(),
        }

    @property
    def output(self) -> Path:
        return self.derived / OUTPUT_NAME

    @property
    def sidecar(self) -> Path:
        return self.derived / SIDECAR_NAME


def _inside(root: Path, rel: str) -> Path:
    path = (root / rel).resolve()
    if root not in path.parents:
        raise Unavailable("missing_file")
    return path


def gather_inputs(db: Session, settings: Settings, work: Work) -> Inputs:
    """Everything a build needs, read in one go; raises :class:`Unavailable`."""
    alignment = db.scalar(select(Alignment).where(Alignment.work_id == work.id))
    if alignment is None or alignment.status not in (
        AlignmentStatus.SUCCEEDED,
        AlignmentStatus.STALE,
    ):
        raise Unavailable("no_alignment")
    if alignment.status == AlignmentStatus.STALE:
        raise Unavailable("alignment_stale")
    book = db.scalar(
        select(BookFile).where(
            BookFile.work_id == work.id,
            BookFile.kind == FileKind.EBOOK,
            BookFile.sha256 == alignment.epub_sha256,
        )
    )
    if book is None:
        raise Unavailable("alignment_stale")
    tracks = db.scalars(
        select(AudioTrack).where(AudioTrack.work_id == work.id).order_by(AudioTrack.position)
    ).all()
    if not tracks:
        raise Unavailable("no_audio")
    if audio_fingerprint(t.sha256 for t in tracks) != alignment.audio_fingerprint:
        raise Unavailable("alignment_stale")
    rows = db.scalars(
        select(Sentence).where(Sentence.work_id == work.id).order_by(Sentence.n)
    ).all()
    if not rows:
        raise Unavailable("no_alignment")
    root = settings.library_dir.resolve()
    work_dir = _inside(root, work.dir_path)
    epub = _inside(work_dir, book.rel_path)
    overlay_tracks = []
    for t in tracks:
        path = _inside(work_dir, t.rel_path)
        if not path.is_file():
            raise Unavailable("missing_file")
        overlay_tracks.append(OverlayTrack(path, t.media_type, t.offset_s, t.duration_s))
    if not epub.is_file():
        raise Unavailable("missing_file")
    return Inputs(
        work_id=work.id,
        title=work.title,
        author=work.author,
        epub_path=epub,
        epub_sha256=book.sha256,
        audio_fingerprint=alignment.audio_fingerprint,
        tracks=tuple(overlay_tracks),
        sentences=tuple(
            SentenceRow(
                r.n,
                r.spine_index,
                r.char_start,
                r.char_end,
                r.audio_start_s,
                r.audio_end_s,
                r.matched,
            )
            for r in rows
        ),
        derived=work_dir / DERIVED_DIR,
        coverage=alignment.coverage,
    )


def read_cached(inputs: Inputs) -> dict | None:
    """The sidecar if the cached file matches the current inputs, else ``None``."""
    try:
        sidecar = json.loads(inputs.sidecar.read_text())
        size = inputs.output.stat().st_size
    except (OSError, ValueError):
        return None
    if sidecar.get("key") != inputs.key or sidecar.get("size") != size:
        return None
    return sidecar


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def generate(inputs: Inputs, now: datetime | None = None) -> dict:
    """Build into a temporary file, then publish it and its sidecar by rename."""
    now = now or datetime.now(UTC)
    if sha256_file(inputs.epub_path) != inputs.epub_sha256:
        raise Unavailable("alignment_stale")  # the file on disk is not the aligned one
    inputs.derived.mkdir(parents=True, exist_ok=True)
    tmp = inputs.derived / f".{OUTPUT_NAME}.{secrets.token_hex(6)}.tmp"
    try:
        report = build_read_aloud_epub(
            inputs.epub_path, tmp, inputs.sentences, inputs.tracks, modified=now
        )
        with tmp.open("rb+") as fh:
            os.fsync(fh.fileno())
        sidecar = {
            "key": inputs.key,
            "size": tmp.stat().st_size,
            "sha256": sha256_file(tmp),
            "generated_at": now.isoformat().replace("+00:00", "Z"),
            "sentences": len(inputs.sentences),
            "pars": report.pars,
            "overlays": report.overlays,
            "duration_s": report.duration_s,
            "upgraded_from": report.upgraded_from,
        }
        os.replace(tmp, inputs.output)
    finally:
        tmp.unlink(missing_ok=True)
    _atomic_write_bytes(inputs.sidecar, (json.dumps(sidecar, indent=1) + "\n").encode())
    log.info(
        "read-aloud EPUB for work %s: %s pars, %s bytes",
        inputs.work_id,
        report.pars,
        sidecar["size"],
    )
    return sidecar


@dataclass
class _Build:
    key: dict
    future: Future
    error: str | None = None


class ReadAloudBuilder:
    """Runs builds on one background thread; at most one build per work at a time.

    The state of a failed build is kept in memory (until its inputs change or it is retried),
    so the UI can say why; a restart forgets it and the next request simply tries again.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="readaloud")
        self._lock = threading.Lock()
        self._work_locks: dict[int, threading.Lock] = {}
        self._builds: dict[int, _Build] = {}

    def _work_lock(self, work_id: int) -> threading.Lock:
        with self._lock:
            return self._work_locks.setdefault(work_id, threading.Lock())

    def _run(self, inputs: Inputs) -> dict:
        with self._work_lock(inputs.work_id):
            cached = read_cached(inputs)
            return cached if cached is not None else generate(inputs)

    def status(self, inputs: Inputs) -> tuple[str, dict | None, str | None]:
        """``(state, sidecar, error)`` with state ready, generating, failed, or not_generated."""
        cached = read_cached(inputs)
        if cached is not None:
            return "ready", cached, None
        with self._lock:
            build = self._builds.get(inputs.work_id)
        if build is None or build.key != inputs.key:
            return "not_generated", None, None
        if not build.future.done():
            return "generating", None, None
        exc = build.future.exception()
        if exc is not None:
            return "failed", None, _message(exc)
        return "not_generated", None, None  # finished, but the file is gone again

    def is_generating(self, work_id: int) -> bool:
        """A build is writing into this work's ``derived/`` folder right now (folder moves
        wait for it: ``lectern.editing.reconcile_folder``)."""
        with self._lock:
            build = self._builds.get(work_id)
        return build is not None and not build.future.done()

    def invalidate(self, inputs: Inputs) -> None:
        """Forget the cached file so the next ``start`` rebuilds it (explicit regeneration)."""
        with self._work_lock(inputs.work_id):
            inputs.sidecar.unlink(missing_ok=True)
        with self._lock:
            build = self._builds.get(inputs.work_id)
            if build is not None and build.future.done():
                self._builds.pop(inputs.work_id, None)

    def start(self, inputs: Inputs) -> None:
        """Queue a build unless one for these inputs is running or the file is current."""
        if read_cached(inputs) is not None:
            return
        with self._lock:
            build = self._builds.get(inputs.work_id)
            if build is not None and build.key == inputs.key and not build.future.done():
                return
            self._builds[inputs.work_id] = _Build(
                inputs.key, self._executor.submit(self._run, inputs)
            )

    def ensure(self, inputs: Inputs) -> dict:
        """The current file's sidecar, building it now (on this thread) if necessary."""
        cached = read_cached(inputs)
        if cached is not None:
            return cached
        with self._lock:
            build = self._builds.get(inputs.work_id)
        if build is not None and build.key == inputs.key and not build.future.done():
            build.future.result()  # raises the build's error
        return self._run(inputs)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def _message(exc: BaseException) -> str:
    if isinstance(exc, Unavailable):
        return exc.message
    if isinstance(exc, OverlayError):
        text = str(exc)
        return text[:1].upper() + text[1:]
    log.error("read-aloud build failed", exc_info=exc)
    return "The read-aloud EPUB could not be built; see the server log."
