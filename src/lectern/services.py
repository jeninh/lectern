"""Background housekeeping for alignment: lease sweeps, track rescans, the local worker.

Started from the app lifespan when background work is enabled. All of it is best effort and
restartable: a crashed sweep is retried on the next tick, and a work whose files changed is
re-queued the next time it is seen (``lectern.align.jobs.refresh_work``).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from lectern.align import jobs
from lectern.align.worker import LocalWorker
from lectern.config import Settings
from lectern.fsops import LIBRARY_LOCK
from lectern.models import Work

log = logging.getLogger("lectern.services")


def rescan_works(session_factory: sessionmaker, library: Path) -> int:
    """Sync every work's tracks with disk and queue alignments; returns jobs queued."""
    queued = 0
    with session_factory() as db:
        ids = list(db.scalars(select(Work.id).order_by(Work.id)))
    for work_id in ids:
        with LIBRARY_LOCK, session_factory() as db:
            work = db.get(Work, work_id)
            if work is None or work.trashed_at is not None:
                continue
            try:
                if jobs.refresh_work(db, library, work) is not None:
                    queued += 1
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
                log.exception("rescan of work %d failed", work_id)
    return queued


class Housekeeping:
    """One thread: reclaim expired leases every ``job_sweep_seconds`` and rescan the library
    for audio every ``track_rescan_seconds`` (and once at start)."""

    def __init__(self, session_factory: sessionmaker, settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lectern-housekeeping", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        next_rescan = next_sweep = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_rescan:
                next_rescan = now + self.settings.track_rescan_seconds
                try:
                    n = rescan_works(self.session_factory, self.settings.library_dir)
                    if n:
                        log.info("queued %d alignment job(s)", n)
                except Exception:  # noqa: BLE001
                    log.exception("library rescan failed")
            if now >= next_sweep:
                next_sweep = now + self.settings.job_sweep_seconds
                try:
                    with self.session_factory() as db:
                        n = jobs.reclaim_expired(db, stall_s=self.settings.align_stall_seconds)
                    if n:
                        log.warning("reclaimed %d expired lease(s)", n)
                except Exception:  # noqa: BLE001
                    log.exception("lease sweep failed")
            self._stop.wait(max(0.1, min(next_rescan, next_sweep) - time.monotonic()))


def start_services(session_factory: sessionmaker, settings: Settings) -> list:
    """Housekeeping always; the in-process worker only when faster-whisper is importable."""
    from lectern.align.transcribe import Transcriber

    with session_factory() as db:
        # The in-process worker of the previous server process is gone (a restart, a crash,
        # the OOM killer): its lease would otherwise hold the job for up to the lease length.
        if n := jobs.reclaim_expired(db, orphaned_local=True):
            log.warning("requeued %d alignment job(s) interrupted by a restart", n)
    started: list = [Housekeeping(session_factory, settings)]
    missing = jobs.local_engine_missing(settings)
    if missing is None:
        started.append(
            LocalWorker(
                session_factory,
                data_dir=settings.data_dir,
                library=settings.library_dir,
                transcriber=Transcriber(
                    settings.align_model,
                    device=settings.align_device,
                    compute_type=settings.align_compute_type,
                    threads=settings.align_threads,
                    download_root=settings.data_dir / "models",
                    window_s=settings.align_window_seconds,
                    overlap_s=settings.align_overlap_seconds,
                ),
                poll_s=settings.worker_poll_seconds,
                isolated=True,
                memory_limit_mb=settings.align_memory_limit_mb,
            )
        )
        log.info(
            "local alignment worker enabled (model %s, %g s windows, memory cap %s)",
            settings.align_model,
            settings.align_window_seconds,
            f"{started[-1].memory_limit_mb} MiB" if started[-1].memory_limit_mb else "none",
        )
    else:
        log.warning(
            "no local alignment engine (%s): alignment jobs wait for an external worker "
            "(lectern worker --server …) and show as unavailable until one connects. See %s",
            missing,
            jobs.DOCS_URL,
        )
    for s in started:
        s.start()
    return started
