"""Job kinds, worker capabilities, and routing (docs/contracts.md, "Capabilities and routing").

Every job has a ``capability``: what a worker must advertise to claim it. It is derived from
the job's kind and payload when the job is inserted (``capability_for``): ``align`` →
``align`` (``align-pdf`` when its text is a PDF's), ``transcode`` → ``transcode``, ``ocr`` → ``ocr``, ``tts`` → ``tts-<payload.engine>``. Workers send
the capabilities they have on every claim; the server leases a job only to a worker that
advertises its capability, that the credential's owner-set allowlist permits, and that may
leave the server at all (``SERVER_ONLY`` never does).

A kind that external workers may run registers a :class:`KindHandler` here. It says which
named input files a lease may download and how an uploaded result is checked and applied.
``align`` keeps its own routes and result path in :mod:`lectern.align.jobs`; it is
registered so the claim and presence rules treat it like any other kind.

Pi fallback: the server's own runners claim through the same rules with
``credential_id=None``. While an external worker that advertises a capability is online and
not paused (:func:`capability_online`), local runners leave that capability's jobs to it for
``LECTERN_LOCAL_DEFER_SECONDS`` after they were queued, then take them anyway.
"""

from __future__ import annotations

import re
import shutil
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from sqlalchemy import func, select

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from lectern.config import Settings
    from lectern.models import Job, WorkerCredential

CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

ALIGN = "align"
# Alignment whose text is a PDF's canonical text document (a ``text`` input instead of an
# EPUB). Workers from before PDF text do not advertise it, so they never claim such a job; a
# credential allowed to ``align`` may also ``align-pdf`` (the same engine).
ALIGN_PDF = "align-pdf"
TRANSCODE = "transcode"
OCR = "ocr"  # text recognition of scanned PDFs (lectern.ocr.jobs)
TTS_KOKORO = "tts-kokoro"
TTS_PIPER = "tts-piper"
TTS_ELEVENLABS = "tts-elevenlabs"
KNOWN_CAPABILITIES = (ALIGN, ALIGN_PDF, TRANSCODE, OCR, TTS_KOKORO, TTS_PIPER, TTS_ELEVENLABS)
# Never leased to an external worker: the job needs a secret only the server holds (the
# ElevenLabs integration key). Local runners may still claim them.
SERVER_ONLY = frozenset({TTS_ELEVENLABS})

# An external worker that called in this recently counts as online (same window as the
# "alignment cannot run" notice in lectern.align.jobs).
WORKER_SEEN_S = 300
# States a worker reports; "paused" and "off" workers do not count as online.
STATES = ("ready", "busy", "paused", "off")
IDLE_STATES = frozenset({"paused", "off"})


class InputFile(NamedTuple):
    path: Path
    media_type: str
    sha256: str


@dataclass(frozen=True)
class KindHandler:
    """How a job kind runs on external workers.

    - ``capability(payload)``: the capability a job of this kind needs.
    - ``remote``: whether external workers may claim it at all.
    - ``inputs(db, settings, job)``: ``{name: InputFile}`` a lease may download from
      ``GET /api/worker/jobs/{id}/inputs/{name}``; checked against the job's snapshot by the
      handler (return ``None`` when the work changed: the caller answers ``409 lease_lost``).
    - ``apply_result(db, settings, job, body, artifacts_dir)``: validate and apply the body of
      ``PUT …/result``; return the response JSON. Raise :class:`lectern.align.jobs.ResultError`
      for a bad result, :class:`lectern.align.jobs.LeaseLost` for a stale one. Called for an
      already succeeded job too (idempotent replay with the same lease): return the same body.
    - ``on_fail(db, job, terminal)``: side effects when a lease fails or expires (optional).
    - ``stats(raw)``: validated heartbeat ``stats`` to store, or ``None`` to drop them.
    """

    kind: str
    capability: Callable[[dict], str]
    remote: bool = True
    inputs: Callable[[Session, Settings, Job], dict[str, InputFile] | None] | None = None
    apply_result: Callable[[Session, Settings, Job, bytes, Path], dict] | None = None
    on_fail: Callable[[Session, Job, bool], None] | None = None
    stats: Callable[[dict], dict | None] | None = None
    # Artifacts kept after the job finishes (a later Retry resumes from them); the kind's own
    # housekeeping removes them. ``sweep_artifacts`` leaves them alone.
    keeps_artifacts: bool = False


_HANDLERS: dict[str, KindHandler] = {}


def register(handler: KindHandler) -> None:
    _HANDLERS[handler.kind] = handler


def handler(kind: str) -> KindHandler | None:
    _load_builtin()
    return _HANDLERS.get(kind)


def remote_kinds() -> list[str]:
    _load_builtin()
    return sorted(k for k, h in _HANDLERS.items() if h.remote)


def _load_builtin() -> None:
    # Imported lazily: the built-in handlers import lectern.align, which imports models.
    if ALIGN not in _HANDLERS:
        import lectern.align.jobs  # noqa: F401 - registers "align"
        import lectern.article.jobs  # noqa: F401 - registers "article" (server-only)
        import lectern.jobs  # noqa: F401 - registers "transcode"
        import lectern.ocr.jobs  # noqa: F401 - registers "ocr" (scanned PDFs)
        import lectern.pdf2epub.jobs  # noqa: F401 - registers "pdf2epub" (server-only)
        import lectern.storage  # noqa: F401 - registers "storage-cleanup" (server-only)

        try:
            import lectern.tts.jobs  # noqa: F401 - registers "tts" (generated audiobooks)
        except ImportError:
            pass


def tts_capability(payload: dict | None) -> str:
    engine = str((payload or {}).get("engine") or "").strip().lower()
    return f"tts-{engine}" if engine else "tts"


def capability_for(kind: str, payload: dict | None) -> str:
    """The capability a job needs. Registered kinds decide; ``tts`` is ``tts-<engine>``
    even before its handler is registered; anything else needs its kind."""
    found = _HANDLERS.get(kind)
    if found is not None:
        return found.capability(payload or {})
    if kind == "tts":
        return tts_capability(payload)
    return kind


def valid_capabilities(values: Iterable[str]) -> list[str]:
    """Deduplicated, validated capability tokens (``ValueError`` on a malformed one).
    Unknown but well-formed tokens are kept: the vocabulary is open."""
    out: list[str] = []
    for v in values:
        if not isinstance(v, str) or not CAPABILITY_RE.match(v):
            raise ValueError(f"invalid capability {v!r}")
        if v not in out:
            out.append(v)
    return out


def claimable(advertised: Iterable[str], cred: WorkerCredential | None) -> list[str]:
    """What a claim may take: the advertised capabilities, narrowed by the credential's
    allowlist; server-only capabilities never go to an external worker."""
    caps = list(dict.fromkeys(advertised))
    if cred is None:
        return caps
    allowed = cred.capabilities
    if allowed is not None and ALIGN in allowed:
        allowed = [*allowed, ALIGN_PDF]
    return [c for c in caps if c not in SERVER_ONLY and (allowed is None or c in allowed)]


# --- pausing (the job queue page, lectern.jobqueue) --------------------------------------------

# A paused job stays ``queued`` with its ``run_after`` this far ahead: every claim takes only
# jobs whose ``run_after`` has passed, so none starts it, and it still holds its work's one
# live job of its kind. Resuming sets ``run_after`` back to now.
PAUSED_UNTIL = datetime(9000, 1, 1, tzinfo=UTC)


def is_paused(job: Job) -> bool:
    from lectern.models import JobStatus

    return (
        job.status == JobStatus.QUEUED
        and job.run_after is not None
        and job.run_after >= PAUSED_UNTIL
    )


def queue_paused(db: Session) -> bool:
    """The owner paused the whole queue: no runner or worker claims a new job (running ones
    finish). Every claim checks it."""
    from lectern import instance

    return instance.get(db, "queue_paused")


# --- presence ---------------------------------------------------------------------------------


def record_presence(
    cred: WorkerCredential,
    *,
    state: str,
    advertised: list[str] | None = None,
    info: dict | None = None,
    now: datetime | None = None,
) -> None:
    """Store what the worker said about itself (caller commits)."""
    from lectern.models import utcnow

    now = now or utcnow()
    cred.state = state
    cred.status_at = now
    cred.last_used_at = now
    if advertised is not None:
        cred.advertised = advertised
    if info is not None:
        cred.info = info


def online_workers(db: Session, now: datetime | None = None) -> list[WorkerCredential]:
    """Active credentials that called in within ``WORKER_SEEN_S`` and are not paused."""
    from lectern.models import WorkerCredential, utcnow

    cutoff = (now or utcnow()) - timedelta(seconds=WORKER_SEEN_S)
    rows = db.scalars(
        select(WorkerCredential).where(
            WorkerCredential.revoked_at.is_(None), WorkerCredential.last_used_at >= cutoff
        )
    ).all()
    return [c for c in rows if (c.state or "ready") not in IDLE_STATES]


def capability_online(db: Session, capability: str, now: datetime | None = None) -> bool:
    """Is an external worker that can take ``capability`` jobs online and not paused?

    A credential that has never advertised (a worker from before capabilities) is taken to
    run ``align`` only, which is all it could claim."""
    if capability in SERVER_ONLY:
        return False
    for cred in online_workers(db, now):
        advertised = cred.advertised if cred.advertised is not None else [ALIGN]
        if capability in claimable(advertised, cred):
            return True
    return False


def deferred_capabilities(
    db: Session, capabilities: Iterable[str], now: datetime | None = None
) -> set[str]:
    """Of a local runner's capabilities, the ones an external worker is online for. Only
    jobs of kinds external workers may claim (``remote_kinds``) are ever left to them: the
    claim applies that part (``lectern.align.jobs.claim``)."""
    return {c for c in capabilities if capability_online(db, c, now)}


# --- artifacts --------------------------------------------------------------------------------


def artifacts_dir(data_dir: Path, job_id: int) -> Path:
    """Where a job's uploaded artifacts live. They survive a retry (a new attempt resumes
    from what an earlier one uploaded) and are removed once the job is finished."""
    return data_dir / "jobs" / str(int(job_id)) / "artifacts"


def sweep_artifacts(db: Session, data_dir: Path) -> int:
    """Delete the artifact directories of finished or vanished jobs (housekeeping)."""
    from lectern.models import JOB_ACTIVE_STATUSES, Job

    root = data_dir / "jobs"
    if not root.is_dir():
        return 0
    removed = 0
    for d in root.iterdir():
        if not d.is_dir() or not d.name.isdigit():
            continue
        job = db.get(Job, int(d.name))
        if job is not None and (found := handler(job.kind)) is not None and found.keeps_artifacts:
            continue
        if job is None or job.status not in JOB_ACTIVE_STATUSES:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


# Remote results are copied here (same filesystem as the library) before they are renamed into
# a work's folder: outside every work folder, so a work renamed or trashed meanwhile cannot
# strand them, and dot-named, so no library scan or backup listing treats them as books.
STAGING_DIR = ".lectern-staging"
STAGING_MAX_AGE = timedelta(hours=6)


def staging_dir(library: Path, job_id: int, tag: str) -> Path:
    return library / STAGING_DIR / f"{int(job_id)}-{tag}"


def sweep_staging(db: Session, library: Path, *, everything: bool = False) -> int:
    """Remove staged copies of finished jobs, and any older than ``STAGING_MAX_AGE`` (a
    result being published right now is younger and its job still active). ``everything`` at
    startup, when no publication can be in flight."""
    import time

    from lectern.models import JOB_ACTIVE_STATUSES, Job

    root = library / STAGING_DIR
    if not root.is_dir():
        return 0
    removed = 0
    for d in root.iterdir():
        job_id = d.name.split("-", 1)[0]
        if not everything:
            job = db.get(Job, int(job_id)) if job_id.isdigit() else None
            try:
                young = time.time() - d.lstat().st_mtime < STAGING_MAX_AGE.total_seconds()
            except OSError:
                continue
            if young and job is not None and job.status in JOB_ACTIVE_STATUSES:
                continue
        if d.is_dir() and not d.is_symlink():
            shutil.rmtree(d, ignore_errors=True)
        else:
            d.unlink(missing_ok=True)
        removed += 1
    return removed


# The worker credential that uploaded an artifact, next to it (dot-named: workers cannot
# address it, listings skip it). Revoking a credential deletes what it uploaded.
def uploader_path(artifact: Path) -> Path:
    return artifact.with_name(f".{artifact.name}.uploader")


def drop_artifacts_of(data_dir: Path, credential_id: int) -> set[int]:
    """Delete every stored artifact a (revoked) credential uploaded, with its sidecars.
    Returns the ids of the jobs that lost artifacts: a lease on one of them (another
    worker's, resuming from those files) must start over (``requeue_leases(job_ids=…)``)."""
    root = data_dir / "jobs"
    if not root.is_dir():
        return set()
    removed: set[int] = set()
    for marker in root.glob("*/artifacts/.*.uploader"):
        try:
            owner = marker.read_text()[:32].strip()
        except OSError:
            continue
        if owner != str(int(credential_id)):
            continue
        name = marker.name[1 : -len(".uploader")]
        artifact = marker.with_name(name)
        artifact.with_name(name + ".sha256").unlink(missing_ok=True)
        artifact.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        job_dir = marker.parent.parent.name
        if job_dir.isdigit():
            removed.add(int(job_dir))
    return removed


def hold_lease(db: Session, job: Job, now: datetime | None = None) -> bool:
    """Write-lock the job row while it is still exactly ``job``'s unexpired lease.

    A conditional ``UPDATE`` on id, status, token hash, holder, and expiry; on SQLite it holds
    the database write lock until the caller commits or rolls back, so no release, claim, or
    reclaim can change the lease in between. Returns ``False`` (nothing held) when the lease
    is gone. The caller does its filesystem step, then commits."""
    from sqlalchemy import update

    from lectern.models import Job, JobStatus, utcnow

    now = now or utcnow()
    held = db.execute(
        update(Job)
        .where(
            Job.id == job.id,
            Job.status == JobStatus.LEASED,
            Job.lease_token_hash == job.lease_token_hash,
            Job.leased_by_id.is_(None)
            if job.leased_by_id is None
            else Job.leased_by_id == job.leased_by_id,
            Job.lease_expires_at > now,
        )
        .values(updated_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    if held != 1:
        db.rollback()
        return False
    # The UPDATE may have waited for the write lock; the lease must still be unexpired now.
    from sqlalchemy import select

    expires = db.scalar(select(Job.lease_expires_at).where(Job.id == job.id))
    if expires is None or expires <= utcnow():
        db.rollback()
        return False
    return True


def cancel_row(db: Session, job: Job) -> bool:
    """Move a live job to ``cancelled`` and drop its lease; ``False`` when it is no longer live.

    Conditional on the row, not this session's copy: a job that finished in another session
    after this one read it stays finished (its output is published). Caller commits."""
    from sqlalchemy import update

    from lectern.models import JOB_ACTIVE_STATUSES, Job, JobStatus, utcnow

    now = utcnow()
    done = db.execute(
        update(Job)
        .where(Job.id == job.id, Job.status.in_(JOB_ACTIVE_STATUSES))
        .values(
            status=JobStatus.CANCELLED,
            lease_token_hash=None,
            lease_expires_at=None,
            heartbeat_at=None,
            finished_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    db.expire(job)
    return done == 1


class ArtifactBudget:
    """In-process reservations for artifact uploads in flight: bytes and slots per job, taken
    atomically before the body is read and given back when the request ends (a crash of the
    server drops them with the process)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bytes: dict[int, int] = {}
        self._slots: dict[int, int] = {}

    def reserve(
        self, job_id: int, size: int, on_disk: Callable[[], int], budget: int, slots: int
    ) -> str | None:
        """``None`` when reserved, else why not (``too_large`` / ``too_many_uploads``).
        ``on_disk()`` is measured inside the lock, so an upload that finished just before
        is counted and two reservations never both see the same free space."""
        with self._lock:
            if self._slots.get(job_id, 0) >= slots:
                return "too_many_uploads"
            if on_disk() + self._bytes.get(job_id, 0) + size > budget:
                return "too_large"
            self._slots[job_id] = self._slots.get(job_id, 0) + 1
            self._bytes[job_id] = self._bytes.get(job_id, 0) + size
            return None

    def release(self, job_id: int, size: int) -> None:
        with self._lock:
            self._slots[job_id] -= 1
            self._bytes[job_id] -= size
            if self._slots[job_id] <= 0:
                self._slots.pop(job_id, None)
                self._bytes.pop(job_id, None)


ARTIFACT_BUDGET = ArtifactBudget()


# --- why a job is waiting ---------------------------------------------------------------------


def _human_time(at: datetime, now: datetime) -> str:
    s = max(0, round((at - now).total_seconds()))
    return f"{s} s" if s < 90 else f"{round(s / 60)} min" if s < 5400 else f"{s / 3600:.1f} h"


def explain_wait(
    db: Session,
    job: Job,
    *,
    server_caps: set[str] | None = None,
    for_owner: bool = True,
    show_error: bool = True,
    now: datetime | None = None,
) -> dict | None:
    """Why an active job is where it is, for the owner (Admin → Workers, the job detail):
    the capability it needs, every worker that could take it and why it does not (offline,
    paused, not offering it and why, not allowed by its allowlist, busy), the lease holder and
    expiry, and ``run_after`` (backoff). ``server_caps``: capabilities this server runs
    itself, when known. ``None`` for a finished job. Without ``for_owner`` (a member) the
    workers, their labels, and their reasons are left out, as on Admin → Workers. Without
    ``show_error`` the job's error (it can hold library paths) is left out of the summary and
    ``last_error``. A paused job (:func:`is_paused`) says so, with no ``run_after``."""
    from lectern.models import JOB_ACTIVE_STATUSES, Job, JobStatus, WorkerCredential, utcnow

    if job.status not in JOB_ACTIVE_STATUSES:
        return None
    now = now or utcnow()
    cap = job.capability
    cutoff = now - timedelta(seconds=WORKER_SEEN_S)
    busy = {
        holder: job_id
        for job_id, holder in db.execute(
            select(Job.id, Job.leased_by_id).where(
                Job.status == JobStatus.LEASED, Job.leased_by_id.is_not(None)
            )
        ).all()
    }
    workers = []
    ready = []
    for c in db.scalars(
        select(WorkerCredential)
        .where(WorkerCredential.revoked_at.is_(None))
        .order_by(WorkerCredential.id)
    ).all():
        online = c.last_used_at is not None and c.last_used_at >= cutoff
        state = (c.state or "ready") if online else "offline"
        advertised = c.advertised if c.advertised is not None else [ALIGN]
        info = c.info or {}
        if cap in SERVER_ONLY:
            why = "this kind of job never leaves the server"
        elif not claimable([cap], c):
            why = f"its allowlist does not include {cap}"
        elif cap not in advertised:
            missing = (info.get("unavailable") or {}).get(cap)
            why = f"{cap} unavailable: {missing}" if missing else f"does not offer {cap}"
        elif not online:
            why = "offline" + (
                f" (last seen {_human_time(now, c.last_used_at)} ago)" if c.last_used_at else ""
            )
        elif state in IDLE_STATES:
            why = f"{state}" + (f": {info['reason']}" if info.get("reason") else "")
        elif c.id in busy and busy[c.id] != job.id:
            why = f"busy with job {busy[c.id]}"
        else:
            why = None
            ready.append(c.label)
        workers.append(
            {"id": c.id, "label": c.label, "state": state, "eligible": why is None, "why": why}
        )
    ahead = 0
    if job.status == JobStatus.QUEUED:
        ahead = db.scalar(
            select(func.count(Job.id)).where(
                Job.status == JobStatus.QUEUED,
                Job.capability == cap,
                Job.id != job.id,
                (Job.priority < job.priority)
                | ((Job.priority == job.priority) & (Job.created_at < job.created_at)),
            )
        )
    lease = None
    if job.status == JobStatus.LEASED:
        holder = None
        if job.leased_by_id is not None:
            cred = db.get(WorkerCredential, job.leased_by_id)
            holder = cred.label if cred else None
        lease = {
            "holder": holder or (job.result or {}).get("worker") or "this server",
            "expires_at": _iso(job.lease_expires_at),
            "heartbeat_at": _iso(job.heartbeat_at),
        }
    server = None if server_caps is None else cap in server_caps
    paused = is_paused(job)
    run_after = None if paused else job.run_after
    if lease is not None:
        summary = f"Running on {lease['holder']}"
    elif paused:
        summary = "Paused by the owner"
    elif run_after is not None and run_after > now:
        summary = f"Waiting {_human_time(run_after, now)} before retrying"
        if job.error and show_error:
            summary += f" after: {job.error[:200]}"
    elif ready:
        who = ", ".join(ready[:3]) if for_owner else "a worker"
        summary = f"Next for {who}" + (f" ({ahead} ahead)" if ahead else "")
    elif server:
        summary = "No worker can take it now; this server will run it itself"
    else:
        summary = f"Waiting for a worker that can run {cap}"
        reasons = [f"{w['label']}: {w['why']}" for w in workers if w["why"]]
        if for_owner:
            summary += f" ({'; '.join(reasons[:3])})" if reasons else " (no workers added)"
    return {
        "capability": cap,
        "summary": summary[:400],
        "workers": workers if for_owner else [],
        "eligible_online": len(ready),
        "server_runs": server,
        "ahead": ahead,
        "lease": lease,
        "run_after": _iso(run_after),
        "paused": paused,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "crashes": job.crashes or 0,
        "last_error": job.error if show_error else None,
    }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None
