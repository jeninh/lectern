"""Server-side jobs run in-process: low-bitrate "data saver" audio renditions.

A ``transcode`` job (``jobs`` table, ``kind = "transcode"``) renders every track of one work
that lacks a rendition to ``<work>/derived/audio/<original sha256>.<m4a|opus>`` with ffmpeg.
Originals are never touched. The file's existence *is* the state, so a replaced track (new
sha256) simply has no rendition until the next job. One job per work, not per track: the
``uq_jobs_active_work_kind`` index allows one live job of a kind per work, and the payload
lists the tracks: ``{"codec", "bitrate_kbps", "tracks": [{"track_id", "sha256"}]}``.

The runner claims jobs with the same single-statement ``UPDATE … WHERE status='QUEUED'`` as
external workers will (docs/contracts.md); ``leased_by_id`` stays NULL for the local runner.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from lectern.config import Settings
from lectern.models import JOB_ACTIVE_STATUSES, AudioTrack, Job, JobStatus, Work, utcnow
from lectern.security.tokens import new_secret, sha256_hex

log = logging.getLogger("lectern.jobs")

TRANSCODE = "transcode"
EXT = {"aac": "m4a", "opus": "opus"}
MEDIA_TYPE = {"aac": "audio/mp4", "opus": "audio/ogg"}


def variant_path(library: Path, work: Work, sha256: str, codec: str) -> Path:
    return library / work.dir_path / "derived" / "audio" / f"{sha256}.{EXT[codec]}"


def find_variant(library: Path, work: Work, sha256: str) -> tuple[Path, str] | None:
    """The rendition of a track, whichever codec it was made with."""
    for codec in EXT:
        p = variant_path(library, work, sha256, codec)
        if p.is_file():
            return p, codec
    return None


def variant_sha256(path: Path) -> str:
    """The rendition's own SHA-256 (its ETag), cached in a ``.sha256`` sidecar."""
    from lectern.ingest.hashing import sha256_file

    side = path.with_name(path.name + ".sha256")
    try:
        if side.stat().st_mtime >= path.stat().st_mtime:
            return side.read_text().strip()
    except OSError:
        pass
    digest = sha256_file(path)
    side.write_text(digest)
    return digest


def active_job(db: Session, work_id: int, kind: str = TRANSCODE) -> Job | None:
    return db.scalar(
        select(Job).where(
            Job.work_id == work_id, Job.kind == kind, Job.status.in_(JOB_ACTIVE_STATUSES)
        )
    )


def enqueue_transcode(
    db: Session, settings: Settings, work: Work, user_id: int | None = None
) -> Job | None:
    """Queue renditions for the work's tracks that lack one. Returns the live job, or None
    when every track already has a rendition. Caller commits."""
    existing = active_job(db, work.id)
    if existing is not None:
        return existing
    tracks = db.scalars(
        select(AudioTrack).where(AudioTrack.work_id == work.id).order_by(AudioTrack.position)
    ).all()
    todo = [
        {"track_id": t.id, "sha256": t.sha256}
        for t in tracks
        if find_variant(settings.library_dir, work, t.sha256) is None
    ]
    if not todo:
        return None
    job = Job(
        kind=TRANSCODE,
        work_id=work.id,
        status=JobStatus.QUEUED,
        priority=10,
        max_attempts=1,
        payload={
            "codec": settings.audio_low_codec,
            "bitrate_kbps": settings.audio_low_bitrate_kbps,
            "tracks": todo,
        },
        created_by=user_id,
    )
    db.add(job)
    db.flush()
    return job


def _ffmpeg_args(ffmpeg: str, src: Path, dest: Path, codec: str, kbps: int) -> list[str]:
    args = [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(src),
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1"]  # fmt: skip
    if codec == "opus":
        args += ["-c:a", "libopus", "-b:a", f"{kbps}k", "-vbr", "on", "-f", "ogg"]
    else:
        args += ["-c:a", "aac", "-b:a", f"{kbps}k", "-movflags", "+faststart", "-f", "mp4"]
    return [*args, str(dest)]


def transcode_track(settings: Settings, src: Path, dest: Path, codec: str, kbps: int) -> None:
    if shutil.which(settings.ffmpeg_path) is None:
        raise RuntimeError("ffmpeg is not installed")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    try:
        res = subprocess.run(  # noqa: S603 - fixed argv, no shell
            _ffmpeg_args(settings.ffmpeg_path, src, tmp, codec, kbps),
            capture_output=True,
            timeout=6 * 3600,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(res.stderr.decode(errors="replace")[-500:] or "ffmpeg failed")
        os.replace(tmp, dest)
        variant_sha256(dest)
    finally:
        tmp.unlink(missing_ok=True)


def claim(db: Session, kinds: tuple[str, ...] = (TRANSCODE,)) -> tuple[Job, str] | None:
    """Atomically lease the next runnable job of ``kinds`` to the in-process runner."""
    now = utcnow()
    for _ in range(3):
        candidate = db.scalar(
            select(Job.id)
            .where(Job.status == JobStatus.QUEUED, Job.kind.in_(kinds), Job.run_after <= now)
            .order_by(Job.priority, Job.created_at)
            .limit(1)
        )
        if candidate is None:
            return None
        token = new_secret()
        res = db.execute(
            update(Job)
            .where(Job.id == candidate, Job.status == JobStatus.QUEUED)
            .values(
                status=JobStatus.LEASED,
                attempts=Job.attempts + 1,
                lease_token_hash=sha256_hex(token),
                lease_expires_at=now + timedelta(hours=6),
                heartbeat_at=now,
                started_at=now,
            )
        )
        db.commit()
        if res.rowcount == 1:
            return db.get(Job, candidate), token
    return None


def run_transcode(db: Session, settings: Settings, job: Job) -> None:
    work = db.get(Work, job.work_id)
    payload = job.payload or {}
    codec = payload.get("codec", settings.audio_low_codec)
    kbps = int(payload.get("bitrate_kbps", settings.audio_low_bitrate_kbps))
    done = 0
    try:
        for item in payload.get("tracks", []):
            track = db.get(AudioTrack, item["track_id"])
            if track is None or track.sha256 != item["sha256"] or work is None:
                continue  # track replaced or removed since enqueue
            if find_variant(settings.library_dir, work, track.sha256):
                continue
            src = settings.library_dir / work.dir_path / track.rel_path
            # Render outside the library and publish under the library lock, re-reading the
            # work's folder: an edit may rename (or trash) it while ffmpeg runs.
            scratch = settings.data_dir / "tmp" / f"{job.id}-{track.sha256}.{EXT[codec]}"
            scratch.parent.mkdir(parents=True, exist_ok=True)
            try:
                transcode_track(settings, src, scratch, codec, kbps)
                _publish_variant(db, settings, job, work, track.sha256, codec, scratch)
            finally:
                scratch.unlink(missing_ok=True)
                scratch.with_name(scratch.name + ".sha256").unlink(missing_ok=True)
            done += 1
            job.heartbeat_at = utcnow()
            job.result = {"done": done, "total": len(payload.get("tracks", []))}
            db.commit()
    except Exception as exc:  # noqa: BLE001 - recorded on the job
        log.warning("transcode job %s failed: %s", job.id, exc)
        db.rollback()
        job = db.get(Job, job.id)
        job.status = JobStatus.FAILED
        job.error = str(exc)[:2000]
        job.finished_at = utcnow()
        db.commit()
        return
    job.status = JobStatus.SUCCEEDED
    job.result = {"done": done, "total": len(payload.get("tracks", []))}
    job.finished_at = utcnow()
    db.commit()
    log.info("transcode job %s: %d renditions for work %s", job.id, done, job.work_id)


def _publish_variant(db, settings, job, work, sha256: str, codec: str, scratch: Path) -> None:
    from lectern.fsops import LIBRARY_LOCK, move_file

    with LIBRARY_LOCK:
        db.refresh(work)
        db.refresh(job)
        if work.trashed_at is not None or job.status != JobStatus.LEASED:
            return
        dest = variant_path(settings.library_dir, work, sha256, codec)
        dest.parent.mkdir(parents=True, exist_ok=True)
        side = scratch.with_name(scratch.name + ".sha256")
        dest.unlink(missing_ok=True)
        move_file(scratch, dest)
        if side.exists():
            dest.with_name(dest.name + ".sha256").unlink(missing_ok=True)
            move_file(side, dest.with_name(dest.name + ".sha256"))


def run_pending(session_factory: sessionmaker, settings: Settings) -> int:
    """Run every queued in-process job now. Returns how many ran."""
    n = 0
    while True:
        with session_factory() as db:
            claimed = claim(db)
            if claimed is None:
                return n
            run_transcode(db, settings, claimed[0])
            n += 1


def recover_local_leases(db: Session) -> int:
    """Jobs the in-process runner held when the server stopped go back to the queue."""
    res = db.execute(
        update(Job)
        .where(Job.kind == TRANSCODE, Job.status == JobStatus.LEASED, Job.leased_by_id.is_(None))
        .values(status=JobStatus.QUEUED, lease_token_hash=None, lease_expires_at=None)
    )
    db.commit()
    return res.rowcount or 0


def housekeeping(session_factory: sessionmaker, settings: Settings, busy=lambda _id: False) -> None:
    """The hourly sweep: abandoned uploads and imports, the 30-day trash, old undo history,
    replay records, and staged covers. Each part is independent; one failing does not stop
    the rest."""
    from lectern import editing, idempotency, imports, trash
    from lectern.fsops import LIBRARY_LOCK
    from lectern.uploads import sweep

    steps = [
        ("uploads", lambda db: sweep(db, settings)),
        ("imports", lambda db: (imports.sweep(db, settings), db.commit())),
        ("trash", lambda db: (trash.purge_expired(db, settings), db.commit())),
        ("idempotency", idempotency.sweep),
        ("editing", lambda db: editing.sweep(db, settings, busy)),
    ]
    for name, step in steps:
        with LIBRARY_LOCK, session_factory() as db:
            try:
                step(db)
            except Exception:  # noqa: BLE001
                db.rollback()
                log.exception("%s sweep failed", name)


class JobRunner:
    """Background thread: runs transcode jobs, the hourly housekeeping, and folder renames
    that had to wait for a build."""

    def __init__(
        self, session_factory: sessionmaker, settings: Settings, poll: float = 5.0, busy=None
    ):
        self.session_factory = session_factory
        self.settings = settings
        self.poll = poll
        self.busy = busy or (lambda _id: False)
        self.wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="lectern-jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        from lectern import editing
        from lectern.fsops import LIBRARY_LOCK

        last_sweep = utcnow()
        with self.session_factory() as db:
            recover_local_leases(db)
        while not self._stop.is_set():
            try:
                run_pending(self.session_factory, self.settings)
                if utcnow() - last_sweep > timedelta(hours=1):
                    housekeeping(self.session_factory, self.settings, self.busy)
                    last_sweep = utcnow()
                elif editing.PENDING_FOLDERS:
                    with LIBRARY_LOCK, self.session_factory() as db:
                        editing.sweep(db, self.settings, self.busy)
            except Exception:  # noqa: BLE001
                log.exception("job runner iteration failed")
            self.wake.wait(self.poll)
            self.wake.clear()
