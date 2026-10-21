"""Job kinds, worker capabilities, and routing (docs/contracts.md, "Capabilities and routing").

Every job has a ``capability``: what a worker must advertise to claim it. It is derived from
the job's kind and payload when the job is inserted (``capability_for``): ``align`` →
``align``, ``transcode`` → ``transcode``, ``tts`` → ``tts-<payload.engine>``. Workers send
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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from lectern.config import Settings
    from lectern.models import Job, WorkerCredential

CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

ALIGN = "align"
TRANSCODE = "transcode"
OCR = "ocr"  # reserved: no job kind uses it yet
TTS_KOKORO = "tts-kokoro"
TTS_PIPER = "tts-piper"
TTS_ELEVENLABS = "tts-elevenlabs"
KNOWN_CAPABILITIES = (ALIGN, TRANSCODE, OCR, TTS_KOKORO, TTS_PIPER, TTS_ELEVENLABS)
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
        import lectern.jobs  # noqa: F401 - registers "transcode"


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
    return [c for c in caps if c not in SERVER_ONLY and (allowed is None or c in allowed)]


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
    """Of a local runner's capabilities, the ones an external worker is online for."""
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
        if job is None or job.status not in JOB_ACTIVE_STATUSES:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed
