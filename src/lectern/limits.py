"""Per-feature resource limits for the server's own heavy jobs (docs/resource-limits.md).

Lectern runs on a Raspberry Pi 4B next to the reader and KOReader sync, so everything heavy
it does by itself is capped per **feature**:

=============  ==============================================================================
``kokoro``     Kokoro narration on this server (``LECTERN_TTS_KOKORO_LOCAL``)
``piper``      Piper narration
``ocr``        text recognition of scanned PDFs
``convert``    PDF → EPUB jobs, and the MOBI/AZW3 → EPUB child at import
``align``      the local alignment worker
``transcode``  data-saver audio renditions
=============  ==============================================================================

Each feature has a :class:`Limit`: how many of its jobs may run at once (``concurrency``;
0 = never here, jobs wait for a worker), CPU threads for its engine (0 = the engine's
default), a memory cap for its child process in MiB (``None`` = no cap of our own), and its
``nice`` level. On top, ``LECTERN_HEAVY_JOBS_MAX`` caps heavy jobs of every feature together,
so OCR, a narration, and a conversion do not all start at once. Jobs that external workers
(the Mac) run never count: only the server's own runners ask for a slot.

The :data:`GOVERNOR` is the one place that decides. A runner asks it before it claims a job::

    with GOVERNOR.gate(OCR) as gate:
        if OCR not in gate.allowed:
            return False  # the job stays queued: "waiting: limit reached"
        job = claim(...)
        slot = gate.admit(OCR, job.id)
    with slot:
        run(job)

Admission is serialised (``gate`` holds a lock while the runner claims), so two runners can
never both take the last slot. :meth:`Governor.snapshot` is what Admin (and the job queue)
read: every feature's effective limits, what is running, and why a feature is waiting.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

KOKORO = "kokoro"
PIPER = "piper"
OCR = "ocr"
CONVERT = "convert"
ALIGN = "align"
TRANSCODE = "transcode"
FEATURES = (KOKORO, PIPER, OCR, CONVERT, ALIGN, TRANSCODE)

LABELS = {
    KOKORO: "Kokoro narration",
    PIPER: "Piper narration",
    OCR: "Text recognition (OCR)",
    CONVERT: "PDF and Kindle conversion",
    ALIGN: "Alignment",
    TRANSCODE: "Data-saver audio",
}

HEAVY_JOBS_ENV = "LECTERN_HEAVY_JOBS_MAX"
# The .env keys behind each limit, so Admin can say what to change. Existing names are kept
# (OCR, alignment, and transcode memory and threads predate this module).
ENV = {
    KOKORO: {"concurrency": "LECTERN_KOKORO_CONCURRENCY", "threads": "LECTERN_KOKORO_THREADS",
             "memory_mb": "LECTERN_KOKORO_MEMORY_LIMIT_MB", "nice": "LECTERN_KOKORO_NICE"},
    PIPER: {"concurrency": "LECTERN_PIPER_CONCURRENCY", "threads": "LECTERN_PIPER_THREADS",
            "memory_mb": "LECTERN_PIPER_MEMORY_LIMIT_MB", "nice": "LECTERN_PIPER_NICE"},
    OCR: {"concurrency": "LECTERN_OCR_CONCURRENCY", "threads": "LECTERN_OCR_THREADS",
          "memory_mb": "LECTERN_OCR_MEMORY_LIMIT_MB", "nice": "LECTERN_OCR_NICE"},
    CONVERT: {"concurrency": "LECTERN_CONVERT_CONCURRENCY", "threads": None,
              "memory_mb": "LECTERN_CONVERT_MEMORY_LIMIT_MB", "nice": "LECTERN_CONVERT_NICE"},
    ALIGN: {"concurrency": "LECTERN_ALIGN_CONCURRENCY", "threads": "LECTERN_ALIGN_THREADS",
            "memory_mb": "LECTERN_ALIGN_MEMORY_LIMIT_MB", "nice": "LECTERN_ALIGN_NICE"},
    TRANSCODE: {"concurrency": "LECTERN_TRANSCODE_CONCURRENCY",
                "threads": "LECTERN_TRANSCODE_THREADS",
                "memory_mb": "LECTERN_TRANSCODE_MEMORY_LIMIT_MB",
                "nice": "LECTERN_TRANSCODE_NICE"},
}  # fmt: skip

# Why a feature cannot start a job now (Governor.blocked).
OFF = "off"  # its concurrency is 0: only workers run it
FEATURE_LIMIT = "feature_limit"
GLOBAL_LIMIT = "global_limit"

# How a child process learns its limits: the parent sets these in the child's environment.
CHILD_NICE = "LECTERN_CHILD_NICE"
CHILD_MEMORY_MB = "LECTERN_CHILD_MEMORY_MB"
_THREAD_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")  # fmt: skip


@dataclass(frozen=True)
class Limit:
    concurrency: int
    threads: int | None  # 0 = the engine's default; None = not applicable
    memory_mb: int | None  # None = no cap of our own
    nice: int

    def child_env(self) -> dict[str, str]:
        """Environment for this feature's child process: its nice level, memory cap, and
        thread count for the usual native thread pools (OpenMP, BLAS)."""
        env = {CHILD_NICE: str(self.nice)}
        if self.memory_mb:
            env[CHILD_MEMORY_MB] = str(self.memory_mb)
        if self.threads:
            env.update({v: str(self.threads) for v in _THREAD_VARS})
        return env


@dataclass(frozen=True)
class Limits:
    heavy_jobs: int
    features: Mapping[str, Limit]

    @classmethod
    def from_settings(cls, settings) -> Limits:
        from lectern.align.isolate import resolve_limit_mb

        s = settings
        return cls(
            heavy_jobs=s.heavy_jobs_max,
            features={
                KOKORO: Limit(s.kokoro_concurrency, s.kokoro_threads,
                              s.kokoro_memory_limit_mb or None, s.kokoro_nice),
                PIPER: Limit(s.piper_concurrency, s.piper_threads,
                             s.piper_memory_limit_mb or None, s.piper_nice),
                OCR: Limit(s.ocr_concurrency, s.ocr_threads, s.ocr_memory_limit_mb, s.ocr_nice),
                CONVERT: Limit(s.convert_concurrency, None, s.convert_memory_limit_mb or None,
                               s.convert_nice),
                ALIGN: Limit(s.align_concurrency, s.align_threads,
                             resolve_limit_mb(s.align_memory_limit_mb), s.align_nice),
                TRANSCODE: Limit(s.transcode_concurrency, s.transcode_threads,
                                 s.transcode_memory_limit_mb or None, s.transcode_nice),
            },
        )  # fmt: skip

    @classmethod
    def defaults(cls) -> Limits:
        from lectern.config import Settings

        return cls.from_settings(Settings.model_construct())


@dataclass
class Running:
    feature: str
    job_id: int | None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class Slot:
    """One running job's hold on its feature's and the global cap; released on exit."""

    def __init__(self, governor: Governor, running: Running) -> None:
        self._governor = governor
        self.running = running
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._governor._release(self.running)

    def __enter__(self) -> Slot:
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


class Gate:
    """What a runner may claim right now (``allowed``) while it holds the admission lock."""

    def __init__(self, governor: Governor, allowed: frozenset[str]) -> None:
        self._governor = governor
        self.allowed = allowed

    def admit(self, feature: str, job_id: int | None = None) -> Slot:
        if feature not in self.allowed:
            raise ValueError(f"{feature} has no free slot")
        return self._governor._take(feature, job_id)


class Governor:
    def __init__(self, limits: Limits | None = None) -> None:
        self._limits = limits
        self._lock = threading.Lock()  # the running set
        self._admission = threading.Lock()  # one claim at a time, across every runner
        self._running: list[Running] = []

    @property
    def limits(self) -> Limits:
        if self._limits is None:
            self._limits = Limits.defaults()
        return self._limits

    def configure(self, limits: Limits) -> None:
        """New limits (at start-up). Running jobs keep their slots."""
        self._limits = limits

    def limit(self, feature: str) -> Limit:
        return self.limits.features[feature]

    def blocked(self, feature: str) -> str | None:
        """Why ``feature`` cannot start another job now, or ``None`` when it can."""
        with self._lock:
            return self._blocked(feature)

    def _blocked(self, feature: str) -> str | None:
        limit = self.limit(feature)
        if limit.concurrency <= 0:
            return OFF
        if sum(1 for r in self._running if r.feature == feature) >= limit.concurrency:
            return FEATURE_LIMIT
        if len(self._running) >= self.limits.heavy_jobs:
            return GLOBAL_LIMIT
        return None

    @contextlib.contextmanager
    def gate(self, *features: str) -> Iterator[Gate]:
        with self._admission:
            with self._lock:
                allowed = frozenset(f for f in features if self._blocked(f) is None)
            yield Gate(self, allowed)

    def _take(self, feature: str, job_id: int | None) -> Slot:
        running = Running(feature, job_id)
        with self._lock:
            self._running.append(running)
        return Slot(self, running)

    def _release(self, running: Running) -> None:
        with self._lock:
            with contextlib.suppress(ValueError):
                self._running.remove(running)

    def snapshot(self) -> dict:
        """The effective limits and current use, for Admin and the job queue (#87)."""
        with self._lock:
            limits = self.limits
            features = []
            for name in FEATURES:
                lim = limits.features[name]
                mine = [r for r in self._running if r.feature == name]
                features.append({
                    "feature": name,
                    "label": LABELS[name],
                    "concurrency": lim.concurrency,
                    "threads": lim.threads,
                    "memory_mb": lim.memory_mb,
                    "nice": lim.nice,
                    "active": len(mine),
                    "running": [{"job_id": r.job_id, "started_at": r.started_at.isoformat()}
                                for r in mine],
                    "blocked": self._blocked(name),
                    "env": ENV[name],
                })  # fmt: skip
            return {
                "heavy_jobs": {"limit": limits.heavy_jobs, "active": len(self._running),
                               "env": HEAVY_JOBS_ENV},
                "features": features,
            }  # fmt: skip


GOVERNOR = Governor()


def feature_of(kind: str, payload: dict | None) -> str | None:
    """The resource-limit feature a job of ``kind`` counts against when this server runs
    it, or ``None`` for light kinds (saved articles, ElevenLabs narration)."""
    if kind == "tts":
        engine = (payload or {}).get("engine")
        return engine if engine in (KOKORO, PIPER) else None
    return {"ocr": OCR, "pdf2epub": CONVERT, "align": ALIGN, "transcode": TRANSCODE}.get(kind)


def status(db) -> dict:
    """:meth:`Governor.snapshot` plus each feature's queued jobs: a feature with queued jobs
    and a ``blocked`` reason is "waiting: limit reached" (Admin, the job queue)."""
    from sqlalchemy import select

    from lectern.models import Job, JobStatus
    from lectern.queue import PAUSED_UNTIL

    queued: dict[str, int] = {}
    for kind, payload in db.execute(
        select(Job.kind, Job.payload).where(
            Job.status == JobStatus.QUEUED, Job.run_after < PAUSED_UNTIL
        )  # paused jobs wait for the owner, not for a limit
    ):
        if feature := feature_of(kind, payload):
            queued[feature] = queued.get(feature, 0) + 1
    snap = GOVERNOR.snapshot()
    for f in snap["features"]:
        f["queued"] = queued.get(f["feature"], 0)
        f["waiting_for_limit"] = bool(f["queued"] and f["blocked"] in (FEATURE_LIMIT, GLOBAL_LIMIT))
    return snap


def pool_size(*features: str) -> int:
    """Runner threads for these features: enough to use their concurrency, at least one."""
    return max(1, *(GOVERNOR.limit(f).concurrency for f in features))


# --- in a child process -------------------------------------------------------------------------


def lower_priority(default: int = 10) -> None:
    """In a child: the ``nice`` level its parent chose (``LECTERN_CHILD_NICE``), else
    ``default``. Best effort."""
    try:
        level = int(os.environ.get(CHILD_NICE, default))
    except ValueError:
        level = default
    with contextlib.suppress(OSError, AttributeError):
        os.nice(max(0, min(19, level)))


def child_memory_bytes(default: int) -> int:
    """In a child: the memory cap its parent chose (``LECTERN_CHILD_MEMORY_MB``), else
    ``default`` bytes."""
    try:
        return int(os.environ[CHILD_MEMORY_MB]) * 1024 * 1024
    except (KeyError, ValueError):
        return default


def confine_cpus(threads: int | None) -> None:
    """In a child: run on at most ``threads`` CPUs (the last ones, so the server's usual
    core stays free). Thread pools that ignore ``OMP_NUM_THREADS`` (ONNX Runtime) are
    bounded this way too. Linux only; elsewhere a no-op."""
    if not threads or not hasattr(os, "sched_setaffinity"):
        return
    with contextlib.suppress(OSError):
        cpus = sorted(os.sched_getaffinity(0))
        if threads < len(cpus):
            os.sched_setaffinity(0, cpus[-threads:])
