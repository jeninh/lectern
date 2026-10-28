"""Generated audiobooks as durable jobs (``kind = "tts"``), docs/tts.md and docs/contracts.md.

One job narrates one work. Its payload is the agreed wire contract (engine, model, voice,
speed, language, audio format, the script's SHA-256, chapter and sentence counts) plus the
server's own bookkeeping (the EPUB it was made from, the chosen chapters, an ElevenLabs
spending cap). The same queue rules as alignment apply: one active job per work and kind, an
atomic claim that hands out a hashed lease token, heartbeats with progress, backoff between
attempts, cancellation through ``POST /api/jobs/{id}/cancel``.

Whoever runs the job (the server's own runner for Piper/ElevenLabs/local Kokoro, or a worker
that advertises ``tts-<engine>``) uploads one audio file per chapter plus its sentence
timings, then a result. :func:`apply_result` checks it against the script and installs it as
an ordinary audiobook edition: tracks under ``audio/``, one per chapter, with titles, and a
sentence map built from the synthesis timings, so read-along works with no alignment run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from lectern import queue
from lectern.align.jobs import LeaseLost, ResultError, backoff_s
from lectern.align.tracks import sync_tracks, work_tracks
from lectern.config import Settings
from lectern.ingest.library import safe_component
from lectern.ingest.sentences import replace_sentences
from lectern.models import (
    JOB_ACTIVE_STATUSES,
    Alignment,
    AlignmentStatus,
    GeneratedAudiobook,
    IntegrationUsage,
    Job,
    JobStatus,
    Work,
    utcnow,
)
from lectern.pdftext.store import work_text
from lectern.queue import InputFile, KindHandler, register
from lectern.queue import tts_capability as capability
from lectern.security.tokens import new_secret, sha256_hex
from lectern.sentence_map import (
    SentenceEntry,
    SentenceMap,
    TrackRef,
    audio_fingerprint,
    map_path,
    write_sentence_map,
)
from lectern.tts import audio as tts_audio
from lectern.tts.engines import ELEVENLABS

log = logging.getLogger("lectern.tts")

KIND = "tts"
PRIORITY = 20  # after alignment (0) and with transcodes (10) ahead
MAX_ATTEMPTS = 3
LEASE_S = 600
RESULT_MAX_BYTES = 32 * 1024 * 1024
TIME_TOLERANCE_S = 0.05
SPEED_SAMPLE_JOBS = 5
ARTIFACT_RE = r"^chapter-\d{4}\.(m4a|flac)$"


def scripts_dir(data_dir: Path) -> Path:
    return data_dir / "tts" / "scripts"


def script_path(data_dir: Path, sha256: str) -> Path:
    return scripts_dir(data_dir) / f"{sha256}.json"


def active_job(db: Session, work_id: int) -> Job | None:
    return db.scalar(
        select(Job).where(
            Job.work_id == work_id, Job.kind == KIND, Job.status.in_(JOB_ACTIVE_STATUSES)
        )
    )


def latest_job(db: Session, work_id: int) -> Job | None:
    return db.scalar(
        select(Job).where(Job.work_id == work_id, Job.kind == KIND).order_by(Job.id.desc()).limit(1)
    )


def generated(db: Session, work_id: int) -> GeneratedAudiobook | None:
    return db.scalar(select(GeneratedAudiobook).where(GeneratedAudiobook.work_id == work_id))


# --- enqueue -----------------------------------------------------------------------------------


def write_script(data_dir: Path, data: bytes, sha256: str) -> Path:
    path = script_path(data_dir, sha256)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    return path


def new_job(db: Session, work: Work, payload: dict, created_by: int | None) -> Job:
    """Insert a queued tts job (caller has validated ``payload`` and commits)."""
    job = Job(
        kind=KIND,
        work_id=work.id,
        status=JobStatus.QUEUED,
        priority=PRIORITY,
        max_attempts=MAX_ATTEMPTS,
        payload=payload,
        created_by=created_by,
        capability=capability(payload),
    )
    db.add(job)
    db.flush()
    if payload.get("engine") == ELEVENLABS and payload.get("cap_group") is None:
        job.payload = {**payload, "cap_group": job.id}
        db.flush()
    return job


# --- claim, lease, failure (the server's own runner) ---------------------------------------------


def claim_local(
    db: Session,
    engines: set[str],
    *,
    worker: str = "local",
    lease_s: int = LEASE_S,
    defer: set[str] | None = None,
    defer_s: float = 0.0,
    now: datetime | None = None,
) -> tuple[Job, str] | None:
    """Lease the oldest runnable tts job whose engine is in ``engines`` to the server's runner.

    ``defer`` are engines an external worker is online for: their jobs are left to it until
    they have waited ``defer_s`` (the Pi fallback). Same single conditional ``UPDATE`` as
    every other claim, so a worker claiming at the same moment cannot get the same job.
    """
    now = now or utcnow()
    reclaim(db, now)
    if queue.queue_paused(db):
        return None
    rows = db.execute(
        select(Job.id, Job.payload, Job.created_at)
        .where(Job.kind == KIND, Job.status == JobStatus.QUEUED, Job.run_after <= now)
        .order_by(Job.priority, Job.created_at, Job.id)
        .limit(50)
    ).all()
    for job_id, payload, created_at in rows:
        engine = (payload or {}).get("engine")
        if engine not in engines:
            continue
        if defer and engine in defer and (now - created_at).total_seconds() < defer_s:
            continue
        token = new_secret()
        done = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == JobStatus.QUEUED)
            .values(
                status=JobStatus.LEASED,
                attempts=Job.attempts + 1,
                leased_by_id=None,
                lease_token_hash=sha256_hex(token),
                lease_expires_at=now + timedelta(seconds=lease_s),
                heartbeat_at=now,
                started_at=func.coalesce(Job.started_at, now),
                updated_at=now,
                result={
                    "worker": worker[:128],
                    "lease_s": lease_s,
                    "progress": 0.0,
                    "claimed_at": _iso(now),
                    "progress_at": _iso(now),
                },  # fmt: skip
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        db.commit()
        if done == 1:
            job = db.get(Job, job_id)
            db.refresh(job)
            return job, token
    return None


class TTSStats(BaseModel):
    """Heartbeat statistics a tts runner may report (unknown keys dropped)."""

    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    engine: str | None = Field(default=None, max_length=32)
    voice: str | None = Field(default=None, max_length=128)
    sentences_done: int | None = Field(default=None, ge=0, le=10**8)
    characters_done: int | None = Field(default=None, ge=0, le=10**10)
    audio_s: float | None = Field(default=None, ge=0, le=10**7)
    elapsed_s: float | None = Field(default=None, ge=0, le=10**7)
    chars_per_second: float | None = Field(default=None, gt=0, le=10**6)
    credits: float | None = Field(default=None, ge=0, le=10**9)
    chapters_resumed: int | None = Field(default=None, ge=0, le=10**5)


def validated_stats(raw: dict | None) -> dict | None:
    try:
        return TTSStats.model_validate(raw or {}).model_dump(exclude_none=True)
    except ValidationError:
        return None


def heartbeat(
    db: Session, job: Job, *, progress: float | None = None, message: str | None = None,
    stats: dict | None = None,
) -> datetime:  # fmt: skip
    now = utcnow()
    result = dict(job.result or {})
    lease_s = int(result.get("lease_s") or LEASE_S)
    job.heartbeat_at = now
    job.lease_expires_at = now + timedelta(seconds=lease_s)
    if progress is not None:
        progress = max(0.0, min(1.0, float(progress)))
        if progress > float(result.get("progress") or 0.0):
            result["progress_at"] = _iso(now)
        result["progress"] = progress
    if message is not None:
        result["message"] = message[:200]
    if stats is not None and (clean := validated_stats(stats)) is not None:
        result["stats"] = clean
    job.result = result
    db.commit()
    return job.lease_expires_at


def fail(db: Session, job: Job, error: str, *, retryable: bool, crashed: bool = False) -> dict:
    from lectern.align.jobs import count_crash

    if crashed:
        error, retryable = count_crash(job, error, retryable)
    now = utcnow()
    job.error = error[:2000]
    job.lease_expires_at = None
    job.heartbeat_at = None
    if retryable and job.attempts < job.max_attempts:
        job.status = JobStatus.QUEUED
        job.lease_token_hash = None
        job.run_after = now + timedelta(seconds=backoff_s(job.attempts))
        job.result = None
        out = {"status": "queued", "run_after": job.run_after}
    else:
        job.status = JobStatus.FAILED
        job.finished_at = now
        out = {"status": "failed"}
    db.commit()
    return out


def release(db: Session, job: Job) -> None:
    """Hand a leased job back without spending an attempt (the server is stopping)."""
    job.status = JobStatus.QUEUED
    job.attempts = max(0, job.attempts - 1)
    job.lease_token_hash = None
    job.lease_expires_at = None
    job.heartbeat_at = None
    job.run_after = utcnow()
    job.result = None
    db.commit()


def reclaim(db: Session, now: datetime | None = None, *, orphaned_local: bool = False) -> int:
    """Expired tts leases (and, at startup, the previous server process's own) go back to the
    queue with backoff, or fail once attempts are used up. Each change is conditional on the
    lease being the one that was read, so a result or cancel landing in between is kept."""
    now = now or utcnow()
    n = 0
    for job in db.scalars(
        select(Job)
        .where(Job.kind == KIND, Job.status == JobStatus.LEASED)
        .execution_options(populate_existing=True)
    ).all():
        expired = job.lease_expires_at is None or job.lease_expires_at < now
        if not (expired or (orphaned_local and job.leased_by_id is None)):
            continue
        retry = job.attempts < job.max_attempts
        error = "lease expired" if expired else "the server restarted during narration"
        values: dict = {
            "error": error,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "updated_at": now,
        }
        if retry:
            values.update(status=JobStatus.QUEUED, lease_token_hash=None, result=None,
                          run_after=now + timedelta(seconds=backoff_s(job.attempts)))  # fmt: skip
        else:
            values.update(status=JobStatus.FAILED, finished_at=now)
        n += db.execute(
            update(Job)
            .where(
                Job.id == job.id,
                Job.status == JobStatus.LEASED,
                Job.lease_token_hash == job.lease_token_hash,
                Job.heartbeat_at.is_(None)
                if job.heartbeat_at is None
                else Job.heartbeat_at == job.heartbeat_at,  # a heartbeat meanwhile extended it
            )  # fmt: skip
            .values(**values)
            .execution_options(synchronize_session=False)
        ).rowcount
    if n:
        db.commit()
    return n


def cancel(db: Session, job: Job) -> bool:
    """Cancel a tts job. Unlike ``align.jobs.cancel_job`` it leaves the work's alignment
    alone: a narration has nothing to do with an existing alignment row. Caller commits."""
    if job.status not in JOB_ACTIVE_STATUSES:
        return False
    job.status = JobStatus.CANCELLED
    job.lease_token_hash = None
    job.lease_expires_at = None
    job.heartbeat_at = None
    job.finished_at = utcnow()
    db.flush()
    return True


def sweep(db: Session, data_dir: Path, now: datetime | None = None) -> int:
    """Housekeeping: artifacts of tts jobs finished over 7 days ago (a Retry within that week
    resumes from them) and cached ElevenLabs chunks older than 30 days."""
    import shutil
    import time

    now = now or utcnow()
    removed = 0
    root = data_dir / "jobs"
    for d in root.iterdir() if root.is_dir() else []:
        if not d.name.isdigit():
            continue
        job = db.get(Job, int(d.name))
        if job is not None and job.kind != KIND:
            continue  # other kinds manage their own artifacts
        if job is None or (job.status not in JOB_ACTIVE_STATUSES and job.finished_at is not None
                           and now - job.finished_at > timedelta(days=7)):  # fmt: skip
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    cache = data_dir / "tts" / "cache" / "elevenlabs"
    cutoff = time.time() - 30 * 86400
    for f in cache.iterdir() if cache.is_dir() else []:
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass
    return removed


# --- ElevenLabs spending ---------------------------------------------------------------------------


def spent_credits(db: Session, cap_group: int | None) -> float:
    if cap_group is None:
        return 0.0
    return float(
        db.scalar(
            select(func.coalesce(func.sum(IntegrationUsage.credits), 0.0)).where(
                IntegrationUsage.cap_group == cap_group
            )
        )
        or 0.0
    )


def record_usage(
    db: Session, *, provider: str, kind: str, characters: int, credits: float,
    job: Job | None = None, work_id: int | None = None, user_id: int | None = None,
    request_key: str | None = None, state: str = "billed",
) -> IntegrationUsage:  # fmt: skip
    row = IntegrationUsage(
        provider=provider,
        kind=kind,
        cap_group=(job.payload or {}).get("cap_group") if job is not None else None,
        job_id=job.id if job is not None else None,
        work_id=work_id if work_id is not None else (job.work_id if job is not None else None),
        user_id=user_id if user_id is not None else (job.created_by if job is not None else None),
        characters=characters,
        credits=round(credits, 3),
        request_key=request_key,
        state=state,
    )
    db.add(row)
    return row


def reserve_chunk(
    db: Session, job: Job, request_key: str, characters: int, credits: float, cap: float
) -> int:
    """Record an ElevenLabs narration request as ``pending`` before it is sent, after checking
    the job's cap against everything billed or reserved in its cap group. A request this same
    job already sent without a known outcome (a crash, a lost response) is not sent again by
    an automatic retry of the job: only a new job (the owner's Retry) may resend it."""
    from lectern.tts.synth import SpendingCapReached, UncertainCharge

    unsettled = db.scalar(
        select(IntegrationUsage.id).where(
            IntegrationUsage.job_id == job.id,
            IntegrationUsage.request_key == request_key,
            IntegrationUsage.state.in_(("pending", "uncertain")),
        )
    )
    if unsettled is not None:
        raise UncertainCharge(
            "an earlier attempt of this job sent the same ElevenLabs request and its outcome is "
            "unknown (it may have been charged). It is not sent again automatically; check your "
            "ElevenLabs usage, then Retry (it still counts against the cap)"
        )
    group = (job.payload or {}).get("cap_group") or job.id
    spent = spent_credits(db, group)
    if spent + credits > cap + 1e-6:
        raise SpendingCapReached(
            f"the next request ({credits:.0f} credits) would pass the job's cap of "
            f"{cap:.0f} credits ({spent:.0f} already used)"
        )
    row = record_usage(db, provider="elevenlabs", kind=KIND, characters=characters,
                       credits=credits, job=job, request_key=request_key, state="pending")  # fmt: skip
    db.commit()
    return row.id


# --- result --------------------------------------------------------------------------------------

Time = Annotated[float, Field(ge=0, le=10**6)]


class ResultSentence(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    n: int = Field(ge=0)
    start_s: Time
    end_s: Time


class ResultChapter(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    index: int = Field(ge=0)
    title: str = Field(default="", max_length=512)
    artifact: str = Field(pattern=ARTIFACT_RE)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    duration_s: float = Field(gt=0, le=10**6)
    sentences: list[ResultSentence] = Field(min_length=1)


class TTSResult(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    version: Literal[1]
    engine: str = Field(max_length=32)
    model: str = Field(default="", max_length=64)
    voice: str = Field(max_length=128)
    speed: float = Field(default=1.0, gt=0, le=4)
    sample_rate: int = Field(default=24000, ge=8000, le=192000)
    codec: Literal["aac", "flac"]
    chapters: list[ResultChapter] = Field(min_length=1, max_length=5000)


def load_job_script(data_dir: Path, payload: dict) -> dict:
    sha = str(payload.get("script_sha256") or "")
    path = script_path(data_dir, sha)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise LeaseLost() from exc
    if hashlib.sha256(data).hexdigest() != sha:
        raise LeaseLost()
    return json.loads(data)


def check_result(result: TTSResult, payload: dict, script: dict) -> None:
    """Contract checks: same engine/voice/codec, every script sentence exactly once in its
    chapter and in order, times monotone inside each chapter's duration. Raises
    :class:`ResultError` (422). The artifacts are checked when they are pinned."""
    errors: list[str] = []
    codec = (payload.get("audio") or {}).get("codec")
    if result.engine != payload.get("engine") or result.voice != payload.get("voice"):
        raise ResultError(422, "input_mismatch", ["engine or voice differs from the job"])
    if result.codec != codec:
        raise ResultError(
            422, "input_mismatch", [f"codec {result.codec}, the job asks for {codec}"]
        )
    chapters = script.get("chapters") or []
    if len(result.chapters) != len(chapters):
        raise ResultError(
            422,
            "input_mismatch",
            [f"{len(result.chapters)} chapters, the script has {len(chapters)}"],
        )
    ext = tts_audio.EXT[codec]
    for got, want in zip(result.chapters, chapters, strict=True):
        where = f"chapter {want['index']}"
        if got.index != want["index"]:
            errors.append(f"{where}: index {got.index}")
            continue
        if not got.artifact.endswith("." + ext):
            errors.append(f"{where}: artifact must be .{ext}")
        expected = [s["n"] for s in want["sentences"]]
        if [s.n for s in got.sentences] != expected:
            errors.append(f"{where}: sentences differ from the script")
        last = 0.0
        for s in got.sentences:
            if s.end_s < s.start_s or s.start_s < last - 1e-6:
                errors.append(f"{where}: sentence {s.n} goes backwards")
                break
            if s.end_s > got.duration_s + TIME_TOLERANCE_S:
                errors.append(f"{where}: sentence {s.n} ends after the audio")
                break
            last = s.start_s
        if len(errors) > 50:
            break
    if errors:
        raise ResultError(422, "invalid_result", errors[:50])


def apply_result(db: Session, settings: Settings, job: Job, body: bytes, artifacts: Path) -> dict:
    """Validate a tts result and install the edition (``KindHandler.apply_result``)."""
    if job.status == JobStatus.SUCCEEDED:
        return _success_body(job)
    if len(body) > RESULT_MAX_BYTES:
        raise ResultError(413, "too_large")
    try:
        result = TTSResult.model_validate_json(body)
    except ValidationError as exc:
        raise ResultError(
            400, "invalid_result", [str(e.get("msg")) for e in exc.errors()[:20]]
        ) from exc
    # The lease that submitted this result, as it is now: publication is fenced by exactly it.
    lease = SimpleNamespace(id=job.id, lease_token_hash=job.lease_token_hash,
                            leased_by_id=job.leased_by_id)  # fmt: skip
    payload = job.payload or {}
    script = load_job_script(settings.data_dir, payload)
    check_result(result, payload, script)
    stage = queue.staging_dir(settings.library_dir, job.id, new_secret()[:8])
    try:
        stage.mkdir(parents=True)
        staged = [_pin_chapter(artifacts, stage, ch) for ch in result.chapters]
        return publish(db, settings, job, lease, result, script, staged)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _pin_chapter(artifacts: Path, stage: Path, ch: ResultChapter) -> Path:
    """Copy a chapter artifact into staging, hashing that same read, and probe the copy: the
    bytes checked are the bytes published, and they must be audio as long as declared."""
    from lectern.align.tracks import probe
    from lectern.jobs import _pin

    copy = stage / ch.artifact
    try:
        digest = _pin(artifacts / ch.artifact, copy)
    except FileNotFoundError:
        digest = None
    if digest != ch.sha256:
        raise ResultError(422, "missing_artifact", [ch.artifact])
    duration = probe(copy)[0]
    if duration <= 0 or abs(duration - ch.duration_s) > max(0.5, 0.01 * ch.duration_s):
        raise ResultError(
            422, "bad_audio", [f"{ch.artifact}: {duration:.2f} s, declared {ch.duration_s:.2f} s"]
        )
    return copy


def _current_inputs(db: Session, settings: Settings, job: Job) -> tuple[Work, Path]:
    work = db.get(Work, job.work_id) if job.work_id is not None else None
    if work is None or work.trashed_at is not None:
        raise LeaseLost()
    found = work_text(db, settings.library_dir, work)
    if found is None or found[0].sha256 != (job.payload or {}).get("epub_sha256"):
        raise LeaseLost()
    sync_tracks(db, settings.library_dir, work)
    if work_tracks(db, work.id):
        raise LeaseLost()  # an audiobook arrived meanwhile; never mix editions
    return work, found[1]


def publish(
    db: Session,
    settings: Settings,
    job: Job,
    lease: SimpleNamespace,
    result: TTSResult,
    script: dict,
    staged: list[Path],
) -> dict:
    """Copy the pinned chapter files into the work's ``audio/`` folder, make them tracks, and
    install the synthesis timings as the work's sentence map, all under the library lock,
    holding ``lease`` (``queue.hold_lease``: the database write lock on that exact, unexpired
    lease), in one transaction (files are removed again if it fails). A lease released,
    reclaimed, revoked, or cancelled meanwhile publishes nothing (``LeaseLost``)."""
    from lectern.align.text import iter_spine_items, segment_items
    from lectern.fsops import LIBRARY_LOCK, unique_name

    payload = job.payload or {}
    with LIBRARY_LOCK:
        if not queue.hold_lease(db, lease):
            raise LeaseLost()
        work, epub = _current_inputs(db, settings, job)
        sentences = segment_items(iter_spine_items(epub))
        by_n = {s.n: s for s in sentences}
        if any(s["n"] not in by_n for c in script["chapters"] for s in c["sentences"]):
            raise LeaseLost()  # the EPUB's text no longer has these sentences
        audio_dir = settings.library_dir / work.dir_path / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        ext = tts_audio.EXT[result.codec]
        placed: list[Path] = []
        try:
            taken: list[str] = []
            for i, (ch, pinned) in enumerate(zip(result.chapters, staged, strict=True)):
                stem = safe_component(
                    ch.title or script["chapters"][i]["title"], f"Chapter {i + 1}"
                )
                name = unique_name(audio_dir, f"D01-T{i + 1:03d} {stem[:80]}.{ext}", taken)
                taken.append(name)
                dest = audio_dir / name
                tmp = dest.with_name(f".{name}.tmp")
                with pinned.open("rb") as src, tmp.open("wb") as out:
                    while block := src.read(1 << 20):
                        out.write(block)
                os.replace(tmp, dest)
                placed.append(dest)
            sync_tracks(db, settings.library_dir, work)
            tracks = work_tracks(db, work.id)
            if len(tracks) != len(result.chapters):
                raise RuntimeError("the chapter files did not all become tracks")
            for t, ch, sc in zip(tracks, result.chapters, script["chapters"], strict=True):
                t.title = (ch.title or sc["title"])[:256]
                t.chapters = [{"title": t.title[:200], "start_s": 0.0}]
            timing: dict[int, tuple[float, float]] = {}
            for t, ch in zip(tracks, result.chapters, strict=True):
                limit = t.offset_s + t.duration_s
                for s in ch.sentences:
                    a = min(t.offset_s + s.start_s, limit)
                    timing[s.n] = (round(a, 3), round(max(a, min(t.offset_s + s.end_s, limit)), 3))
            entries = [
                SentenceEntry(
                    n=s.n,
                    spine_index=s.spine_index,
                    char_start=s.char_start,
                    char_end=s.char_end,
                    audio_start_s=timing[s.n][0] if s.n in timing else None,
                    audio_end_s=timing[s.n][1] if s.n in timing else None,
                    matched=s.n in timing,
                )  # fmt: skip
                for s in sentences
            ]
            fingerprint = audio_fingerprint(t.sha256 for t in tracks)
            label = f"tts-{result.engine}-{result.voice}"[:64]
            smap = SentenceMap(
                version=1,
                work_id=work.id,
                epub_sha256=payload["epub_sha256"],
                audio_fingerprint=fingerprint,
                model=label,
                language=payload.get("lang") or work.language,
                generated_at=utcnow(),
                tracks=[
                    TrackRef(
                        position=t.position,
                        sha256=t.sha256,
                        duration_s=t.duration_s,
                        offset_s=t.offset_s,
                    )
                    for t in tracks
                ],  # fmt: skip
                sentences=entries,
            )
            write_sentence_map(map_path(settings.data_dir, work.id), smap)
            replace_sentences(db, work.id, [e.model_dump(exclude={"text"}) for e in entries])
            now = utcnow()
            alignment = db.scalar(select(Alignment).where(Alignment.work_id == work.id))
            if alignment is None:
                alignment = Alignment(work_id=work.id, model=label, status=AlignmentStatus.SUCCEEDED,
                                      epub_sha256=smap.epub_sha256, audio_fingerprint=fingerprint)  # fmt: skip
                db.add(alignment)
            alignment.model = label
            alignment.status = AlignmentStatus.SUCCEEDED
            alignment.coverage = smap.coverage
            alignment.sentence_count = len(entries)
            alignment.error = None
            alignment.epub_sha256 = smap.epub_sha256
            alignment.audio_fingerprint = fingerprint
            alignment.job_id = job.id
            alignment.completed_at = now
            duration = round(sum(t.duration_s for t in tracks), 3)
            edition = generated(db, work.id) or GeneratedAudiobook(work_id=work.id)
            edition.job_id = job.id
            edition.engine = result.engine
            edition.model = (result.model or payload.get("model") or "")[:64]
            edition.voice = result.voice
            edition.speed = float(payload.get("speed") or 1.0)
            edition.settings = {
                k: payload[k] for k in ("voice_settings", "audio", "lang") if k in payload
            }
            edition.chapters = len(tracks)
            edition.characters = int(payload.get("characters") or 0)
            edition.duration_s = duration
            edition.epub_sha256 = smap.epub_sha256
            edition.audio_fingerprint = fingerprint
            edition.created_by = job.created_by
            edition.created_at = now
            db.add(edition)
            prior = dict(job.result or {})
            job.status = JobStatus.SUCCEEDED
            job.finished_at = now
            job.error = None
            job.lease_expires_at = None
            job.result = {
                "chapters": len(tracks),
                "duration_s": duration,
                "sentences": sum(len(c.sentences) for c in result.chapters),
                "coverage": round(smap.coverage, 4),
                "elapsed_s": round((now - job.started_at).total_seconds(), 3)
                if job.started_at
                else None,
                "stats": prior.get("stats") or {},
            }
            db.commit()
        except Exception:
            db.rollback()
            for p in placed:
                p.unlink(missing_ok=True)
            raise
    log.info(
        "tts job %d: %d chapters, %.0f s of audio for work %d",
        job.id,
        len(placed),
        duration,
        work.id,
    )
    return _success_body(job)


def _success_body(job: Job) -> dict:
    r = job.result or {}
    return {"job_id": job.id, "status": "succeeded", "chapters": r.get("chapters"),
            "duration_s": r.get("duration_s"), "coverage": r.get("coverage")}  # fmt: skip


# --- reporting -------------------------------------------------------------------------------------


def measured_cps(db: Session, engine: str | None) -> tuple[float, int] | None:
    """Median characters narrated per wall-clock second over recent successful jobs (same
    engine first): the basis of the ETA, measured on this server's own runs."""
    rows = db.scalars(
        select(Job)
        .where(Job.kind == KIND, Job.status == JobStatus.SUCCEEDED)
        .order_by(Job.finished_at.desc())
        .limit(50)  # fmt: skip
    ).all()
    same = [j for j in rows if (j.payload or {}).get("engine") == engine]
    for pool in (same, rows):
        speeds = []
        for j in pool:
            elapsed = (j.result or {}).get("elapsed_s")
            chars = (j.payload or {}).get("characters")
            try:
                cps = float(chars) / float(elapsed)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            if math.isfinite(cps) and cps > 0:
                speeds.append(cps)
            if len(speeds) >= SPEED_SAMPLE_JOBS:
                break
        if speeds:
            return statistics.median(speeds), len(speeds)
    return None


def eta(db: Session, job: Job, now: datetime | None = None) -> dict | None:
    if job.status not in JOB_ACTIVE_STATUSES:
        return None
    now = now or utcnow()
    payload = job.payload or {}
    progress = float((job.result or {}).get("progress") or 0.0)
    measured = measured_cps(db, payload.get("engine"))
    if measured is None:
        claimed = (job.result or {}).get("claimed_at")
        if job.status != JobStatus.LEASED or progress < 0.05 or not claimed:
            return None
        elapsed = (now - datetime.fromisoformat(claimed.replace("Z", "+00:00"))).total_seconds()
        if elapsed <= 0:
            return None
        return {"seconds": round(elapsed * (1 - progress) / progress), "speed": None,
                "basis": "this run's progress so far"}  # fmt: skip
    cps, samples = measured
    remaining = float(payload.get("characters") or 0) / cps * (1 - progress)
    return {
        "seconds": round(remaining),
        "speed": round(cps, 2),
        "basis": f"median of {samples} measured run{'s' if samples != 1 else ''} (characters per second)",
    }


def job_json(
    db: Session,
    job: Job,
    workers: dict[int, str] | None = None,
    *,
    detailed: bool = True,
    server_caps: set[str] | None = None,
    for_owner: bool = False,
) -> dict:
    p = job.payload or {}
    r = job.result or {}
    lease = None
    if job.status == JobStatus.LEASED:
        lease = {
            "worker": r.get("worker") or (workers or {}).get(job.leased_by_id or -1) or "local",
            "expires_at": _iso(job.lease_expires_at),
            "heartbeat_at": _iso(job.heartbeat_at),
            "progress": r.get("progress"),
            "message": r.get("message"),
        }
    return {
        "id": job.id,
        "kind": job.kind,
        "capability": capability(p),
        "work_id": job.work_id,
        "status": job.status.value,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "crashes": job.crashes or 0,
        "priority": job.priority,
        "engine": p.get("engine"),
        "model": p.get("model"),
        "voice": p.get("voice"),
        "speed": p.get("speed"),
        "chapters": p.get("chapters"),
        "sentences": p.get("sentences"),
        "characters": p.get("characters"),
        "cap_credits": p.get("cap_credits"),
        "spent_credits": round(spent_credits(db, p.get("cap_group")), 3)
        if p.get("engine") == ELEVENLABS
        else None,
        "run_after": None if queue.is_paused(job) else _iso(job.run_after),
        "paused": queue.is_paused(job),
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "lease": lease,
        "progress": r.get("progress")
        if job.status == JobStatus.LEASED
        else (1.0 if job.status == JobStatus.SUCCEEDED else None),
        "message": r.get("message") if job.status == JobStatus.LEASED else None,
        "error": job.error if detailed else (None if job.error is None else "failed"),
        "result": _success_body(job) if job.status == JobStatus.SUCCEEDED else None,
        "eta": eta(db, job),
        "waiting": queue.explain_wait(db, job, server_caps=server_caps, for_owner=for_owner)
        if detailed
        else None,
    }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None


# --- external workers (lectern.queue, docs/contracts.md "Kind handlers") --------------------------


def worker_inputs(db: Session, settings: Settings, job: Job) -> dict | None:
    """``{"script": InputFile}`` for a worker holding the lease, or ``None`` when the work
    changed under the job (its EPUB was replaced, or it gained an audiobook meanwhile)."""
    payload = job.payload or {}
    work = db.get(Work, job.work_id) if job.work_id is not None else None
    found = work_text(db, settings.library_dir, work) if work is not None else None
    if found is None or found[0].sha256 != payload.get("epub_sha256") or work_tracks(db, work.id):
        return None
    path = script_path(settings.data_dir, str(payload.get("script_sha256") or ""))
    if not path.is_file():
        return None
    return {"script": InputFile(path, "application/json", payload["script_sha256"])}


# Workers that advertise ``tts-<engine>`` may claim narrations; ``tts-elevenlabs`` is
# server-only in lectern.queue, so the ElevenLabs key never leaves the server.
register(
    KindHandler(
        kind=KIND,
        capability=capability,
        remote=True,
        inputs=worker_inputs,
        apply_result=apply_result,
        stats=validated_stats,
        keeps_artifacts=True,  # Retry within a week resumes from finished chapters (sweep())
    )
)
