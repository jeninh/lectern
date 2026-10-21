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
from lectern.align.pipeline import AlignOutput, TrackInput, TranscribeFn, align_work
from lectern.align.tracks import work_epub, work_tracks
from lectern.align.transcribe import Transcriber, TranscriptionUnavailable, transcribe_tracks
from lectern.models import Job, Work
from lectern.sentence_map import SentenceMap

log = logging.getLogger("lectern.align.worker")

DEFAULT_LEASE_S = 600
DEFAULT_POLL_S = 10.0
# Heartbeat at least every lease_s / 3 (the contract), and often enough that progress shows.
MAX_HEARTBEAT_S = 30.0


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
        stopping: threading.Event | None = None,
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
        if self._stopping is not None and self._stopping.is_set():
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
    ) -> None:
        self.session_factory = session_factory
        self.stall_s = stall_s
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
                worker=self.name,
                lease_s=self.lease_s,
                credential_id=None,
                stall_s=self.stall_s,
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


class RemoteWorker:
    """One worker process talking to ``/api/worker/*`` over HTTPS with a worker credential."""

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
    ) -> None:
        import httpx

        self.server = server.rstrip("/")
        self.name = name
        self.lease_s = lease_s
        self.poll_s = poll_s
        self.workdir = workdir
        self.transcriber = transcriber
        self._transcribe = transcribe
        self._model_label = model_label
        self._stop = stop or threading.Event()
        self.client = client or httpx.Client(
            base_url=self.server,
            headers={"Authorization": f"Bearer {key}", "User-Agent": "lectern-worker"},
            timeout=httpx.Timeout(60.0, read=600.0),
            follow_redirects=False,
        )

    def run_forever(self) -> None:
        stop = self._stop
        log.info("worker %s polling %s every %.0fs", self.name, self.server, self.poll_s)
        while not stop.is_set():
            try:
                ran = self.run_once()
            except PermissionError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("worker loop failed; retrying after %.0fs", self.poll_s)
                ran = False
            if not ran:
                stop.wait(self.poll_s)

    # -- protocol steps ----------------------------------------------------------------------

    def run_once(self) -> bool:
        r = self.client.post(
            "/api/worker/jobs/claim",
            json={"kinds": [jobs.KIND_ALIGN], "worker": self.name, "lease_s": self.lease_s},
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
        payload = job["payload"]
        lease_s = int(claimed["lease"].get("lease_s") or self.lease_s)

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

        hb = _Heartbeat(send, min(lease_s / 3, MAX_HEARTBEAT_S), self._stop)
        hb.start()
        tmp = Path(tempfile.mkdtemp(prefix=f"lectern-job-{job_id}-", dir=self.workdir))
        log.info("job %d: claimed (work %s, model %s)", job_id, job["work_id"], payload["model"])
        try:
            hb.on_progress(0.0, "downloading inputs")
            epub = self._download(
                claimed["inputs"]["epub"], tmp / "book.epub", headers, payload["epub_sha256"]
            )
            tracks = []
            offset = 0.0
            for url, t in zip(claimed["inputs"]["tracks"], payload["tracks"], strict=True):
                path = self._download(url, tmp / f"track-{t['position']}", headers, t["sha256"])
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
            out = align_work(
                work_id=job["work_id"],
                epub_path=epub,
                epub_sha256=payload["epub_sha256"],
                tracks=tracks,
                model_label=label,
                transcribe=transcribe,
                language=payload.get("language"),
                on_progress=hb.on_progress,
            )
        except (JobAbandoned, LeaseLostError):
            log.warning("job %d: lease lost, abandoning", job_id)
            return True
        except WorkerStopping:
            # The protocol has no "release"; a retryable failure requeues it with backoff.
            self._fail(job_id, headers, "worker stopped", retryable=True)
            return True
        except TranscriptionUnavailable as exc:
            self._fail(job_id, headers, _error_text(exc), retryable=False)
            return True
        except Exception as exc:  # noqa: BLE001
            log.exception("job %d failed", job_id)
            self._fail(job_id, headers, _error_text(exc), retryable=True)
            return True
        finally:
            hb.stop()
            shutil.rmtree(tmp, ignore_errors=True)
        self._upload(job_id, claimed["result_url"], headers, out)
        return True

    def _transcribe_for(self, model: str) -> tuple[TranscribeFn, str]:
        if self._transcribe is not None:
            return self._transcribe, self._model_label or f"recorded-{model}"
        tr = (self.transcriber or Transcriber()).with_model(model)
        return transcriber_fn(tr), tr.label

    def _download(self, url: str, dest: Path, headers: dict, expected_sha256: str) -> Path:
        """Authenticated streaming download verified against the job's input snapshot."""
        digest = hashlib.sha256()
        with self.client.stream("GET", url, headers=headers) as r:
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
                    digest.update(chunk)
                    fh.write(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ValueError(
                f"downloaded {dest.name} does not match sha256 {expected_sha256[:12]}…"
            )
        return dest

    def _upload(self, job_id: int, url: str, headers: dict, out: AlignOutput) -> None:
        # Stats ride on a heartbeat so the server records them with the result.
        hr = self.client.post(
            f"/api/worker/jobs/{job_id}/heartbeat",
            json={"progress": 1.0, "message": "uploading", "stats": out.stats},
            headers=headers,
        )
        if hr.status_code == 409:
            log.warning("job %d: lease lost before upload", job_id)
            return
        body = out.sentence_map.model_dump_json().encode()
        for attempt in range(3):
            r = self.client.put(
                url, content=body, headers={**headers, "Content-Type": "application/json"}
            )
            if r.status_code == 200:
                res = r.json()
                log.info(
                    "job %d: %s sentences, coverage %s, %.1fs (%.2fx realtime)",
                    job_id,
                    res.get("sentence_count"),
                    res.get("coverage"),
                    out.stats["elapsed_s"],
                    out.stats.get("speed") or 0.0,
                )
                return
            if r.status_code == 409:
                log.warning("job %d: lease lost, result discarded", job_id)
                return
            if r.status_code in (400, 413, 422):
                detail = (
                    r.json()
                    if r.headers.get("content-type", "").startswith("application/json")
                    else {}
                )
                error = f"{detail.get('detail', r.status_code)}: {'; '.join(detail.get('errors', []))[:1500]}"
                self._fail(job_id, headers, error, retryable=False)
                return
            log.warning("job %d: upload got %d, retrying", job_id, r.status_code)
            time.sleep(2.0 * (attempt + 1))
        self._fail(job_id, headers, "result upload failed repeatedly", retryable=True)

    def _fail(self, job_id: int, headers: dict, error: str, *, retryable: bool) -> None:
        try:
            self.client.post(
                f"/api/worker/jobs/{job_id}/fail",
                json={"error": error, "retryable": retryable},
                headers=headers,
            )
        except Exception:  # noqa: BLE001 - the lease will expire and the server reclaims it
            log.warning("job %d: could not report failure", job_id, exc_info=True)
