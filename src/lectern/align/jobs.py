"""Durable job queue with leases (docs/contracts.md, "Worker protocol").

Everything here takes an open SQLAlchemy session and commits; the HTTP layer only translates
exceptions into status codes. The same functions drive the in-process worker and the
``/api/worker/*`` routes, so the rules (one claim statement, hashed lease tokens, backoff,
reclaim, staleness) exist exactly once.
"""

from __future__ import annotations

import logging
import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from lectern.align.text import spine_lengths
from lectern.align.tracks import fingerprint_of, sync_tracks, work_epub, work_tracks
from lectern.align.transcribe import DEFAULT_MODEL, MODELS, larger_model
from lectern.ingest.sentences import replace_sentences
from lectern.models import (
    JOB_ACTIVE_STATUSES,
    Alignment,
    AlignmentStatus,
    Job,
    JobStatus,
    Work,
    WorkerCredential,
    utcnow,
)
from lectern.security.tokens import constant_time_equal, new_secret, sha256_hex
from lectern.sentence_map import SentenceMap, map_path, write_sentence_map

log = logging.getLogger("lectern.align.jobs")

KIND_ALIGN = "align"
LEASE_MIN_S = 60
LEASE_MAX_S = 3600
BACKOFF_BASE_S = 30.0
BACKOFF_MAX_S = 3600.0
BACKOFF_JITTER = 0.2
RESULT_MAX_BYTES = 32 * 1024 * 1024
LOW_COVERAGE = 0.85
TRACK_TOLERANCE_S = 0.05
SPEED_SAMPLE_JOBS = 5
# A leased job whose progress has not moved for this long is treated like an expired lease,
# even while its worker still heartbeats (a hung decoder, a wedged GPU). Model downloads on
# a slow link report no progress, hence the generous default (LECTERN_ALIGN_STALL_SECONDS).
STALL_S = 3600.0
# An external worker that polled within this window counts as an alignment engine.
WORKER_SEEN_S = 300
DOCS_URL = "https://github.com/jeninh/lectern/blob/main/docs/alignment.md#turning-it-on"


class LeaseLost(Exception):
    """The caller's lease is not the job's current, unexpired lease."""


class NoInputs(ValueError):
    """The work has no EPUB or no audio tracks, so there is nothing to align."""


Count = Annotated[int, Field(ge=0, le=10**9)]
Seconds = Annotated[float, Field(ge=0, le=10**7)]
Fraction = Annotated[float, Field(ge=0, le=1)]
# Audio seconds per wall-clock second. GPU runs reach a few hundred times realtime and a
# re-match of a recorded transcript a few thousand; beyond MAX_SPEED is a bug or a lie.
MAX_SPEED = 100_000.0


class AlignStats(BaseModel):
    """What a worker may report about a run (``lectern.align.pipeline`` produces it).

    Workers are trusted to align, not to write arbitrary JSON into job rows: unknown keys are
    dropped, numbers must be finite and in range, and ``speed`` (the basis of every ETA) must
    be positive. Stored as ``job.result["stats"]``.
    """

    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    model: str | None = Field(default=None, max_length=64)
    language: str | None = Field(default=None, max_length=16)
    speed: float | None = Field(default=None, gt=0, le=MAX_SPEED)
    diagnosis: (
        Literal["ok", "partial_audio", "poor_transcription", "abridged", "no_match"] | None
    ) = None
    coverage: Fraction | None = None
    span_coverage: Fraction | None = None
    low_coverage: bool | None = None
    likely_abridged: bool | None = None
    span_audio_vs_expected: float | None = Field(default=None, ge=0, le=1000)
    chars_per_second: float | None = Field(default=None, ge=0, le=10**4)
    tokens_per_second: float | None = Field(default=None, ge=0, le=10**4)
    words: Count | None = None
    text_tokens: Count | None = None
    audio_tokens: Count | None = None
    matched_tokens: Count | None = None
    sentences: Count | None = None
    matched_sentences: Count | None = None
    span_first_sentence: Count | None = None
    span_last_sentence: Count | None = None
    leading_unmatched_tokens: Count | None = None
    trailing_unmatched_tokens: Count | None = None
    interior_unmatched_tokens: Count | None = None
    interior_unmatched_runs: Count | None = None
    longest_interior_unmatched_run: Count | None = None
    reordered_text_tokens: Count | None = None
    audio_s: Seconds | None = None
    span_audio_s: Seconds | None = None
    expected_span_audio_s: Seconds | None = None
    text_s: Seconds | None = None
    transcribe_s: Seconds | None = None
    match_s: Seconds | None = None
    elapsed_s: Seconds | None = None
    model_load_s: Seconds | None = None
    peak_rss_mb: float | None = Field(default=None, ge=0, le=10**7)

    def stored(self) -> dict:
        return self.model_dump(exclude_none=True)


@dataclass
class ResultError(Exception):
    status: int
    code: str
    errors: list = field(default_factory=list)


# --- enqueue, cancel, staleness --------------------------------------------------------------


def backoff_s(attempts: int, rng: random.Random | None = None) -> float:
    """``min(30 s × 2^(a−1), 1 h)`` ± 20 % jitter for the ``attempts``-th failure."""
    base = min(BACKOFF_BASE_S * 2 ** max(attempts - 1, 0), BACKOFF_MAX_S)
    jitter = (rng or random).uniform(-BACKOFF_JITTER, BACKOFF_JITTER)  # noqa: S311 - not security
    return base * (1 + jitter)


def align_payload(db: Session, library: Path, work: Work, model: str) -> dict:
    """Input snapshot for an align job: what a result must match."""
    found = work_epub(db, library, work)
    tracks = work_tracks(db, work.id)
    if found is None or not tracks:
        raise NoInputs("the work needs an EPUB and at least one audio track")
    book, _ = found
    return {
        "epub_sha256": book.sha256,
        "audio_fingerprint": fingerprint_of(tracks),
        "model": model,
        "language": work.language,
        "tracks": [
            {
                "position": t.position,
                "sha256": t.sha256,
                "duration_s": t.duration_s,
                "offset_s": t.offset_s,
                "size": t.size,
                "media_type": t.media_type,
            }
            for t in tracks
        ],
    }


def active_job(db: Session, work_id: int, kind: str = KIND_ALIGN) -> Job | None:
    return db.scalar(
        select(Job).where(
            Job.work_id == work_id, Job.kind == kind, Job.status.in_(JOB_ACTIVE_STATUSES)
        )
    )


def work_alignment(db: Session, work_id: int) -> Alignment | None:
    return db.scalar(select(Alignment).where(Alignment.work_id == work_id))


def enqueue_align(
    db: Session,
    library: Path,
    work: Work,
    *,
    model: str | None = None,
    created_by: int | None = None,
    force: bool = False,
    priority: int = 0,
) -> tuple[Job, bool]:
    """Queue an alignment. Returns ``(job, created)``; an existing active job is returned
    unchanged unless ``force`` cancels it first. Caller commits."""
    existing = active_job(db, work.id)
    if existing is not None:
        if not force:
            return existing, False
        cancel_job(db, existing)
    payload = align_payload(db, library, work, model or DEFAULT_MODEL)
    job = Job(
        kind=KIND_ALIGN,
        work_id=work.id,
        status=JobStatus.QUEUED,
        priority=priority,
        payload=payload,
        created_by=created_by,
    )
    db.add(job)
    alignment = work_alignment(db, work.id)
    if alignment is None:
        db.add(
            Alignment(
                work_id=work.id,
                model=payload["model"],
                status=AlignmentStatus.PENDING,
                epub_sha256=payload["epub_sha256"],
                audio_fingerprint=payload["audio_fingerprint"],
            )
        )
    elif alignment.status in (AlignmentStatus.FAILED, AlignmentStatus.RUNNING):
        alignment.status = AlignmentStatus.PENDING
        alignment.model = payload["model"]
    db.flush()
    return job, True


def cancel_job(db: Session, job: Job) -> bool:
    """Move an active job to ``cancelled`` and drop its lease. Caller commits."""
    if job.status not in JOB_ACTIVE_STATUSES:
        return False
    job.status = JobStatus.CANCELLED
    job.lease_token_hash = None
    job.lease_expires_at = None
    job.heartbeat_at = None
    job.finished_at = utcnow()
    alignment = work_alignment(db, job.work_id) if job.work_id is not None else None
    if alignment is not None and alignment.status in (
        AlignmentStatus.PENDING,
        AlignmentStatus.RUNNING,
    ):
        alignment.status = AlignmentStatus.FAILED
        alignment.error = "cancelled"
    db.flush()
    return True


def refresh_work(
    db: Session, library: Path, work: Work, *, auto_enqueue: bool = True
) -> Job | None:
    """Bring the work's tracks and alignment state in line with its files. Caller commits.

    - ``audio_tracks`` is synced with the files in the work directory;
    - if the EPUB or audio no longer match the alignment or the active job's snapshot, the
      job is cancelled, a ``succeeded`` alignment becomes ``stale``, and a fresh job is
      queued (contract: "Staleness and cancellation");
    - with ``auto_enqueue``, a work that gained its first EPUB + audio pair is queued once.
      Failed and cancelled alignments are never retried on their own.

    Returns the job queued by this call, if any.
    """
    sync_tracks(db, library, work)
    alignment = work_alignment(db, work.id)
    existing = active_job(db, work.id)
    try:
        current = align_payload(db, library, work, DEFAULT_MODEL)
    except NoInputs:
        if existing is not None:
            cancel_job(db, existing)
        if alignment is not None and alignment.status == AlignmentStatus.SUCCEEDED:
            alignment.status = AlignmentStatus.STALE
        return None

    def _matches(snapshot: dict | None) -> bool:
        return snapshot is not None and (
            snapshot.get("epub_sha256"),
            snapshot.get("audio_fingerprint"),
        ) == (current["epub_sha256"], current["audio_fingerprint"])

    if existing is not None:
        if _matches(existing.payload):
            return None
        model = (existing.payload or {}).get("model") or DEFAULT_MODEL
        cancel_job(db, existing)
        if alignment is not None and alignment.status == AlignmentStatus.SUCCEEDED:
            alignment.status = AlignmentStatus.STALE
        job, _ = enqueue_align(db, library, work, model=model)
        return job
    if alignment is not None:
        if alignment.status == AlignmentStatus.SUCCEEDED and not _matches(
            {
                "epub_sha256": alignment.epub_sha256,
                "audio_fingerprint": alignment.audio_fingerprint,
            }
        ):
            alignment.status = AlignmentStatus.STALE
            job, _ = enqueue_align(db, library, work, model=_model_name(alignment.model))
            return job
        return None
    if auto_enqueue:
        job, _ = enqueue_align(db, library, work)
        return job
    return None


# --- claim, lease, heartbeat ---------------------------------------------------------------


def reclaim_expired(
    db: Session,
    now: datetime | None = None,
    *,
    stall_s: float = STALL_S,
    orphaned_local: bool = False,
) -> int:
    """Leases past their expiry (or held by a revoked credential), runs whose progress has
    not moved for ``stall_s``, and, with ``orphaned_local``, leases of the in-process worker
    (only true at startup: that worker died with the previous server process) go back to the
    queue with backoff, or fail when attempts are exhausted. Returns the number reclaimed."""
    now = now or utcnow()
    revoked = select(WorkerCredential.id).where(WorkerCredential.revoked_at.is_not(None))
    leased = db.scalars(
        select(Job)
        .where(
            Job.kind == KIND_ALIGN,  # transcode jobs belong to lectern.jobs.JobRunner
            Job.status == JobStatus.LEASED,
        )
        .execution_options(populate_existing=True)
    ).all()
    revoked_ids = set(db.scalars(revoked))
    reclaimed = 0
    for job in leased:
        if job.lease_expires_at is None or job.lease_expires_at < now:
            error = "lease expired"
        elif job.leased_by_id is not None and job.leased_by_id in revoked_ids:
            error = "lease expired"
        elif orphaned_local and job.leased_by_id is None:
            error = "the server restarted during alignment (it may have run out of memory)"
        elif (stalled := _since(job, "progress_at", now)) is not None and stalled > stall_s:
            error = f"no progress for {round(stalled / 60)} min"
        else:
            continue
        reclaimed += _reclaim_one(db, job, error, now)
    if reclaimed:
        db.commit()
    return reclaimed


def _reclaim_one(db: Session, job: Job, error: str, now: datetime) -> bool:
    """Requeue (with backoff) or fail one lease, but only if it is still exactly the lease
    that was looked at: the same job, still ``leased``, with the same token hash and last
    heartbeat. A worker
    that succeeded, failed, or was cancelled in the meantime (and a fresh claim of a requeued
    job, which has a new token) is left alone; requeueing a cancelled job next to its
    replacement would also break the one-active-job-per-work index and roll everything back.
    """
    retry = job.attempts < job.max_attempts
    values: dict = {
        "error": error[:2000],
        "lease_expires_at": None,
        "heartbeat_at": None,
        "updated_at": now,
    }
    if retry:
        values.update(
            status=JobStatus.QUEUED,
            lease_token_hash=None,  # a late upload from the old lease is 409 lease_lost
            run_after=now + timedelta(seconds=backoff_s(job.attempts)),
            result=None,
        )
    else:
        values.update(status=JobStatus.FAILED, finished_at=now)
    done = db.execute(
        update(Job)
        .where(
            Job.id == job.id,
            Job.status == JobStatus.LEASED,
            Job.lease_token_hash == job.lease_token_hash,
            # a heartbeat (with progress) that landed after the read keeps the lease
            Job.heartbeat_at.is_(None)
            if job.heartbeat_at is None
            else Job.heartbeat_at == job.heartbeat_at,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.expire(job)
    if done != 1:
        return False
    alignment = (
        db.scalar(
            select(Alignment)
            .where(Alignment.work_id == job.work_id)
            .execution_options(populate_existing=True)
        )
        if job.work_id is not None
        else None
    )
    if alignment is not None:
        if retry and alignment.status == AlignmentStatus.RUNNING:
            alignment.status = AlignmentStatus.PENDING
        elif not retry and alignment.status != AlignmentStatus.SUCCEEDED:
            alignment.status = AlignmentStatus.FAILED
            alignment.error = values["error"]
    return True


def _since(job: Job, key: str, now: datetime) -> float | None:
    """Seconds since the ISO time stored in ``job.result[key]`` (claim/heartbeat bookkeeping)."""
    value = (job.result or {}).get(key)
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - at).total_seconds()


def claim(
    db: Session,
    *,
    kinds: list[str],
    worker: str,
    lease_s: int,
    credential_id: int | None,
    now: datetime | None = None,
    stall_s: float = STALL_S,
) -> tuple[Job, str] | None:
    """Atomically lease the next runnable job. Returns ``(job, lease_token)`` or ``None``.
    ``stall_s`` is ``LECTERN_ALIGN_STALL_SECONDS`` for the reclaim that runs first."""
    now = now or utcnow()
    lease_s = max(LEASE_MIN_S, min(LEASE_MAX_S, int(lease_s)))
    kinds = [k for k in kinds if k == KIND_ALIGN]  # only align jobs leave the server
    if not kinds:
        return None
    reclaim_expired(db, now, stall_s=stall_s)
    token = new_secret()
    runnable = select(Job.id).where(
        Job.status == JobStatus.QUEUED, Job.kind.in_(kinds), Job.run_after <= now
    )
    for _ in range(3):
        candidate = (
            runnable.order_by(Job.priority, Job.created_at, Job.id).limit(1).scalar_subquery()
        )
        stmt = (
            update(Job)
            .where(Job.id == candidate, Job.status == JobStatus.QUEUED)
            .values(
                status=JobStatus.LEASED,
                attempts=Job.attempts + 1,
                leased_by_id=credential_id,
                lease_token_hash=sha256_hex(token),
                lease_expires_at=now + timedelta(seconds=lease_s),
                heartbeat_at=now,
                started_at=func.coalesce(Job.started_at, now),
                updated_at=now,
                # heartbeat/progress bookkeeping lives in ``result`` while the job runs
                result={
                    "worker": worker[:128],
                    "lease_s": lease_s,
                    "progress": 0.0,
                    "claimed_at": _iso(now),
                    "progress_at": _iso(now),
                },
            )
            .returning(Job.id)
        )
        job_id = db.execute(stmt).scalar()
        if job_id is None:
            db.rollback()
            if db.scalar(runnable.limit(1)) is None:
                return None
            continue
        job = db.get(Job, job_id)
        assert job is not None
        alignment = work_alignment(db, job.work_id) if job.work_id is not None else None
        if alignment is not None and alignment.status in (
            AlignmentStatus.PENDING,
            AlignmentStatus.FAILED,
        ):
            alignment.status = AlignmentStatus.RUNNING
        db.commit()
        db.refresh(job)
        return job, token
    return None


def verify_lease(
    db: Session,
    job_id: int,
    token: str | None,
    *,
    credential_id: int | None = None,
    allow_succeeded: bool = False,
) -> Job:
    """The job if ``token`` is its current lease *and* the lease belongs to
    ``credential_id`` (``None`` = the in-process worker); ``LeaseLost`` otherwise. A token is
    a secret, but binding it to the credential that claimed means a leaked token is useless
    with any other worker key. With ``allow_succeeded`` a finished job whose last lease
    matches is returned too (idempotent result upload)."""
    job = db.get(Job, job_id)
    if job is None or not token or job.lease_token_hash is None:
        raise LeaseLost()
    if job.leased_by_id != credential_id:
        raise LeaseLost()
    if not constant_time_equal(job.lease_token_hash, sha256_hex(token)):
        raise LeaseLost()
    if job.status == JobStatus.SUCCEEDED and allow_succeeded:
        return job
    if job.status != JobStatus.LEASED:
        raise LeaseLost()
    if job.lease_expires_at is None or job.lease_expires_at <= utcnow():
        raise LeaseLost()
    return job


def heartbeat(
    db: Session,
    job: Job,
    *,
    progress: float | None = None,
    message: str | None = None,
    stats: AlignStats | dict | None = None,
) -> datetime:
    """Extend the lease. ``stats`` is validated (``ValidationError`` if it does not fit)."""
    now = utcnow()
    lease_s = int((job.result or {}).get("lease_s") or LEASE_MIN_S)
    job.heartbeat_at = now
    job.lease_expires_at = now + timedelta(seconds=lease_s)
    result = dict(job.result or {})
    if progress is not None:
        progress = max(0.0, min(1.0, float(progress)))
        if progress > float(result.get("progress") or 0.0):
            result["progress_at"] = _iso(now)
        result["progress"] = progress
    if message is not None:
        result["message"] = message[:200]
    if stats is not None:
        result["stats"] = (
            stats if isinstance(stats, AlignStats) else AlignStats.model_validate(stats)
        ).stored()
    job.result = result
    db.commit()
    return job.lease_expires_at


# --- failure and reclaim --------------------------------------------------------------------


def release(db: Session, job: Job) -> None:
    """Give a leased job back untouched (the worker is shutting down, not failing): queued
    again at once, and the attempt does not count."""
    job.status = JobStatus.QUEUED
    job.attempts = max(0, job.attempts - 1)
    job.lease_token_hash = None
    job.lease_expires_at = None
    job.heartbeat_at = None
    job.run_after = utcnow()
    job.result = None
    if job.work_id is not None:
        alignment = work_alignment(db, job.work_id)
        if alignment is not None and alignment.status == AlignmentStatus.RUNNING:
            alignment.status = AlignmentStatus.PENDING
    db.commit()


def fail(db: Session, job: Job, error: str, *, retryable: bool) -> dict:
    out = _retry_or_fail(db, job, error, retryable=retryable, now=utcnow())
    db.commit()
    return out


def _retry_or_fail(db: Session, job: Job, error: str, *, retryable: bool, now: datetime) -> dict:
    job.error = error[:2000]
    job.lease_expires_at = None
    job.heartbeat_at = None
    if retryable and job.attempts < job.max_attempts:
        job.status = JobStatus.QUEUED
        job.lease_token_hash = None  # a late upload from the old lease is 409 lease_lost
        job.run_after = now + timedelta(seconds=backoff_s(job.attempts))
        job.result = None
        alignment = work_alignment(db, job.work_id) if job.work_id is not None else None
        if alignment is not None and alignment.status == AlignmentStatus.RUNNING:
            alignment.status = AlignmentStatus.PENDING
        return {"status": "queued", "run_after": job.run_after}
    job.status = JobStatus.FAILED
    job.finished_at = now
    if job.work_id is not None:
        alignment = work_alignment(db, job.work_id)
        if alignment is not None and alignment.status != AlignmentStatus.SUCCEEDED:
            alignment.status = AlignmentStatus.FAILED
            alignment.error = job.error
    return {"status": "failed"}


# --- result ------------------------------------------------------------------------------------


def apply_result(db: Session, data_dir: Path, library: Path, job: Job, body: bytes) -> dict:
    """Validate and apply an uploaded sentence map (contract: Result, checks 2-5)."""
    if job.status == JobStatus.SUCCEEDED:
        return _success_body(job)
    if len(body) > RESULT_MAX_BYTES:
        raise ResultError(413, "too_large")
    try:
        sentence_map = SentenceMap.model_validate_json(body)
    except ValidationError as exc:
        raise ResultError(400, "invalid_map", _errors(exc)) from exc
    except ValueError as exc:
        raise ResultError(400, "invalid_map", [str(exc)]) from exc

    payload = job.payload or {}
    if (
        sentence_map.work_id != job.work_id
        or sentence_map.epub_sha256 != payload.get("epub_sha256")
        or sentence_map.audio_fingerprint != payload.get("audio_fingerprint")
    ):
        raise ResultError(422, "input_mismatch")
    mismatch = _track_mismatch(sentence_map, payload.get("tracks") or [])
    if mismatch:
        raise ResultError(422, "input_mismatch", [mismatch])

    if not inputs_current(db, library, job):
        raise LeaseLost()
    work = db.get(Work, job.work_id)
    found = work_epub(db, library, work)
    lengths = spine_lengths(found[1]) if found else []
    for s in sentence_map.sentences:
        if s.spine_index >= len(lengths) or s.char_end > lengths[s.spine_index]:
            raise ResultError(
                400, "invalid_map", [f"sentence {s.n}: char_end beyond spine item {s.spine_index}"]
            )

    stats = _stored_stats(job.result)
    write_sentence_map(map_path(data_dir, work.id), sentence_map)
    # Moves everyone's stored position from the text-only list onto the aligned one.
    replace_sentences(
        db,
        work.id,
        [
            {
                "n": s.n,
                "spine_index": s.spine_index,
                "char_start": s.char_start,
                "char_end": s.char_end,
                "audio_start_s": s.audio_start_s,
                "audio_end_s": s.audio_end_s,
                "matched": s.matched,
            }
            for s in sentence_map.sentences
        ],
    )
    now = utcnow()
    alignment = work_alignment(db, work.id)
    if alignment is None:
        alignment = Alignment(
            work_id=work.id,
            model=sentence_map.model,
            status=AlignmentStatus.SUCCEEDED,
            epub_sha256=sentence_map.epub_sha256,
            audio_fingerprint=sentence_map.audio_fingerprint,
        )
        db.add(alignment)
    alignment.model = sentence_map.model
    alignment.status = AlignmentStatus.SUCCEEDED
    alignment.coverage = sentence_map.coverage
    alignment.sentence_count = len(sentence_map.sentences)
    alignment.error = None
    alignment.epub_sha256 = sentence_map.epub_sha256
    alignment.audio_fingerprint = sentence_map.audio_fingerprint
    alignment.job_id = job.id
    alignment.completed_at = now
    job.status = JobStatus.SUCCEEDED
    job.finished_at = now
    job.error = None
    job.lease_expires_at = None
    job.result = {
        "sentence_count": len(sentence_map.sentences),
        "coverage": round(sentence_map.coverage, 4),
        "audio_s": round(sentence_map.total_duration_s, 3),
        "elapsed_s": round((now - job.started_at).total_seconds(), 3) if job.started_at else None,
        "stats": stats,
    }
    db.commit()
    return _success_body(job)


def inputs_current(db: Session, library: Path, job: Job) -> bool:
    """Re-check the work's files against the job snapshot right now, not at the next rescan.

    Track rows are synced with the disk first (a ``stat`` per file; only a changed size is
    rehashed). If the inputs moved on, :func:`refresh_work` cancels this job and queues a
    fresh one; that is committed, so the caller's ``409 lease_lost`` reflects the database.
    """
    work = db.get(Work, job.work_id) if job.work_id is not None else None
    if work is None:
        return False
    refresh_work(db, library, work, auto_enqueue=False)
    db.commit()
    db.refresh(job)
    return job.status in JOB_ACTIVE_STATUSES and payload_matches_work(
        db, library, work, job.payload or {}
    )


def _track_mismatch(sentence_map: SentenceMap, expected: list) -> str | None:
    """The map's track list must be the job's: the fingerprint already pins the hashes and
    their order, so this checks what it cannot, the durations and offsets the sentence times
    are laid out on (a worker could otherwise stretch the timeline)."""
    if len(sentence_map.tracks) != len(expected):
        return f"{len(sentence_map.tracks)} tracks, the job has {len(expected)}"
    offset = 0.0
    for got, want in zip(sentence_map.tracks, expected, strict=True):
        duration = float(want.get("duration_s") or 0.0)
        if got.sha256 != want.get("sha256") or got.position != want.get("position"):
            return f"track {got.position}: not the job's track"
        if abs(got.duration_s - duration) > TRACK_TOLERANCE_S:
            return f"track {got.position}: duration {got.duration_s} != {duration}"
        if abs(got.offset_s - offset) > TRACK_TOLERANCE_S:
            return f"track {got.position}: offset {got.offset_s} != {offset:.3f}"
        offset += duration
    return None


def payload_matches_work(db: Session, library: Path, work: Work, payload: dict) -> bool:
    """Does the job's input snapshot still describe the work's current files?"""
    found = work_epub(db, library, work)
    if found is None or found[0].sha256 != payload.get("epub_sha256"):
        return False
    return fingerprint_of(work_tracks(db, work.id)) == payload.get("audio_fingerprint")


def _success_body(job: Job) -> dict:
    result = job.result or {}
    return {
        "job_id": job.id,
        "status": "succeeded",
        "sentence_count": result.get("sentence_count"),
        "coverage": result.get("coverage"),
    }


def _errors(exc: ValidationError) -> list[str]:
    out = []
    for e in exc.errors()[:50]:
        loc = ".".join(str(p) for p in e.get("loc", ()))
        out.append(f"{loc}: {e.get('msg')}" if loc else str(e.get("msg")))
    return out


# --- reporting: JSON shape, ETA ----------------------------------------------------------------


def measured_speed(db: Session, model: str | None) -> tuple[float, int] | None:
    """Median audio-seconds-per-second of recent successful runs (same model first)."""
    rows = db.scalars(
        select(Job)
        .where(Job.kind == KIND_ALIGN, Job.status == JobStatus.SUCCEEDED)
        .order_by(Job.finished_at.desc())
        .limit(50)
    ).all()
    same = [j for j in rows if (j.payload or {}).get("model") == model]
    for pool in (same, rows):
        speeds = []
        for j in pool:
            s = _run_speed(j.result)
            if s is not None:
                speeds.append(s)
            if len(speeds) >= SPEED_SAMPLE_JOBS:
                break
        if speeds:
            return statistics.median(speeds), len(speeds)
    return None


def _stored_stats(result) -> dict:
    """``result["stats"]`` re-validated on the way out; rows from before validation existed
    that do not fit are reported as no stats rather than passed on."""
    stats = result.get("stats") if isinstance(result, dict) else None
    if not isinstance(stats, dict):
        return {}
    try:
        return AlignStats.model_validate(stats).stored()
    except ValidationError:
        return {}


def _sane_speed(value) -> float | None:
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return None
    return speed if math.isfinite(speed) and 0 < speed <= MAX_SPEED else None


def _run_speed(result) -> float | None:
    """A succeeded run's speed, or ``None``. Rows written before stats were validated may
    hold anything (strings, NaN, zero, lists), so every value is checked here too."""
    if not isinstance(result, dict):
        return None
    stats = result.get("stats")
    speed = _sane_speed(stats.get("speed")) if isinstance(stats, dict) else None
    if speed is None:
        try:
            speed = _sane_speed(float(result.get("audio_s")) / float(result.get("elapsed_s")))
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    return speed


def eta(db: Session, job: Job, now: datetime | None = None) -> dict | None:
    """Honest ETA: ``None`` until at least one run has been measured. Assumes one worker
    draining the queue in order."""
    if job.status not in JOB_ACTIVE_STATUSES:
        return None
    measured = measured_speed(db, (job.payload or {}).get("model"))
    if measured is None:
        return _eta_from_this_run(job, now or utcnow())
    speed, samples = measured
    remaining = _payload_duration(job) / speed
    progress = float((job.result or {}).get("progress") or 0.0)
    if job.status == JobStatus.LEASED:
        remaining *= max(0.0, 1.0 - progress)
    else:
        ahead = db.scalars(
            select(Job).where(
                Job.kind == KIND_ALIGN,
                Job.status.in_(JOB_ACTIVE_STATUSES),
                Job.id != job.id,
                (Job.priority < job.priority)
                | ((Job.priority == job.priority) & (Job.created_at <= job.created_at)),
            )
        ).all()
        for other in ahead:
            frac = float((other.result or {}).get("progress") or 0.0)
            remaining += _payload_duration(other) / speed * (1.0 - frac)
    return {
        "seconds": round(remaining),
        "speed": round(speed, 3),
        "basis": f"median of {samples} measured run{'s' if samples != 1 else ''}",
    }


def _eta_from_this_run(job: Job, now: datetime) -> dict | None:
    """Before any run has finished on this server (a first alignment on a new Pi), extrapolate
    a running job from its own measured progress, once there is enough of it to go on."""
    progress = float((job.result or {}).get("progress") or 0.0)
    elapsed = _since(job, "claimed_at", now)
    if job.status != JobStatus.LEASED or progress < 0.05 or not elapsed or elapsed <= 0:
        return None
    return {
        "seconds": round(elapsed * (1.0 - progress) / progress),
        "speed": None,
        "basis": "this run's progress so far",
    }


def _payload_duration(job: Job) -> float:
    return sum(float(t.get("duration_s") or 0.0) for t in (job.payload or {}).get("tracks", []))


def job_json(db: Session, job: Job, workers: dict[int, str] | None = None) -> dict:
    result = job.result or {}
    lease = None
    if job.status == JobStatus.LEASED:
        name = result.get("worker") or (workers or {}).get(job.leased_by_id or -1) or "local"
        lease = {
            "worker": name,
            "expires_at": _iso(job.lease_expires_at),
            "heartbeat_at": _iso(job.heartbeat_at),
            "progress": result.get("progress"),
            "message": result.get("message"),
        }
    done = (
        {
            "sentence_count": result.get("sentence_count"),
            "coverage": result.get("coverage"),
            "stats": _stored_stats(result),
        }
        if job.status == JobStatus.SUCCEEDED
        else None
    )
    return {
        "id": job.id,
        "kind": job.kind,
        "work_id": job.work_id,
        "status": job.status.value,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "priority": job.priority,
        "model": (job.payload or {}).get("model"),
        "audio_s": round(_payload_duration(job), 3),
        "run_after": _iso(job.run_after),
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "lease": lease,
        "error": job.error,
        "result": done,
        "eta": eta(db, job),
    }


def alignment_json(
    db: Session, alignment: Alignment | None, *, detailed: bool = True
) -> dict | None:
    """Alignment state for the API, with the matcher's diagnosis of a low coverage.

    Below 85 % coverage the alignment warns ``low_coverage``. A larger model is suggested
    only when the narrated span itself matched poorly (``poor_transcription``); when the audio
    simply does not cover the whole text (an excerpt, front matter, a licence, missing
    chapters at the ends) or the narration is abridged, a bigger model would not help.
    """
    if alignment is None:
        return None
    warnings = []
    suggestion = None
    stats = {}
    if alignment.job_id is not None:
        job = db.get(Job, alignment.job_id)
        stats = _stored_stats(job.result) if job is not None else {}
    diagnosis = stats.get("diagnosis")
    if alignment.status == AlignmentStatus.SUCCEEDED and alignment.coverage is not None:
        if alignment.coverage < LOW_COVERAGE:
            warnings.append("low_coverage")
            if diagnosis in (None, "poor_transcription", "no_match"):
                suggestion = larger_model(_model_name(alignment.model))
        if stats.get("likely_abridged"):
            warnings.append("likely_abridged")
    if alignment.status == AlignmentStatus.STALE:
        warnings.append("stale")
    return {
        "status": alignment.status.value,
        "model": alignment.model,
        "coverage": alignment.coverage,
        "span_coverage": stats.get("span_coverage"),
        "diagnosis": diagnosis,
        "sentence_count": alignment.sentence_count,
        "error": alignment.error if detailed else None,  # paths and logs: not for readers
        "completed_at": _iso(alignment.completed_at),
        "warnings": warnings,
        "suggested_model": suggestion,
    }


def local_engine_missing(settings) -> str | None:
    """Why this server will not run align jobs itself, or ``None`` when it will
    (the same decision :func:`lectern.services.start_services` makes)."""
    from lectern.align.transcribe import transcription_available

    if not settings.background_jobs:
        return "background_jobs_off"
    if not settings.align_local_worker:
        return "local_worker_off"
    if not transcription_available():
        return "not_installed"
    return None


_MISSING = {
    "not_installed": "This server was built without the alignment engine (LECTERN_EXTRAS=align)",
    "local_worker_off": "The local alignment worker is turned off (LECTERN_ALIGN_LOCAL_WORKER=0)",
    "background_jobs_off": "Background jobs are turned off (LECTERN_BACKGROUND_JOBS=0)",
}


def engine_unavailable(db: Session, settings, now: datetime | None = None) -> dict | None:
    """``None`` when something can run align jobs: the local worker, or an external worker
    that polled in the last ``WORKER_SEEN_S``. Otherwise why not, and where to read how to
    turn alignment on. Queued jobs are left queued: they run as soon as an engine appears."""
    reason = local_engine_missing(settings)
    if reason is None:
        return None
    cutoff = (now or utcnow()) - timedelta(seconds=WORKER_SEEN_S)
    seen = db.scalar(
        select(WorkerCredential.id)
        .where(WorkerCredential.revoked_at.is_(None), WorkerCredential.last_used_at >= cutoff)
        .limit(1)
    )
    if seen is not None:
        return None
    return {
        "reason": reason,
        "message": f"{_MISSING[reason]} and no external worker (lectern worker) has "
        f"connected in the last {WORKER_SEEN_S // 60} minutes, so alignment cannot run.",
        "docs_url": DOCS_URL,
    }


# Retry steps down this ladder after running out of memory (turbo/distil count as large).
_SMALLER = {
    "base": "tiny",
    "small": "base",
    "medium": "small",
    "large-v3": "medium",
    "large-v3-turbo": "medium",
    "distil-large-v3": "medium",
}


def out_of_memory(error: str | None) -> bool:
    """The failure was the memory cap, a MemoryError, or the OOM killer (lectern.align.isolate),
    or a worker reporting that it ran out of memory."""
    text = (error or "").lower()
    return "out of memory" in text or "out-of-memory" in text or "memoryerror" in text


def retry_model(alignment: Alignment | None) -> str | None:
    """The model a plain Retry of a failed alignment runs, or ``None`` when there is no safe
    automatic choice. After running out of memory it is the next smaller model, never the
    one that failed; at ``tiny`` there is none, and a rerun has to be asked for deliberately
    (with other settings or an external worker) from the editor."""
    if alignment is None or alignment.status != AlignmentStatus.FAILED:
        return None
    model = _model_name(alignment.model)
    if out_of_memory(alignment.error):
        return _SMALLER.get(model)
    return model if model in MODELS else None


def alignment_activity(db: Session, work_id: int, settings, *, detailed: bool = True) -> dict:
    """What the work's alignment is doing right now, for the UI: the latest job's heartbeat
    progress, ETA, attempts and error, whether any engine can run a queued job, and the model
    a Retry would run. Without ``detailed`` (readers) the error text is left out: it can hold
    file paths and the alignment process's log."""
    retry = retry_model(work_alignment(db, work_id)) if detailed else None
    job = db.scalar(
        select(Job)
        .where(Job.work_id == work_id, Job.kind == KIND_ALIGN)
        .order_by(Job.id.desc())
        .limit(1)
    )
    if job is None:
        return {"job": None, "unavailable": None, "retry_model": retry}
    result = job.result or {}
    leased = job.status == JobStatus.LEASED
    estimate = eta(db, job)
    return {
        "job": {
            "id": job.id,
            "status": job.status.value,
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
            "progress": result.get("progress") if leased else None,
            "message": result.get("message") if leased else None,
            "heartbeat_at": _iso(job.heartbeat_at),
            "run_after": _iso(job.run_after) if job.status == JobStatus.QUEUED else None,
            "eta_s": estimate["seconds"] if estimate else None,
            "error": job.error if detailed else None,
        },
        "retry_model": retry,
        "unavailable": (
            engine_unavailable(db, settings) if job.status == JobStatus.QUEUED else None
        ),
    }


_COMPUTE_TYPES = ("int8", "int8_float16", "int8_float32", "float16", "float32")


def _model_name(label: str) -> str:
    """``faster-whisper-small-int8`` → ``small``; anything else is returned as is."""
    if label.startswith("faster-whisper-"):
        rest = label[len("faster-whisper-") :]
        for ct in _COMPUTE_TYPES:
            if rest.endswith("-" + ct):
                return rest[: -len(ct) - 1]
        return rest
    return label


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None
