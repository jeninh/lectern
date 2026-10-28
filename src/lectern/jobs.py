"""Server-side jobs run in-process: low-bitrate "data saver" audio renditions.

A ``transcode`` job (``jobs`` table, ``kind = "transcode"``) renders every track of one work
that lacks a rendition to ``<work>/derived/audio/<original sha256>.<m4a|opus>`` with ffmpeg.
Originals are never touched. The file's existence *is* the state, so a replaced track (new
sha256) simply has no rendition until the next job. One job per work, not per track: the
``uq_jobs_active_work_kind`` index allows one live job of a kind per work, and the payload
lists the tracks: ``{"codec", "bitrate_kbps", "tracks": [{"track_id", "sha256"}]}``.

The runner claims jobs with the same single-statement ``UPDATE … WHERE status='QUEUED'`` as
external workers (docs/contracts.md); ``leased_by_id`` stays NULL for the local runner.

External workers that advertise the ``transcode`` capability may run these jobs too
(``lectern worker`` with ffmpeg on its PATH): the kind is registered in :mod:`lectern.queue`,
each track is an input named ``track-<track_id>``, the worker uploads every rendition as an
artifact and then a result naming them, and the server publishes them exactly as the local
runner does. While such a worker is online the local runner leaves new jobs to it for
``LECTERN_LOCAL_DEFER_SECONDS``, then runs them itself (the Raspberry Pi fallback): one track
at a time, at the lowest CPU priority, on one thread, under a memory cap
(``LECTERN_TRANSCODE_MEMORY_LIMIT_MB``). A server without ffmpeg never claims them.

With neither (no ffmpeg here, no transcoding worker online) nothing is queued:
:func:`transcoder_unavailable` says why, ``POST /api/works/{id}/transcode`` answers
``503 no_transcoder``, and a job already queued reports the same message until a worker
calls in. The owner can turn copies off for the library (``instance_settings``) or one work
(``works.data_saver_enabled``); :func:`delete_renditions` removes a work's copies.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import threading
from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from lectern import limits, queue
from lectern.article import ARTICLE
from lectern.config import Settings
from lectern.models import JOB_ACTIVE_STATUSES, AudioTrack, Job, JobStatus, Work, utcnow
from lectern.pdf2epub.build import PDF2EPUB
from lectern.security.tokens import new_secret, sha256_hex

log = logging.getLogger("lectern.jobs")

TRANSCODE = "transcode"
CLEANUP = "storage-cleanup"  # lectern.storage (the storage dashboard's cleanup)
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


def generation_off(db: Session, work: Work) -> str | None:
    """Why no data-saver copies may be made for ``work``, or ``None`` when they may."""
    from lectern import instance

    if not instance.get(db, "data_saver_enabled"):
        return "data_saver_off"
    if not work.data_saver_enabled:
        return "data_saver_off_for_work"
    return None


def local_transcoder(settings: Settings) -> bool:
    """Whether this server makes renditions itself (ffmpeg is installed)."""
    return shutil.which(settings.ffmpeg_path) is not None


def transcoder_unavailable(db: Session, settings: Settings, now=None) -> dict | None:
    """``None`` when something can make renditions: ffmpeg on this server, or an external
    worker that advertises ``transcode`` and is online and not paused. Otherwise why not."""
    if local_transcoder(settings) or queue.capability_online(db, queue.TRANSCODE, now):
        return None
    return {
        "reason": "no_transcoder",
        "message": "ffmpeg is not installed on this server and no worker that can transcode "
        f"(lectern worker with ffmpeg) has connected in the last {queue.WORKER_SEEN_S // 60} "
        "minutes, so a data-saver copy cannot be made.",
    }


def cancel_transcode(db: Session, job: Job) -> None:
    """Cancel a live transcode job. A local run stops publishing (``_publish_variant`` needs
    the lease); a worker's result is refused (``queue.hold_lease``). Caller commits."""
    # Conditional on the row, not this session's copy: a job that finished meanwhile stays
    # finished (its renditions are published).
    db.execute(
        update(Job)
        .where(Job.id == job.id, Job.status.in_(JOB_ACTIVE_STATUSES))
        .values(
            status=JobStatus.CANCELLED,
            lease_token_hash=None,
            lease_expires_at=None,
            heartbeat_at=None,
            finished_at=utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    db.expire(job)


def cancel_all_transcodes(db: Session) -> int:
    """Cancel every live transcode job (the owner turned data saver off). Caller commits."""
    jobs = db.scalars(
        select(Job).where(Job.kind == TRANSCODE, Job.status.in_(JOB_ACTIVE_STATUSES))
    ).all()
    for job in jobs:
        cancel_transcode(db, job)
    return len(jobs)


def delete_renditions(db: Session, settings: Settings, work: Work) -> int:
    """Cancel the work's transcode job and delete every rendition in its ``derived/audio``
    folder (stale ones of replaced tracks too). Originals are not touched. Returns the bytes
    freed. Caller commits; the copies can be made again."""
    from lectern.fsops import LIBRARY_LOCK

    job = active_job(db, work.id)
    if job is not None:
        cancel_transcode(db, job)
        db.commit()
    freed = 0
    with LIBRARY_LOCK:
        db.refresh(work)
        folder = settings.library_dir / work.dir_path / "derived" / "audio"
        if not folder.is_dir() or folder.is_symlink():
            return 0
        for p in folder.iterdir():
            rendition = p.suffix.lstrip(".") in EXT.values()
            sidecar = p.name.endswith(".sha256") or p.name.endswith(".tmp")
            if p.is_symlink() or not p.is_file() or not (rendition or sidecar):
                continue
            if rendition:
                freed += p.stat().st_size
            p.unlink(missing_ok=True)
    return freed


def rendition_sizes(settings: Settings, work: Work, tracks) -> tuple[int, int, int]:
    """``(tracks with a rendition, their originals' bytes, the renditions' bytes)``."""
    have = original = low = 0
    for t in tracks:
        found = find_variant(settings.library_dir, work, t.sha256)
        if found is not None:
            have += 1
            original += t.size
            low += found[0].stat().st_size
    return have, original, low


def auto_enqueue(db: Session, settings: Settings, work: Work) -> Job | None:
    """After an audiobook import with ``LECTERN_AUDIO_LOW_AUTO``: queue renditions when the
    library and the work allow them and something can make them; otherwise nothing is queued
    (the owner can make them later). Caller commits."""
    if generation_off(db, work) or transcoder_unavailable(db, settings):
        log.info("no data-saver copy queued for work %s: off or no transcoder", work.id)
        return None
    return enqueue_transcode(db, settings, work)


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
        # The local runner fails a job on its first error; attempts matter for leases held
        # by external workers (an expired lease is retried, like any remote job).
        max_attempts=3,
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


def _ffmpeg_args(
    ffmpeg: str, src: Path, dest: Path, codec: str, kbps: int, *, threads: int | None = None
) -> list[str]:
    """One track, same timeline: the first audio stream from 0 s, nothing trimmed or padded.
    Encoder priming is recorded in the container (an MP4 edit list, Ogg pre-skip), so players
    start at the same sample as the original (docs/contracts.md, "seek tolerance")."""
    args = [ffmpeg, "-nostdin", "-v", "error", "-y"]
    if threads:
        args += ["-threads", str(threads)]
    args += ["-i", str(src),
             "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1"]  # fmt: skip
    if threads:
        args += ["-threads", str(threads)]
    if codec == "opus":
        args += ["-c:a", "libopus", "-b:a", f"{kbps}k", "-vbr", "on", "-f", "ogg"]
    else:
        args += ["-c:a", "aac", "-b:a", f"{kbps}k", "-movflags", "+faststart", "-f", "mp4"]
    return [*args, str(dest)]


def _throttle(memory_mb: int | None, nice: int = 19):
    """``preexec_fn`` for the server's own ffmpeg: a low CPU priority, no core dumps, and
    the address space capped (best effort where the platform refuses a limit). Only plain
    system calls run in the forked child: ``resource`` is imported here, in the parent."""
    import resource

    def limit() -> None:
        caps = [(resource.RLIMIT_CORE, 0)]
        if memory_mb:
            caps.append((resource.RLIMIT_AS, memory_mb * 1024 * 1024))
        for which, value in caps:
            try:
                resource.setrlimit(which, (value, value))
            except (ValueError, OSError):
                pass
        try:
            os.nice(nice)
        except OSError:
            pass

    return limit


def transcode_track(settings: Settings, src: Path, dest: Path, codec: str, kbps: int) -> None:
    """Render one track on this server (the Raspberry Pi fallback), throttled so playback and
    sync stay responsive: the ``transcode`` limits (lectern.limits; one thread, ``nice`` 19,
    ``LECTERN_TRANSCODE_MEMORY_LIMIT_MB`` by default)."""
    if not local_transcoder(settings):
        raise RuntimeError("ffmpeg is not installed")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    lim = limits.GOVERNOR.limit(limits.TRANSCODE)
    try:
        res = subprocess.run(  # noqa: S603 - fixed argv, no shell
            _ffmpeg_args(settings.ffmpeg_path, src, tmp, codec, kbps, threads=lim.threads),
            capture_output=True,
            timeout=6 * 3600,
            check=False,
            preexec_fn=_throttle(lim.memory_mb, lim.nice),
        )
        if res.returncode != 0:
            raise RuntimeError(res.stderr.decode(errors="replace")[-500:] or "ffmpeg failed")
        os.replace(tmp, dest)
        variant_sha256(dest)
    finally:
        tmp.unlink(missing_ok=True)


def claim(
    db: Session, kinds: tuple[str, ...] = (TRANSCODE,), *, defer_s: float = 0.0
) -> tuple[Job, str] | None:
    """Atomically lease the next runnable job of ``kinds`` to the in-process runner. While an
    external worker that can transcode is online, only jobs queued at least ``defer_s`` ago."""
    now = utcnow()
    waiting = Job.status == JobStatus.QUEUED
    if defer_s > 0 and queue.capability_online(db, queue.TRANSCODE, now):
        waiting = waiting & (Job.created_at <= now - timedelta(seconds=defer_s))
    for _ in range(3):
        candidate = db.scalar(
            select(Job.id)
            .where(waiting, Job.kind.in_(kinds), Job.run_after <= now)
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
                # the in-process runner holds it now, not a worker that released it earlier
                leased_by_id=None,
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
            db.refresh(job)
            if job.status != JobStatus.LEASED:
                log.info("transcode job %s stopped: %s", job.id, job.status.value)
                return  # cancelled (data saver turned off, copies deleted) meanwhile
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
    db.refresh(job)
    if job.status != JobStatus.LEASED:
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


# Heavy kinds this runner runs, by resource-limit feature (lectern.limits). Saved web articles
# and storage cleanups are light and never wait for a slot.
FEATURE_OF_KIND = {PDF2EPUB: limits.CONVERT, TRANSCODE: limits.TRANSCODE}


def run_pending(session_factory: sessionmaker, settings: Settings, busy=lambda _id: False) -> int:
    """Run every queued in-process job now. Returns how many ran. Conversions (``pdf2epub``)
    and saved web articles (``article``) never wait for a transcode worker: they are claimed
    first, without the deferral.
    Transcodes run here only with ffmpeg; otherwise they wait for an external worker (and
    say so, :func:`transcoder_unavailable`). Conversions and transcodes start only while
    their resource limits allow (:data:`lectern.limits.GOVERNOR`); until then they stay
    queued."""
    transcodes = local_transcoder(settings)
    n = 0
    while True:
        with session_factory() as db:
            with limits.GOVERNOR.gate(limits.CONVERT, limits.TRANSCODE) as gate:
                light = (ARTICLE, CLEANUP)
                kinds = (PDF2EPUB, *light) if limits.CONVERT in gate.allowed else light
                claimed = claim(db, kinds=kinds)
                if claimed is None and transcodes and limits.TRANSCODE in gate.allowed:
                    claimed = claim(db, defer_s=settings.local_defer_seconds)
                if claimed is None:
                    return n
                job = claimed[0]
                feature = FEATURE_OF_KIND.get(job.kind)
                slot = gate.admit(feature, job.id) if feature else contextlib.nullcontext()
            with slot:
                if job.kind == PDF2EPUB:
                    from lectern.pdf2epub import jobs as pdf2epub

                    pdf2epub.run(db, settings, job)
                elif job.kind == CLEANUP:
                    from lectern import storage

                    storage.run(db, settings, job, busy)
                elif job.kind == ARTICLE:
                    from lectern.article import jobs as article

                    article.run(db, settings, job)
                else:
                    run_transcode(db, settings, job)
            n += 1


# --- external workers -------------------------------------------------------------------------


class Rendition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track_id: int
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # the original track's
    artifact: str = Field(pattern=queue.ARTIFACT_NAME_RE.pattern)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TranscodeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    renditions: list[Rendition] = Field(max_length=5000)


def _remote_inputs(db: Session, settings: Settings, job: Job) -> dict[str, queue.InputFile]:
    """``track-<id>`` for every payload track that is still the same file."""
    work = db.get(Work, job.work_id) if job.work_id is not None else None
    if work is None or work.trashed_at is not None:
        return None
    library = settings.library_dir.resolve()
    out = {}
    for item in (job.payload or {}).get("tracks", []):
        track = db.get(AudioTrack, item.get("track_id"))
        if track is None or track.work_id != work.id or track.sha256 != item.get("sha256"):
            continue
        # resolved now, so a file swapped for a symlink out of the library is never served
        path = (settings.library_dir / work.dir_path / track.rel_path).resolve()
        if library not in path.parents:
            continue
        out[f"track-{track.id}"] = queue.InputFile(path, track.media_type, track.sha256)
    return out


def _pin(src: Path, dest: Path) -> str:
    """Copy ``src`` to ``dest`` and hash what was copied, from one open file: a later rename
    over ``src`` cannot change the bytes that are checked and published."""
    import hashlib

    digest = hashlib.sha256()
    with src.open("rb") as fin, dest.open("xb") as fout:
        while chunk := fin.read(1 << 20):
            digest.update(chunk)
            fout.write(chunk)
        fout.flush()
        os.fsync(fout.fileno())
    return digest.hexdigest()


def _place(staged: Path, dest: Path, digest: str) -> None:
    """The publication step proper: a rename, then the checksum sidecar. A work folder on
    another filesystem than the library root (a bind mount) gets a copy next to ``dest``
    first, so the final step is still an atomic rename."""
    import errno

    try:
        os.replace(staged, dest)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        tmp = dest.with_name(f".{dest.name}.{new_secret()[:8]}.part")
        try:
            shutil.copyfile(staged, tmp)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
    dest.with_name(dest.name + ".sha256").write_text(digest)


def _plays_like(path: Path, original: AudioTrack) -> bool:
    """A rendition must be audio mutagen can read, as long as the original (within 1 s or
    1 %): a worker cannot publish a truncated file or something that is not audio at all."""
    from lectern.align.tracks import probe

    duration, _codec, _chapters = probe(path)
    if duration <= 0:
        return False
    if original.duration_s <= 0:
        return True
    return abs(duration - original.duration_s) <= max(1.0, 0.01 * original.duration_s)


def _remote_result(db: Session, settings: Settings, job: Job, body: bytes, artifacts: Path) -> dict:
    """Check a worker's renditions and publish them, fenced by the uploading lease.

    Every payload track that is still current and has no rendition yet must be in the result,
    once. Each artifact is pinned: copied into ``<library>/.lectern-staging/<job>-<tag>/``
    (the library's filesystem, outside every work folder, so a rename or trash of the work
    meanwhile strands nothing) and hashed from that one read, then probed (it must play as
    long as the original), before any lock is taken. Then, under the library lock, the
    work's current folder is read, the lease is held (``queue.hold_lease``: the database
    write lock on the exact, unexpired lease), the pinned files are renamed into place, and
    the job becomes ``succeeded`` in that same transaction. A lost lease publishes nothing
    (``LeaseLost``); a failure rolls back, the job stays leased, and the worker's retry of
    the upload finishes it. The staged copies are always removed (and swept if the server
    dies in between: ``queue.sweep_staging``)."""
    from lectern.align.jobs import LeaseLost, ResultError
    from lectern.fsops import LIBRARY_LOCK

    total = len((job.payload or {}).get("tracks", []))
    if job.status == JobStatus.SUCCEEDED:
        return {"job_id": job.id, "status": "succeeded", **(job.result or {})}
    try:
        parsed = TranscodeResult.model_validate_json(body)
    except (ValidationError, ValueError) as exc:
        raise ResultError(400, "invalid_result", [str(exc)[:500]]) from exc
    expected = {t["track_id"]: t["sha256"] for t in (job.payload or {}).get("tracks", [])}
    work = db.get(Work, job.work_id)
    if work is None or work.trashed_at is not None:
        raise LeaseLost()
    codec = (job.payload or {}).get("codec", settings.audio_low_codec)
    ids = [r.track_id for r in parsed.renditions]
    if len(ids) != len(set(ids)):
        raise ResultError(422, "duplicate_track", [])
    current = {}
    for track_id, sha in expected.items():
        track = db.get(AudioTrack, track_id)
        if track is not None and track.sha256 == sha:
            current[track_id] = track
    missing = [
        t for t, track in current.items()
        if t not in ids and find_variant(settings.library_dir, work, track.sha256) is None
    ]  # fmt: skip
    if missing:
        raise ResultError(422, "missing_renditions", [f"track {t}" for t in missing])
    stage = queue.staging_dir(settings.library_dir, job.id, new_secret()[:8])
    try:
        stage.mkdir(parents=True)
        pinned = []
        for r in parsed.renditions:
            if expected.get(r.track_id) != r.sha256:
                raise ResultError(422, "input_mismatch", [f"track {r.track_id} is not in the job"])
            src = artifacts / r.artifact
            copy = stage / f"{r.track_id}.{EXT.get(codec, 'bin')}"  # one per track
            try:
                digest = _pin(src, copy)
            except FileNotFoundError:
                digest = None
            if digest != r.artifact_sha256:
                raise ResultError(422, "missing_artifact", [r.artifact])
            if r.track_id in current:
                if not _plays_like(copy, current[r.track_id]):
                    raise ResultError(422, "bad_rendition", [f"track {r.track_id}"])
                pinned.append((r, copy))
        result = {"done": len(pinned), "total": total}
        # The library lock first, then the database's: the order rescans and edits use.
        with LIBRARY_LOCK:
            db.refresh(work)
            if work.trashed_at is not None:
                raise LeaseLost()
            if not queue.hold_lease(db, job):
                raise LeaseLost()
            try:
                for r, copy in pinned:
                    if find_variant(settings.library_dir, work, r.sha256) is None:
                        # the work's folder as it is now, under the lock: a rename earlier
                        # moved nothing of ours
                        dest = variant_path(settings.library_dir, work, r.sha256, codec)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        _place(copy, dest, r.artifact_sha256)
                now = utcnow()
                db.execute(
                    update(Job)
                    .where(Job.id == job.id)
                    .values(
                        status=JobStatus.SUCCEEDED,
                        result=result,
                        finished_at=now,
                        error=None,
                        lease_expires_at=None,
                        updated_at=now,
                    )  # fmt: skip
                    .execution_options(synchronize_session=False)
                )
            except BaseException:
                db.rollback()
                raise
            db.commit()
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    db.refresh(job)
    log.info("transcode job %s: %d renditions from an external worker", job.id, len(pinned))
    return {"job_id": job.id, "status": "succeeded", **result}


queue.register(
    queue.KindHandler(
        kind=TRANSCODE,
        capability=lambda _payload: queue.TRANSCODE,
        inputs=_remote_inputs,
        apply_result=_remote_result,
    )
)


def recover_local_leases(db: Session) -> int:
    """Jobs the in-process runner held when the server stopped go back to the queue."""
    res = db.execute(
        update(Job)
        .where(
            Job.kind.in_((TRANSCODE, PDF2EPUB, ARTICLE, CLEANUP)),
            Job.status == JobStatus.LEASED,
            Job.leased_by_id.is_(None),
        )
        .values(status=JobStatus.QUEUED, lease_token_hash=None, lease_expires_at=None)
    )
    db.commit()
    return res.rowcount or 0


def housekeeping(session_factory: sessionmaker, settings: Settings, busy=lambda _id: False) -> None:
    """The hourly sweep: abandoned uploads and imports, the 30-day trash, old undo history,
    replay records, and staged covers. Each part is independent; one failing does not stop
    the rest."""
    from lectern import duplicates, editing, idempotency, imports, trash
    from lectern.fsops import LIBRARY_LOCK
    from lectern.tts.jobs import sweep as tts_sweep
    from lectern.uploads import sweep

    steps = [
        ("uploads", lambda db: sweep(db, settings)),
        ("imports", lambda db: (imports.sweep(db, settings), db.commit())),
        ("trash", lambda db: (trash.purge_expired(db, settings), db.commit())),
        ("idempotency", idempotency.sweep),
        ("editing", lambda db: editing.sweep(db, settings, busy)),
        ("tts", lambda db: tts_sweep(db, settings.data_dir)),
        ("duplicates", lambda db: (duplicates.scan(db), db.commit())),
    ]
    for name, step in steps:
        with LIBRARY_LOCK, session_factory() as db:
            try:
                step(db)
            except Exception:  # noqa: BLE001
                db.rollback()
                log.exception("%s sweep failed", name)


class JobRunner:
    """Background thread: runs transcode jobs, the metadata pass (``lectern.autometa``), the
    hourly housekeeping, and folder renames that had to wait for a build. ``fetch`` is the
    metadata lookup's transport (``app.state.lookup_fetch``)."""

    def __init__(
        self,
        session_factory: sessionmaker,
        settings: Settings,
        poll: float = 5.0,
        busy=None,
        fetch=None,
    ):
        self.session_factory = session_factory
        self.settings = settings
        self.fetch = fetch
        self.poll = poll
        self.busy = busy or (lambda _id: False)
        self.wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._helpers: list[threading.Thread] = []

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="lectern-jobs", daemon=True)
        self._thread.start()
        # More conversions or transcodes at once only when their limits allow it
        # (LECTERN_CONVERT_CONCURRENCY, LECTERN_TRANSCODE_CONCURRENCY): helper threads that
        # only run jobs. This thread keeps the metadata pass and the sweeps.
        self._helpers = [
            threading.Thread(target=self._run_jobs, name=f"lectern-jobs-{i}", daemon=True)
            for i in range(1, limits.pool_size(limits.CONVERT, limits.TRANSCODE))
        ]
        for t in self._helpers:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        self.wake.set()
        for t in [self._thread, *self._helpers]:
            if t:
                t.join(timeout=5)

    def _run_jobs(self) -> None:
        while not self._stop.is_set():
            try:
                run_pending(self.session_factory, self.settings, self.busy)
            except Exception:  # noqa: BLE001
                log.exception("job runner iteration failed")
            self._stop.wait(self.poll)

    def _run(self) -> None:
        from lectern import autometa, editing
        from lectern.fsops import LIBRARY_LOCK

        last_sweep = utcnow()
        with self.session_factory() as db:
            recover_local_leases(db)
        while not self._stop.is_set():
            try:
                run_pending(self.session_factory, self.settings, self.busy)
                autometa.run_pending(self.session_factory, self.fetch)
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
