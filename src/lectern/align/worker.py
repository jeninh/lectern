"""Running alignment jobs: in process (``LocalWorker``) or against a remote server
(``RemoteWorker``, what ``lectern worker --server`` runs).

Both follow the worker protocol in docs/contracts.md: claim with a lease, heartbeat at
``min(lease_s / 3, 30 s)`` while :func:`lectern.align.pipeline.align_work` runs, upload the sentence map,
or report the failure. A lost lease (``409 lease_lost``: expiry, cancellation, a changed work,
a revoked credential) abandons the job; the server already owns it again.

The local worker skips HTTP, reads inputs straight from the library, and applies the result
through :func:`lectern.align.jobs.apply_result`, so the validation path is identical.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import sessionmaker

from lectern.align import isolate, jobs
from lectern.align.pipeline import TrackInput, TranscribeFn, align_work
from lectern.align.tracks import work_epub, work_tracks
from lectern.align.transcribe import Transcriber, TranscriptionUnavailable, transcribe_tracks
from lectern.models import Job, Work
from lectern.sentence_map import SentenceMap

log = logging.getLogger("lectern.align.worker")

DEFAULT_LEASE_S = 600
DEFAULT_POLL_S = 10.0
# Heartbeat at least every lease_s / 3 (the contract), and often enough that progress shows.
MAX_HEARTBEAT_S = 30.0
# An idle remote worker must poll well inside the server's "worker seen" window, or books
# show "alignment cannot run" between its polls.
MAX_POLL_S = jobs.WORKER_SEEN_S - 60


class JobAbandoned(Exception):
    """The lease was lost while the job ran; stop and do not report anything."""


class WorkerStopping(Exception):
    """The worker was asked to stop while a job ran; hand the job back."""


class _Heartbeat:
    """Sends ``send(progress, message)`` every ``interval`` seconds from a thread; a ``False``
    answer means the lease is gone, and the job's progress callback then raises."""

    def __init__(
        self,
        send: Callable[[float, str], bool],
        interval: float,
        stopping: threading.Event | Callable[[], bool] | None = None,
    ) -> None:
        self._send = send
        self._stopping = stopping
        self._interval = max(1.0, interval)
        self._stop = threading.Event()
        self.lost = threading.Event()
        self.progress = 0.0
        self.message = "starting"
        self._thread = threading.Thread(target=self._run, name="lectern-heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self._interval + 5)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                ok = self._send(self.progress, self.message)
            except Exception:  # noqa: BLE001 - a transient error is not a lost lease
                log.warning("heartbeat failed", exc_info=True)
                continue
            if not ok:
                self.lost.set()
                return

    def on_progress(self, frac: float, message: str) -> None:
        if self.lost.is_set():
            raise JobAbandoned()
        stopping = self._stopping
        if stopping is not None and (
            stopping.is_set() if isinstance(stopping, threading.Event) else stopping()
        ):
            raise WorkerStopping()
        self.progress, self.message = frac, message


def transcriber_fn(transcriber: Transcriber) -> TranscribeFn:
    def _run(tracks, language, on_progress):
        return transcribe_tracks(transcriber, tracks, language, on_progress)

    return _run


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]


# --- in-process worker ------------------------------------------------------------------------


class LocalWorker:
    """Claims ``align`` jobs for the server and runs them.

    With ``isolated`` (what the server uses) each job runs in a supervised child process
    (:mod:`lectern.align.isolate`): this thread keeps the lease and the heartbeat, and a
    crash or out-of-memory kill of the child fails the job instead of the server. Without it
    the pipeline runs in this thread, which is how tests inject ``transcribe`` (a function
    returning recorded words). An isolated worker can use recorded words too (``transcript``,
    a JSON file in the ``lectern align-transcribe`` format).
    """

    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        data_dir: Path,
        library: Path,
        transcriber: Transcriber | None = None,
        transcribe: TranscribeFn | None = None,
        model_label: str | None = None,
        name: str = "local",
        lease_s: int = DEFAULT_LEASE_S,
        poll_s: float = DEFAULT_POLL_S,
        isolated: bool = False,
        memory_limit_mb: int | None = None,
        transcript: Path | None = None,
        child_command: list[str] | None = None,
        stall_s: float = jobs.STALL_S,
        defer_s: float = 0.0,
    ) -> None:
        self.session_factory = session_factory
        self.stall_s = stall_s
        # Leave jobs to an online external align worker this long (the Pi fallback).
        self.defer_s = defer_s
        self.data_dir = data_dir
        self.library = library
        self.transcriber = transcriber
        self._transcribe = transcribe
        self._model_label = model_label
        self.name = name
        self.lease_s = lease_s
        self.poll_s = poll_s
        self.isolated = isolated
        # resolved once: None = no cap of our own (see lectern.align.isolate)
        self.memory_limit_mb = isolate.resolve_limit_mb(memory_limit_mb) if isolated else None
        self.transcript = transcript
        self.child_command = child_command
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current: tuple[int, str] | None = None  # (job id, lease token) while running

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="lectern-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop and hand back a running job. The thread notices the stop at its next progress
        report; if transcription has not reported within the join timeout, the job is released
        from here, so a restart neither waits for the lease to expire nor spends an attempt.
        A late result from the old thread then fails the lease check."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            current = self._current
            if self._thread.is_alive() and current is not None:
                log.info("job %d: worker still busy at shutdown, releasing the job", current[0])
                self._release(*current)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ran = self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("local worker loop failed")
                ran = False
            if not ran:
                self._stop.wait(self.poll_s)

    def _transcribe_for(self, model: str) -> tuple[TranscribeFn, str]:
        if self._transcribe is not None:
            return self._transcribe, self._model_label or f"recorded-{model}"
        tr = (self.transcriber or Transcriber()).with_model(model)
        return transcriber_fn(tr), tr.label

    def run_once(self) -> bool:
        """Claim and run one job. Returns False when the queue is empty."""
        with self.session_factory() as db:
            claimed = jobs.claim(
                db,
                kinds=[jobs.KIND_ALIGN],
                capabilities=[jobs.KIND_ALIGN],
                worker=self.name,
                lease_s=self.lease_s,
                credential_id=None,
                stall_s=self.stall_s,
                defer_s=self.defer_s,
            )
            if claimed is None:
                return False
            job, token = claimed
            job_id, payload, work_id = job.id, dict(job.payload or {}), job.work_id
            attempt = job.attempts
            work = db.get(Work, work_id)
            found = work_epub(db, self.library, work) if work else None
            tracks = [
                TrackInput(
                    t.position,
                    self.library / work.dir_path / t.rel_path,
                    t.sha256,
                    t.duration_s,
                    t.offset_s,
                )
                for t in (work_tracks(db, work_id) if work else [])
            ]
        if found is None or not tracks:
            self._fail(job_id, token, "inputs missing from the library", retryable=False)
            return True

        def send(progress: float, message: str) -> bool:
            with self.session_factory() as db:
                try:
                    row = jobs.verify_lease(db, job_id, token)
                except jobs.LeaseLost:
                    return False
                jobs.heartbeat(db, row, progress=progress, message=message)
                return True

        hb = _Heartbeat(send, min(self.lease_s / 3, MAX_HEARTBEAT_S), self._stop)
        hb.start()
        self._current = (job_id, token)
        log.info("job %d: aligning work %s with %s", job_id, work_id, payload.get("model"))
        try:
            if self.isolated:
                return self._align_isolated(
                    job_id, token, attempt, payload, work_id, found, tracks, hb
                )
            return self._align(job_id, token, payload, work_id, found, tracks, hb)
        finally:
            self._current = None

    def _align(self, job_id, token, payload, work_id, found, tracks, hb) -> bool:
        try:
            transcribe, label = self._transcribe_for(payload.get("model") or "small")
            out = align_work(
                work_id=work_id,
                epub_path=found[1],
                epub_sha256=found[0].sha256,
                tracks=tracks,
                model_label=label,
                transcribe=transcribe,
                language=payload.get("language"),
                on_progress=hb.on_progress,
            )
        except JobAbandoned:
            log.warning("job %d: lease lost, abandoning", job_id)
            return True
        except WorkerStopping:
            log.info("job %d: worker stopping, releasing the job", job_id)
            self._release(job_id, token)
            return True
        except TranscriptionUnavailable as exc:
            self._fail(job_id, token, _error_text(exc), retryable=False)
            return True
        except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised
            log.exception("job %d failed", job_id)
            self._fail(job_id, token, _error_text(exc), retryable=True)
            return True
        finally:
            hb.stop()
        self._submit(job_id, token, out.sentence_map.model_dump_json().encode(), out.stats)
        return True

    def _align_isolated(self, job_id, token, attempt, payload, work_id, found, tracks, hb) -> bool:
        """The same job in a supervised child process (``lectern.align.isolate``)."""
        model = payload.get("model") or "small"
        spec: dict = {
            "work_id": work_id,
            "epub_path": str(found[1]),
            "epub_sha256": found[0].sha256,
            "language": payload.get("language"),
            "tracks": [
                {
                    "position": t.position,
                    "path": str(t.path),
                    "sha256": t.sha256,
                    "duration_s": t.duration_s,
                    "offset_s": t.offset_s,
                }
                for t in tracks
            ],
        }
        if self.transcript is not None:
            spec["transcript"] = str(self.transcript)
            spec["model_label"] = self._model_label or f"recorded-{model}"
        else:
            tr = (self.transcriber or Transcriber()).with_model(model)
            spec["transcriber"] = {
                "model": tr.model_name,
                "device": tr.device,
                "compute_type": tr.compute_type,
                "threads": tr.threads,
                "download_root": str(tr.download_root) if tr.download_root else None,
                "beam_size": tr.beam_size,
                "window_s": tr.window_s,
                "overlap_s": tr.overlap_s,
            }

        def progress(frac: float, message: str) -> None:
            hb.progress, hb.message = frac, message

        def interrupt() -> str | None:
            if hb.lost.is_set():
                return "abandon"
            if self._stop.is_set():
                return "stop"
            return None

        try:
            outcome = isolate.run(
                spec,
                workdir=self.data_dir / "tmp",
                log_path=isolate.log_path_for(self.data_dir, job_id, attempt),
                on_progress=progress,
                interrupt=interrupt,
                memory_limit_mb=self.memory_limit_mb,
                command=self.child_command,
            )
        except OSError as exc:  # could not start a process at all
            log.exception("job %d: could not start the alignment process", job_id)
            hb.stop()
            self._fail(job_id, token, _error_text(exc), retryable=True)
            return True
        hb.stop()
        if outcome.kind == "abandoned":
            log.warning("job %d: lease lost, alignment process stopped", job_id)
        elif outcome.kind == "stopped":
            log.info("job %d: worker stopping, releasing the job", job_id)
            self._release(job_id, token)
        elif outcome.kind == "ok":
            result = outcome.result or {}
            self._submit(
                job_id,
                token,
                SentenceMap.model_validate(result.get("sentence_map")).model_dump_json().encode(),
                result.get("stats") or {},
            )
        else:
            log.error(
                "job %d: alignment process %s (exit %s, peak %s MiB): %s",
                job_id,
                outcome.kind,
                outcome.returncode,
                outcome.peak_rss_mb,
                (outcome.error or "").splitlines()[0] if outcome.error else "",
            )
            self._fail(job_id, token, outcome.error or outcome.kind, retryable=outcome.retryable)
        return True

    def _submit(self, job_id: int, token: str, body: bytes, raw_stats: dict) -> None:
        with self.session_factory() as db:
            try:
                job = jobs.verify_lease(db, job_id, token)
                try:
                    stats = jobs.AlignStats.model_validate(raw_stats)
                except ValidationError:
                    log.warning("job %d: dropping stats that do not validate", job_id)
                    stats = None
                jobs.heartbeat(db, job, progress=1.0, message="uploading", stats=stats)
                result = jobs.apply_result(db, self.data_dir, self.library, job, body)
            except jobs.LeaseLost:
                db.rollback()
                log.warning("job %d: lease lost before the result was applied", job_id)
                return
            except jobs.ResultError as exc:
                db.rollback()
                job = db.get(Job, job_id)
                jobs.fail(db, job, f"{exc.code}: {'; '.join(exc.errors)[:1500]}", retryable=False)
                return
        log.info(
            "job %d: %d sentences, coverage %.3f, %.1fs (%.2fx realtime)",
            job_id,
            result["sentence_count"],
            result["coverage"],
            raw_stats.get("elapsed_s") or 0.0,
            raw_stats.get("speed") or 0.0,
        )

    def _fail(self, job_id: int, token: str, error: str, *, retryable: bool) -> None:
        with self.session_factory() as db:
            try:
                job = jobs.verify_lease(db, job_id, token)
            except jobs.LeaseLost:
                return
            jobs.fail(db, job, error, retryable=retryable)

    def _release(self, job_id: int, token: str) -> None:
        with self.session_factory() as db:
            try:
                job = jobs.verify_lease(db, job_id, token)
            except jobs.LeaseLost:
                return
            jobs.release(db, job)


# --- remote worker (``lectern worker``) --------------------------------------------------------


class LeaseLostError(Exception):
    pass


class PermanentJobError(Exception):
    """The job cannot succeed on this worker (bad input, missing engine): fail, no retry."""


class JobContext:
    """What a runner gets for one leased job: progress, inputs, artifacts, and the result.

    Every call carries the worker key and the lease token. A ``409`` from the server means the
    lease is gone (:class:`LeaseLostError`); :meth:`progress` and :meth:`check` raise
    ``WorkerStopping`` when the worker is stopping or paused, so the job is handed back."""

    def __init__(self, worker: RemoteWorker, claimed: dict, hb: _Heartbeat, tmp: Path) -> None:
        self.worker = worker
        self.client = worker.client
        self.claimed = claimed
        self.job = claimed["job"]
        self.id = self.job["id"]
        self.payload = self.job.get("payload") or {}
        self.headers = {"X-Lease-Token": claimed["lease"]["token"]}
        self.tmp = tmp
        self.hb = hb

    def progress(self, frac: float, message: str) -> None:
        self.hb.on_progress(frac, message)

    def check(self) -> None:
        self.hb.on_progress(self.hb.progress, self.hb.message)

    def download(self, url: str, dest: Path, expected_sha256: str) -> Path:
        """Authenticated streaming download verified against the expected hash."""
        digest = hashlib.sha256()
        with self.client.stream("GET", url, headers=self.headers) as r:
            if r.status_code == 409:
                raise LeaseLostError()
            r.raise_for_status()
            declared = r.headers.get("X-Content-SHA256")
            if declared and declared.lower() != expected_sha256:
                raise ValueError(
                    f"server offers {declared[:12]}…, job expects {expected_sha256[:12]}…"
                )
            with dest.open("wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    self.check()
                    digest.update(chunk)
                    fh.write(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ValueError(
                f"downloaded {dest.name} does not match sha256 {expected_sha256[:12]}…"
            )
        return dest

    def input(self, name: str, dest: Path | None = None) -> Path:
        """A named input of a non-align job (``inputs[name] = {url, sha256, …}``)."""
        spec = (self.claimed.get("inputs") or {}).get(name)
        if not isinstance(spec, dict):
            raise PermanentJobError(f"the job has no input {name!r}")
        return self.download(spec["url"], dest or self.tmp / name, spec["sha256"])

    def artifacts(self) -> dict[str, dict]:
        """What this job's earlier attempts uploaded: ``{name: {size, sha256}}``."""
        r = self.client.get(self.claimed["artifacts_url"], headers=self.headers)
        if r.status_code == 409:
            raise LeaseLostError()
        r.raise_for_status()
        return {a["name"]: a for a in r.json().get("artifacts", [])}

    def fetch_artifact(self, name: str, sha256: str, dest: Path | None = None) -> Path:
        return self.download(
            f"{self.claimed['artifacts_url']}/{name}", dest or self.tmp / name, sha256
        )

    def upload_artifact(self, name: str, path: Path) -> str:
        """Upload one output file; returns its SHA-256. Retried on 5xx and transport errors."""
        from lectern.ingest.hashing import sha256_file

        digest = sha256_file(path)
        for attempt in range(3):
            self.check()
            try:
                with path.open("rb") as fh:
                    r = self.client.put(
                        f"{self.claimed['artifacts_url']}/{name}",
                        content=fh,
                        headers={
                            **self.headers,
                            "X-Content-SHA256": digest,
                            "Content-Type": "application/octet-stream",
                            "Content-Length": str(path.stat().st_size),
                        },
                    )
            except Exception:  # noqa: BLE001 - a dropped connection is worth a retry
                if attempt == 2:
                    raise
                log.warning("job %d: uploading %s failed, retrying", self.id, name, exc_info=True)
                time.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code == 409:
                raise LeaseLostError()
            if r.status_code < 500:
                r.raise_for_status()
                return digest
            log.warning("job %d: artifact %s got %d, retrying", self.id, name, r.status_code)
            time.sleep(2.0 * (attempt + 1))
        raise RuntimeError(f"uploading {name} failed repeatedly")

    def stats(self, stats: dict) -> None:
        """Statistics ride on a heartbeat so the server records them with the result."""
        r = self.client.post(
            f"/api/worker/jobs/{self.id}/heartbeat",
            json={"progress": 1.0, "message": "uploading", "stats": stats},
            headers=self.headers,
        )
        if r.status_code == 409:
            raise LeaseLostError()

    def submit(self, body: bytes) -> dict | None:
        """``PUT`` the result. A rejected result fails the job (no retry); a lost lease
        discards it; other errors are retried, then reported as a retryable failure."""
        for attempt in range(3):
            r = self.client.put(
                self.claimed["result_url"],
                content=body,
                headers={**self.headers, "Content-Type": "application/json"},
            )
            if r.status_code == 200:
                return r.json()
            if r.status_code == 409:
                log.warning("job %d: lease lost, result discarded", self.id)
                return None
            if r.status_code in (400, 413, 422):
                detail = (
                    r.json()
                    if r.headers.get("content-type", "").startswith("application/json")
                    else {}
                )
                errors = "; ".join(str(e) for e in detail.get("errors", []))[:1500]
                self.worker._fail(
                    self.id,
                    self.headers,
                    f"{detail.get('detail', r.status_code)}: {errors}",
                    retryable=False,
                )
                return None
            log.warning("job %d: upload got %d, retrying", self.id, r.status_code)
            time.sleep(2.0 * (attempt + 1))
        self.worker._fail(self.id, self.headers, "result upload failed repeatedly", retryable=True)
        return None


class AlignRunner:
    """``align`` jobs: download the EPUB and tracks, transcribe, match, upload the map."""

    kind = jobs.KIND_ALIGN
    capabilities = ("align",)

    def __init__(
        self,
        transcriber: Transcriber | None = None,
        transcribe: TranscribeFn | None = None,
        model_label: str | None = None,
    ) -> None:
        self.transcriber = transcriber
        self._transcribe = transcribe
        self._model_label = model_label

    def _transcribe_for(self, model: str) -> tuple[TranscribeFn, str]:
        if self._transcribe is not None:
            return self._transcribe, self._model_label or f"recorded-{model}"
        tr = (self.transcriber or Transcriber()).with_model(model)
        tr.load()  # before reading the label: MLX may fall back to the CPU here
        return transcriber_fn(tr), tr.label

    def run(self, ctx: JobContext) -> None:
        job, payload, claimed = ctx.job, ctx.payload, ctx.claimed
        ctx.progress(0.0, "downloading inputs")
        epub = ctx.download(
            claimed["inputs"]["epub"], ctx.tmp / "book.epub", payload["epub_sha256"]
        )
        tracks = []
        offset = 0.0
        for url, t in zip(claimed["inputs"]["tracks"], payload["tracks"], strict=True):
            path = ctx.download(url, ctx.tmp / f"track-{t['position']}", t["sha256"])
            tracks.append(
                TrackInput(
                    t["position"],
                    path,
                    t["sha256"],
                    float(t["duration_s"]),
                    float(t.get("offset_s", offset)),
                )
            )
            offset += float(t["duration_s"])
        transcribe, label = self._transcribe_for(payload.get("model") or "small")
        try:
            out = align_work(
                work_id=job["work_id"],
                epub_path=epub,
                epub_sha256=payload["epub_sha256"],
                tracks=tracks,
                model_label=label,
                transcribe=transcribe,
                language=payload.get("language"),
                on_progress=ctx.progress,
            )
        except TranscriptionUnavailable as exc:
            raise PermanentJobError(_error_text(exc)) from exc
        ctx.hb.stop()
        ctx.stats(out.stats)
        res = ctx.submit(out.sentence_map.model_dump_json().encode())
        if res is not None:
            log.info(
                "job %d: %s sentences, coverage %s, %.1fs (%.2fx realtime)",
                ctx.id,
                res.get("sentence_count"),
                res.get("coverage"),
                out.stats["elapsed_s"],
                out.stats.get("speed") or 0.0,
            )


class RemoteWorker:
    """One worker process talking to ``/api/worker/*`` over HTTPS with a worker credential.

    It runs every kind it has a runner for (``AlignRunner`` by default; the Mac worker adds
    Kokoro TTS and transcode) and advertises the runners' capabilities on each claim.
    ``pause()`` returns a reason (on battery, not on Wi-Fi, switched off) while the worker
    must not run jobs; a job in progress is then handed back without spending an attempt.
    """

    def __init__(
        self,
        server: str,
        key: str,
        *,
        transcriber: Transcriber | None = None,
        transcribe: TranscribeFn | None = None,
        model_label: str | None = None,
        name: str = "worker",
        lease_s: int = DEFAULT_LEASE_S,
        poll_s: float = DEFAULT_POLL_S,
        workdir: Path | None = None,
        client=None,
        stop: threading.Event | None = None,
        runners: list | None = None,
        pause: Callable[[], str | None] | None = None,
        info: dict | None = None,
        proxy: str | None = None,
        verify=True,
    ) -> None:
        import httpx

        self.server = server.rstrip("/")
        self.name = name
        self.lease_s = lease_s
        if poll_s > MAX_POLL_S:
            log.warning(
                "--poll %.0f s is longer than the server waits before it reports no worker; "
                "polling every %d s instead",
                poll_s,
                MAX_POLL_S,
            )
            poll_s = MAX_POLL_S
        self.poll_s = poll_s
        self.workdir = workdir
        self.runners = (
            runners if runners is not None else [AlignRunner(transcriber, transcribe, model_label)]
        )
        self._pause = pause or (lambda: None)
        self.info = info or {}
        self._stop = stop or threading.Event()
        self._running: tuple[dict, _Heartbeat] | None = None
        self.client = client or httpx.Client(
            base_url=self.server,
            headers={"Authorization": f"Bearer {key}", "User-Agent": "lectern-worker"},
            timeout=httpx.Timeout(60.0, read=600.0),
            follow_redirects=False,
            proxy=proxy,
            verify=verify,
        )

    @property
    def current(self) -> dict | None:
        """The running job with its latest progress, for the menu bar; ``None`` when idle."""
        running = self._running
        if running is None:
            return None
        about, hb = running
        return {**about, "progress": hb.progress, "message": hb.message}

    @property
    def kinds(self) -> list[str]:
        return list(dict.fromkeys(r.kind for r in self.runners))

    @property
    def capabilities(self) -> list[str]:
        return list(dict.fromkeys(c for r in self.runners for c in r.capabilities))

    def paused(self) -> str | None:
        return "stopping" if self._stop.is_set() else self._pause()

    def run_forever(self) -> None:
        stop = self._stop
        log.info(
            "worker %s polling %s every %.0fs for %s",
            self.name,
            self.server,
            self.poll_s,
            ", ".join(self.capabilities),
        )
        while not stop.is_set():
            reason = self._pause()
            if reason:
                self.report("paused", reason)
                stop.wait(self.poll_s)
                continue
            try:
                ran = self.run_once()
            except PermissionError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("worker loop failed; retrying after %.0fs", self.poll_s)
                ran = False
            if not ran:
                stop.wait(self.poll_s)

    def report(self, state: str, reason: str | None = None) -> bool:
        """Presence while not running a job (``POST /api/worker/status``)."""
        try:
            r = self.client.post(
                "/api/worker/status",
                json={
                    "worker": self.name,
                    "state": state,
                    "capabilities": self.capabilities,
                    "info": {**self.info, "reason": reason} if reason else self.info,
                },
            )
        except Exception:  # noqa: BLE001 - presence is best effort
            log.debug("status report failed", exc_info=True)
            return False
        if r.status_code == 401:
            raise PermissionError("the server rejected the worker credential")
        return r.status_code == 200

    # -- protocol steps ----------------------------------------------------------------------

    def run_once(self) -> bool:
        r = self.client.post(
            "/api/worker/jobs/claim",
            json={
                "kinds": self.kinds,
                "capabilities": self.capabilities,
                "worker": self.name,
                "lease_s": self.lease_s,
                "info": self.info,
            },
        )
        if r.status_code == 204:
            return False
        if r.status_code == 401:
            raise PermissionError("the server rejected the worker credential")
        r.raise_for_status()
        claimed = r.json()
        job, token = claimed["job"], claimed["lease"]["token"]
        headers = {"X-Lease-Token": token}
        job_id = job["id"]
        lease_s = int(claimed["lease"].get("lease_s") or self.lease_s)
        runner = next((x for x in self.runners if x.kind == job["kind"]), None)
        if runner is None:  # the server sent a kind we did not ask for
            self._release(job_id, headers)
            return True
        claimed.setdefault("artifacts_url", f"/api/worker/jobs/{job_id}/artifacts")

        def send(progress: float, message: str) -> bool:
            hr = self.client.post(
                f"/api/worker/jobs/{job_id}/heartbeat",
                json={"progress": progress, "message": message},
                headers=headers,
            )
            if hr.status_code == 409:
                return False
            hr.raise_for_status()
            return True

        hb = _Heartbeat(send, min(lease_s / 3, MAX_HEARTBEAT_S), lambda: bool(self.paused()))
        tmp = Path(tempfile.mkdtemp(prefix=f"lectern-job-{job_id}-", dir=self.workdir))
        ctx = JobContext(self, claimed, hb, tmp)
        about = {
            "id": job_id,
            "kind": job["kind"],
            "capability": job.get("capability") or job["kind"],
            "work_id": job.get("work_id"),
        }
        self._running = (about, hb)
        log.info("job %d: claimed (%s, work %s)", job_id, about["capability"], job["work_id"])
        hb.start()
        try:
            runner.run(ctx)
        except (JobAbandoned, LeaseLostError):
            log.warning("job %d: lease lost, abandoning", job_id)
        except WorkerStopping:
            log.info("job %d: %s, handing the job back", job_id, self.paused() or "stopping")
            self._release(job_id, headers)
        except PermanentJobError as exc:
            self._fail(job_id, headers, str(exc)[:2000], retryable=False)
        except TranscriptionUnavailable as exc:
            self._fail(job_id, headers, _error_text(exc), retryable=False)
        except Exception as exc:  # noqa: BLE001
            log.exception("job %d failed", job_id)
            self._fail(job_id, headers, _error_text(exc), retryable=True)
        finally:
            hb.stop()
            self._running = None
            shutil.rmtree(tmp, ignore_errors=True)
        return True

    def _release(self, job_id: int, headers: dict) -> None:
        """Give the job back without spending an attempt; a server from before
        ``/release`` existed gets a retryable failure instead."""
        try:
            r = self.client.post(f"/api/worker/jobs/{job_id}/release", headers=headers)
            if r.status_code in (404, 405):
                self._fail(job_id, headers, "worker stopped", retryable=True)
        except Exception:  # noqa: BLE001 - the lease will expire and the server reclaims it
            log.warning("job %d: could not hand the job back", job_id, exc_info=True)

    def _fail(self, job_id: int, headers: dict, error: str, *, retryable: bool) -> None:
        try:
            self.client.post(
                f"/api/worker/jobs/{job_id}/fail",
                json={"error": error, "retryable": retryable},
                headers=headers,
            )
        except Exception:  # noqa: BLE001 - the lease will expire and the server reclaims it
            log.warning("job %d: could not report failure", job_id, exc_info=True)
