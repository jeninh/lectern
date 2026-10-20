"""Inbox watcher: polls the inbox folder and ingests EPUBs and audio once they stop changing.

Files may sit directly in the inbox or in sub-folders (an audiobook folder of MP3s, copied in
one go). Every supported file is ingested on its own once its size and mtime have been stable
for a poll; audio files of one book find each other through their tags, so a folder, loose
files, or one browser upload per file all end up on the same work timeline. Emptied
sub-folders are removed. Names starting with ``.`` or ``_`` are skipped (``_rejected``,
in-progress copies).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from lectern.fsops import LIBRARY_LOCK
from lectern.ingest.audio import AudioError, ingest_audio, is_audio, probe
from lectern.ingest.epub import EpubError, read_epub_metadata
from lectern.ingest.hashing import partial_md5, sha256_file
from lectern.ingest.library import ingest_epub
from lectern.ingest.sentences import ensure_text_sentences
from lectern.workmeta import write_book_json

log = logging.getLogger("lectern.ingest")

STABLE_CYCLES = 2


class InboxWatcher:
    def __init__(
        self,
        inbox: Path,
        library: Path,
        session_factory: sessionmaker,
        poll_seconds: float,
        settings=None,
    ) -> None:
        self.inbox = inbox
        self.library = library
        self.session_factory = session_factory
        self.poll = poll_seconds
        self.settings = settings
        self._seen: dict[Path, tuple[int, float, int]] = {}  # size, mtime, stable_count
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.inbox.mkdir(parents=True, exist_ok=True)
        (self.inbox / "_rejected").mkdir(exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="lectern-inbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def backfill_tints(self) -> int:
        """Compute cover tints for works ingested before tints existed. Returns count updated."""
        from sqlalchemy import select

        from lectern.ingest.cover import dominant_tint
        from lectern.models import Work

        updated = 0
        with self.session_factory() as db:
            for work in db.scalars(
                select(Work).where(Work.has_cover.is_(True), Work.cover_tint.is_(None))
            ):
                tint = dominant_tint(self.library / work.dir_path / "cover.jpg")
                if tint:
                    work.cover_tint = tint
                    updated += 1
            db.commit()
        return updated

    def backfill_sentences(self) -> int:
        """Text-only sentence lists for works ingested before milestone 2. Returns works done."""
        from sqlalchemy import select

        from lectern.models import BookFile, FileKind, Sentence

        done = 0
        with self.session_factory() as db:
            ids = db.scalars(
                select(BookFile.work_id)
                .where(BookFile.kind == FileKind.EBOOK)
                .where(~BookFile.work_id.in_(select(Sentence.work_id)))
                .distinct()
            ).all()
            for work_id in ids:
                if self._stop.is_set():
                    break
                if ensure_text_sentences(db, self.library, work_id):
                    done += 1
                db.commit()
        return done

    def _run(self) -> None:
        for name, job in (
            ("cover tint", self.backfill_tints),
            ("sentence", self.backfill_sentences),
        ):
            try:
                n = job()
                if n:
                    log.info("%s backfill: %d works", name, n)
            except Exception:  # noqa: BLE001
                log.exception("%s backfill failed", name)
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:  # noqa: BLE001
                log.exception("inbox scan failed")
            self._stop.wait(self.poll)

    def _candidates(self) -> list[Path]:
        out: list[Path] = []
        stack = [self.inbox]
        while stack:
            d = stack.pop()
            try:
                entries = sorted(d.iterdir())
            except OSError:
                continue
            for path in entries:
                if path.name.startswith((".", "_")):
                    continue
                if path.is_dir():
                    stack.append(path)
                elif path.is_file() and (path.suffix.lower() == ".epub" or is_audio(path)):
                    out.append(path)
        return sorted(out)

    def scan_once(self) -> list[Path]:
        """One poll. Returns the paths ingested this round (useful for tests)."""
        ingested: list[Path] = []
        present: set[Path] = set()
        for path in self._candidates():
            present.add(path)
            try:
                st = path.stat()
            except OSError:
                continue
            size, mtime, stable = self._seen.get(path, (-1, -1.0, 0))
            if (st.st_size, st.st_mtime) == (size, mtime):
                stable += 1
            else:
                stable = 0
            self._seen[path] = (st.st_size, st.st_mtime, stable)
            if stable >= STABLE_CYCLES - 1:
                if self.ingest_file(path):
                    ingested.append(path)
                self._seen.pop(path, None)
                self._prune_dirs(path.parent)
        for gone in [p for p in self._seen if p not in present]:
            self._seen.pop(gone, None)
        return ingested

    def _prune_dirs(self, d: Path) -> None:
        while d != self.inbox and self.inbox in d.parents:
            try:
                d.rmdir()  # only succeeds when empty
            except OSError:
                return
            d = d.parent

    def ingest_file(self, path: Path) -> bool:
        if is_audio(path):
            return self._ingest_audio(path)
        try:
            meta = read_epub_metadata(path)
        except EpubError as exc:
            log.warning("rejecting %s: %s", path.name, exc)
            self._reject(path)
            return False
        sha = sha256_file(path)
        pmd5 = partial_md5(path)
        with LIBRARY_LOCK, self.session_factory() as db:
            try:
                work, book, created = ingest_epub(db, self.library, path, meta, sha, pmd5)
                db.flush()
                ensure_text_sentences(db, self.library, work.id)
                write_book_json(db, self.library, work)
                db.commit()
            except FileExistsError:
                log.warning("rejecting %s: duplicate of an existing file", path.name)
                db.rollback()
                self._reject(path)
                return False
            except Exception:
                db.rollback()
                raise
        log.info(
            "ingested %s -> %s (%s work #%d, pmd5=%s)",
            path.name,
            work.dir_path,
            "new" if created else "existing",
            work.id,
            pmd5,
        )
        return True

    def _ingest_audio(self, path: Path) -> bool:
        ffprobe = self.settings.ffprobe_path if self.settings else "ffprobe"
        try:
            info = probe(path, ffprobe)
        except AudioError as exc:
            log.warning("rejecting %s: %s", path.name, exc)
            self._reject(path)
            return False
        sha = sha256_file(path)
        with LIBRARY_LOCK, self.session_factory() as db:
            try:
                result = ingest_audio(db, self.library, path, info, sha)
                write_book_json(db, self.library, result.work)
                db.commit()
            except FileExistsError as exc:
                log.warning("rejecting %s: %s", path.name, exc)
                db.rollback()
                self._reject(path)
                return False
            except Exception:
                db.rollback()
                raise
            if self.settings is not None and self.settings.audio_low_auto:
                from lectern.jobs import enqueue_transcode

                enqueue_transcode(db, self.settings, result.work)
                db.commit()
        log.info(
            "ingested audio %s -> %s/%s (%s work #%d%s)",
            path.name,
            result.work.dir_path,
            result.track.rel_path,
            "new" if result.created else "existing",
            result.work.id,
            f", maybe the same as #{result.match.work.id}"
            if result.match.work and not result.match.certain
            else "",
        )
        return True

    def _reject(self, path: Path) -> None:
        dest = self.inbox / "_rejected" / path.name
        n = 2
        while dest.exists():
            dest = self.inbox / "_rejected" / f"{path.stem} ({n}){path.suffix}"
            n += 1
        try:
            dest.parent.mkdir(exist_ok=True)
            path.replace(dest)
        except OSError:
            log.exception("could not move %s to _rejected", path)
        self._prune_dirs(path.parent)
