"""The server's own tts runner: Piper (the Raspberry Pi fallback), ElevenLabs (server-only:
the key never leaves this process), and Kokoro when it is installed here.

It claims through :func:`lectern.tts.jobs.claim_local`, heartbeats with progress while
:func:`lectern.tts.synth.run` narrates, and applies the result through the same
:func:`lectern.tts.jobs.apply_result` a worker's upload goes through. While a worker that
advertises ``tts-<engine>`` is online (``lectern.queue.capability_online``, when that module is
present) this runner leaves that engine's new jobs to it for ``LECTERN_LOCAL_DEFER_SECONDS``.
"""

from __future__ import annotations

import logging
import shutil
import threading
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from lectern import integrations, queue
from lectern.align.jobs import LeaseLost, ResultError, verify_lease
from lectern.align.worker import MAX_HEARTBEAT_S, JobAbandoned, WorkerStopping, _Heartbeat
from lectern.config import Settings
from lectern.models import Job, Work, utcnow
from lectern.security import audit
from lectern.tts import elevenlabs as el
from lectern.tts import engines as eng
from lectern.tts import jobs as tj
from lectern.tts import synth

log = logging.getLogger("lectern.tts.runner")

SynthFactory = Callable[[str, dict, Settings], eng.Synth]


def default_synth(engine: str, payload: dict, settings: Settings) -> eng.Synth:
    speed = float(payload.get("speed") or 1.0)
    if engine == eng.PIPER:
        voice = next((v for v in eng.piper_voices(settings) if v.id == payload.get("voice")), None)
        if voice is None:
            raise eng.EngineUnavailable(f"Piper voice {payload.get('voice')!r} is not installed")
        return eng.PiperSynth(voice, speed)
    if engine == eng.KOKORO:
        return eng.KokoroSynth(str(payload.get("voice")), speed)
    raise eng.EngineUnavailable(f"no local synthesiser for {engine}")


def local_engines(db, settings: Settings) -> set[str]:
    """Engines this server can run right now."""
    out = set()
    if eng.piper_installed() and eng.piper_voices(settings):
        out.add(eng.PIPER)
    if settings.tts_kokoro_local and eng.kokoro_installed():
        out.add(eng.KOKORO)
    if integrations.active(db, integrations.ELEVENLABS) is not None:
        out.add(eng.ELEVENLABS)
    return out


def deferred(db, engines: set[str]) -> set[str]:
    return {e for e in engines if e != eng.ELEVENLABS and queue.capability_online(db, f"tts-{e}")}


class TTSRunner:
    def __init__(
        self,
        session_factory: sessionmaker,
        settings: Settings,
        *,
        synth_factory: SynthFactory | None = None,
        engines: Callable[[object], set[str]] | None = None,
        eleven_transport: el.Transport | None = None,
        eleven_sleep: Callable[[float], None] | None = None,
        poll_s: float = 10.0,
        name: str = "local",
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.synth_factory = synth_factory or default_synth
        self._engines = engines or (lambda db: local_engines(db, settings))
        self.eleven_transport = eleven_transport
        self.eleven_sleep = eleven_sleep
        self.poll_s = poll_s
        self.name = name
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        with self.session_factory() as db:
            if n := tj.reclaim(db, orphaned_local=True):
                log.warning("requeued %d narration job(s) interrupted by a restart", n)
        self._thread = threading.Thread(target=self._loop, name="lectern-tts", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ran = self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("tts runner loop failed")
                ran = False
            if not ran:
                self._wake.wait(self.poll_s)
                self._wake.clear()

    # -- one job -------------------------------------------------------------------------------

    def run_once(self) -> bool:
        with self.session_factory() as db:
            engines = self._engines(db)
            if not engines:
                tj.reclaim(db)
                return False
            defer = deferred(db, engines)
            claimed = tj.claim_local(
                db, engines, worker=self.name, defer=defer,
                defer_s=float(getattr(self.settings, "local_defer_seconds", 600)),
            )  # fmt: skip
            if claimed is None:
                return False
            job, token = claimed
            job_id, payload, work_id = job.id, dict(job.payload or {}), job.work_id
            work = db.get(Work, work_id)
            tags = {"album": work.title if work else "", "artist": work.author if work else "",
                    "genre": "Audiobook", "comment": f"Narrated by {payload.get('engine')} "
                    f"({payload.get('voice')}) in Lectern"}  # fmt: skip
        log.info("tts job %d: narrating work %s with %s/%s", job_id, work_id,
                 payload.get("engine"), payload.get("voice"))  # fmt: skip
        latest: dict = {}

        def send(progress: float, message: str) -> bool:
            with self.session_factory() as db:
                try:
                    row = verify_lease(db, job_id, token)
                except LeaseLost:
                    return False
                tj.heartbeat(db, row, progress=progress, message=message, stats=dict(latest))
                return True

        hb = _Heartbeat(send, min(tj.LEASE_S / 3, MAX_HEARTBEAT_S), self._stop)
        hb.start()

        def progress(frac: float, message: str, stats: dict) -> None:
            latest.clear()
            latest.update(stats)
            hb.on_progress(frac, message)

        artifacts = queue.artifacts_dir(self.settings.data_dir, job_id)
        scratch = self.settings.data_dir / "tmp" / f"tts-{job_id}"
        try:
            with self.session_factory() as db:
                script = tj.load_job_script(self.settings.data_dir, payload)
                plan = (
                    self._eleven_plan(db, job_id, payload)
                    if payload.get("engine") == eng.ELEVENLABS
                    else None
                )
            voice = None if plan else self.synth_factory(payload["engine"], payload, self.settings)
            body = synth.run(
                synth.Run(
                    payload=payload,
                    script=script,
                    artifacts=artifacts,
                    scratch=scratch,
                    ffmpeg=self.settings.ffmpeg_path,
                    tags=tags,
                    progress=progress,
                    synth=voice,
                    eleven=plan,
                )  # fmt: skip
            )
        except JobAbandoned:
            log.warning("tts job %d: lease lost (cancelled?), stopping", job_id)
            return True
        except WorkerStopping:
            self._with_lease(job_id, token, lambda db, j: tj.release(db, j))
            return True
        except LeaseLost:
            self._fail(job_id, token, "the book's text changed; queue it again", retryable=False)
            return True
        except (eng.EngineUnavailable, synth.SpendingCapReached, integrations.Undecryptable) as exc:
            self._fail(job_id, token, _text(exc), retryable=False)
            return True
        except el.ElevenLabsError as exc:
            self._fail(job_id, token, f"ElevenLabs: {exc.message}", retryable=exc.retryable)
            return True
        except Exception as exc:  # noqa: BLE001 - every failure is reported
            log.exception("tts job %d failed", job_id)
            self._fail(job_id, token, _text(exc), retryable=True)
            return True
        finally:
            hb.stop()
            shutil.rmtree(scratch, ignore_errors=True)
        self._submit(job_id, token, body, artifacts, dict(latest))
        return True

    def _eleven_plan(self, db, job_id: int, payload: dict) -> synth.ElevenLabsPlan:
        key = integrations.secret(db, self.settings, integrations.ELEVENLABS)
        if key is None:
            raise eng.EngineUnavailable("the ElevenLabs key was removed; add it again to continue")
        client = el.Client(
            key,
            self.eleven_transport,
            **({"sleep": self.eleven_sleep} if self.eleven_sleep else {}),
        )
        group = payload.get("cap_group") or job_id

        def spent() -> float:
            with self.session_factory() as s:
                return tj.spent_credits(s, group)

        def bill(characters: int, credits: float) -> None:
            with self.session_factory() as s:
                job = s.get(Job, job_id)
                tj.record_usage(s, provider=integrations.ELEVENLABS, kind="tts",
                                characters=characters, credits=credits, job=job)  # fmt: skip
                row = integrations.active(s, integrations.ELEVENLABS)
                if row is not None:
                    row.last_used_at = utcnow()
                s.commit()

        return synth.ElevenLabsPlan(
            client=client,
            model=str(payload.get("model") or el.DEFAULT_MODEL),
            voice_settings=payload.get("voice_settings") or None,
            credits_per_character=float(payload.get("credits_per_character") or 1.0),
            cap_credits=float(payload.get("cap_credits") or 0.0),
            spent=spent,
            bill=bill,
            cache_dir=self.settings.data_dir / "tts" / "cache" / "elevenlabs",
        )

    def _submit(self, job_id: int, token: str, body: bytes, artifacts: Path, stats: dict) -> None:
        with self.session_factory() as db:
            try:
                job = verify_lease(db, job_id, token)
                tj.heartbeat(db, job, progress=1.0, message="publishing", stats=stats)
                out = tj.apply_result(db, self.settings, job, body, artifacts)
            except LeaseLost:
                db.rollback()
                log.warning("tts job %d: lease lost before the edition was installed", job_id)
                return
            except ResultError as exc:
                db.rollback()
                job = db.get(Job, job_id)
                tj.fail(db, job, f"{exc.code}: {'; '.join(exc.errors)[:1500]}", retryable=False)
                return
            job = db.get(Job, job_id)
            if (job.payload or {}).get("engine") == eng.ELEVENLABS:
                audit.record(db, "integration.elevenlabs.use", actor_user_id=job.created_by,
                             target=f"job:{job_id}",
                             detail=f"{tj.spent_credits(db, job.payload.get('cap_group')):.0f} credits "
                             f"for work {job.work_id}")  # fmt: skip
                db.commit()
        shutil.rmtree(artifacts.parent, ignore_errors=True)
        log.info("tts job %d: %s", job_id, out)

    def _with_lease(self, job_id: int, token: str, fn) -> None:
        with self.session_factory() as db:
            try:
                job = verify_lease(db, job_id, token)
            except LeaseLost:
                return
            fn(db, job)

    def _fail(self, job_id: int, token: str, error: str, *, retryable: bool) -> None:
        log.warning("tts job %d: %s", job_id, error)
        self._with_lease(job_id, token, lambda db, j: tj.fail(db, j, error, retryable=retryable))


def _text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000] if not str(exc) else str(exc)[:2000]
