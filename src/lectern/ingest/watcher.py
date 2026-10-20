"""Inbox watcher: polls the inbox folder and ingests EPUBs once they stop changing."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from lectern.ingest.epub import EpubError, read_epub_metadata
from lectern.ingest.hashing import partial_md5, sha256_file
from lectern.ingest.library import ingest_epub

log = logging.getLogger("lectern.ingest")

STABLE_CYCLES = 2


class InboxWatcher:
    def __init__(
        self, inbox: Path, library: Path, session_factory: sessionmaker, poll_seconds: float
    ) -> None:
        self.inbox = inbox
        self.library = library
        self.session_factory = session_factory
        self.poll = poll_seconds
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

    def _run(self) -> None:
        try:
            n = self.backfill_tints()
            if n:
                log.info("computed cover tints for %d works", n)
        except Exception:  # noqa: BLE001
            log.exception("cover tint backfill failed")
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:  # noqa: BLE001
                log.exception("inbox scan failed")
            self._stop.wait(self.poll)

    def scan_once(self) -> list[Path]:
        """One poll. Returns the paths ingested this round (useful for tests)."""
        ingested: list[Path] = []
        present: set[Path] = set()
        for path in sorted(self.inbox.iterdir()):
            if not path.is_file() or path.name.startswith(".") or path.suffix.lower() != ".epub":
                continue
            present.add(path)
            st = path.stat()
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
        for gone in [p for p in self._seen if p not in present]:
            self._seen.pop(gone, None)
        return ingested

    def ingest_file(self, path: Path) -> bool:
        try:
            meta = read_epub_metadata(path)
        except EpubError as exc:
            log.warning("rejecting %s: %s", path.name, exc)
            self._reject(path)
            return False
        sha = sha256_file(path)
        pmd5 = partial_md5(path)
        with self.session_factory() as db:
            try:
                work, book, created = ingest_epub(db, self.library, path, meta, sha, pmd5)
                db.commit()
            except FileExistsError:
                log.warning("rejecting %s: duplicate of an existing file", path.name)
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

    def _reject(self, path: Path) -> None:
        dest = self.inbox / "_rejected" / path.name
        n = 2
        while dest.exists():
            dest = self.inbox / "_rejected" / f"{path.stem} ({n}){path.suffix}"
            n += 1
        try:
            path.replace(dest)
        except OSError:
            log.exception("could not move %s to _rejected", path)
