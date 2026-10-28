"""Piper and Kokoro in a supervised child process (``python -m lectern.tts.child``).

The server's narration used to load the voice into the web server itself: Kokoro brings
PyTorch (gigabytes, every core), Piper ONNX Runtime. Now the runner (and a voice preview)
talks to a child instead, the way OCR and alignment already work, so the server stays
responsive and a voice that runs out of memory fails its job rather than the server.

Limits, from the feature's :class:`lectern.limits.Limit` (``LECTERN_KOKORO_*`` /
``LECTERN_PIPER_*``):

- resident memory: the parent watches ``/proc/<pid>/status`` and kills the child above
  ``memory_mb``; the job fails with :class:`OverMemoryLimit` (not retryable: the same voice
  and limit would fail the same way);
- CPU: the child runs on at most ``threads`` CPUs (affinity, plus the usual thread-count
  variables and ``torch.set_num_threads``), at ``nice`` level ``nice``;
- the out-of-memory killer picks the child first, and it never dumps core.

Protocol: one JSON line in with the voice (``{"engine", "voice", "speed", "piper"}``), one
JSON line out (``{"ok": true, "sample_rate": n}`` or an error); then per sentence a JSON line
``{"text": ...}`` in and ``{"ok": true, "bytes": n}`` plus ``n`` bytes of 16-bit PCM out.
The child's stdout carries only the protocol; anything a library prints goes to stderr,
whose tail is quoted when the child dies.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from lectern.limits import Limit, confine_cpus, lower_priority

MAX_LINE = 64 * 1024
MAX_PCM = 256 * 1024 * 1024  # one sentence of 16-bit PCM; far above any real one
POLL_S = 0.2
# A wedged engine must not hold its slot forever: loading a voice (Kokoro on a Pi is slow)
# and each sentence get this long before the child is killed and the job fails.
START_TIMEOUT_S = 900
SENTENCE_TIMEOUT_S = 300
CLOSE_GRACE_S = 3.0


class OverMemoryLimit(RuntimeError):
    """The engine's child went over its memory cap and was stopped."""


class ChildSynth:
    """A :class:`lectern.tts.engines.Synth` whose engine runs in a child. ``close()`` ends it."""

    def __init__(self, engine: str, spec: dict, limit: Limit, *,
                 command: list[str] | None = None) -> None:  # fmt: skip
        from lectern.tts import engines as eng

        self.engine = engine
        self.limit = limit
        self._stderr = tempfile.TemporaryFile()
        env = {**os.environ, **limit.child_env()}
        if limit.threads:
            env["LECTERN_CHILD_THREADS"] = str(limit.threads)
        self._proc = subprocess.Popen(  # noqa: S603 - fixed argv: this interpreter, our module
            command or [sys.executable, "-m", "lectern.tts.child"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            close_fds=True,
            start_new_session=True,
            env=env,
        )
        self.peak_mb = 0.0
        self._over = False
        self._timed_out = False
        self._killed = False  # we signalled it: its SIGKILL is ours, not the OOM killer's
        self._done = threading.Event()
        self._watch = threading.Thread(target=self._watch_memory, name="lectern-tts-child",
                                       daemon=True)  # fmt: skip
        self._watch.start()
        try:
            reply = self._ask({"engine": engine, **spec}, START_TIMEOUT_S)
        except BaseException:
            self.close()
            raise
        if not reply.get("ok"):
            self.close()
            if reply.get("kind") == "unavailable":
                raise eng.EngineUnavailable(str(reply.get("error") or "engine unavailable"))
            raise RuntimeError(str(reply.get("error") or f"{engine} did not start"))
        self.sample_rate = int(reply["sample_rate"])

    # -- the Synth protocol ---------------------------------------------------------------------

    def sentence(self, text: str, timeout_s: float = SENTENCE_TIMEOUT_S) -> bytes:
        with self._deadline(timeout_s):
            reply = self._ask({"text": text})
            if not reply.get("ok"):
                raise RuntimeError(str(reply.get("error") or f"{self.engine} failed"))
            size = int(reply.get("bytes") or 0)
            if not 0 <= size <= MAX_PCM:
                raise self._died(f"a sentence of {size} bytes")
            pcm = self._proc.stdout.read(size) if size else b""
            if len(pcm) != size:
                raise self._died(f"{len(pcm)} of {size} bytes of audio")
            return pcm

    def close(self) -> None:
        self._done.set()
        with contextlib.suppress(OSError, ValueError):
            self._proc.stdin.close()  # the child's loop ends at EOF
        try:
            self._proc.wait(timeout=CLOSE_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
        self._kill()
        self._proc.wait()
        with contextlib.suppress(OSError, ValueError):
            self._proc.stdout.close()
        self._stderr.close()

    # -- internals ------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _deadline(self, seconds: float):
        """Kill the child if the block has not finished within ``seconds``: the blocked read
        then ends and :meth:`_died` reports the timeout."""

        def expire() -> None:
            self._timed_out = True
            self._kill()

        timer = threading.Timer(seconds, expire)
        timer.daemon = True
        timer.start()
        try:
            yield
        finally:
            timer.cancel()

    def _ask(self, msg: dict, timeout_s: float | None = None) -> dict:
        if timeout_s is not None:
            with self._deadline(timeout_s):
                return self._ask(msg)
        try:
            self._proc.stdin.write(json.dumps(msg).encode() + b"\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline(MAX_LINE)
        except (BrokenPipeError, OSError, ValueError):
            raise self._died() from None
        try:
            reply = json.loads(line)
        except ValueError:
            raise self._died("a reply that is not JSON") from None
        if not isinstance(reply, dict):
            raise self._died("a reply that is not a JSON object")
        return reply

    def _watch_memory(self) -> None:
        from lectern.align.isolate import rss_mb

        cap = self.limit.memory_mb
        while not self._done.wait(POLL_S) and self._proc.poll() is None:
            rss = rss_mb(self._proc.pid)
            if rss is None:
                continue
            self.peak_mb = max(self.peak_mb, rss)
            if cap and rss > cap:
                self._over = True
                self._kill()
                return

    def _kill(self) -> None:
        if self._proc.poll() is None:  # never signal a group id that may be reused
            self._killed = True
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self._proc.pid, signal.SIGKILL)

    def _died(self, invalid: str | None = None) -> Exception:
        """The child is gone (or talking nonsense, ``invalid`` says how): why, as the job's
        error."""
        self._done.set()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self._proc.wait(timeout=CLOSE_GRACE_S)
        self._kill()
        rc = self._proc.wait()
        key = f"LECTERN_{self.engine.upper()}_MEMORY_LIMIT_MB"
        if self._over:
            return OverMemoryLimit(
                f"{self.engine.capitalize()} needed more memory than {key} allows and was "
                f"stopped. Raise {key}, or let a worker (the Mac) narrate this book."
            )
        if self._timed_out:
            return RuntimeError(f"{self.engine.capitalize()} stopped answering and was stopped")
        tail = _tail(self._stderr)
        if invalid and self._killed:
            # it outlived the grace and we stopped it: a protocol failure, worth a retry
            return RuntimeError(f"the {self.engine} process sent an invalid reply ({invalid}) and "
                                "was stopped" + (f": {tail}" if tail else ""))  # fmt: skip
        if rc == -signal.SIGKILL and not self._killed:
            # a SIGKILL nobody here sent: on Linux, the kernel's out-of-memory killer (the
            # child volunteers for it), so retrying the same voice would fail the same way
            return OverMemoryLimit(
                f"{self.engine.capitalize()} was stopped by the system for using too much memory. "
                f"Lower {key}, or let a worker (the Mac) narrate this book."
            )
        if "MemoryError" in tail:
            return OverMemoryLimit(f"{self.engine.capitalize()} ran out of memory ({key}).")
        how = f"signal {-rc}" if rc < 0 else f"exit {rc}"
        return RuntimeError(f"the {self.engine} process ended ({how})" + (f": {tail}" if tail
                                                                           else ""))  # fmt: skip


def _tail(fh, chars: int = 600) -> str:
    with contextlib.suppress(OSError, ValueError):
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - 8192))
        text = fh.read().decode(errors="replace").strip()
        return "\n".join(text.splitlines()[-6:])[-chars:]
    return ""


# --- the child --------------------------------------------------------------------------------


def _confine() -> None:
    with contextlib.suppress(OSError):
        Path("/proc/self/oom_score_adj").write_text("1000")
    lower_priority(10)
    with contextlib.suppress(ImportError, OSError, ValueError):
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    confine_cpus(_threads())


def _threads() -> int | None:
    try:
        return int(os.environ["LECTERN_CHILD_THREADS"]) or None
    except (KeyError, ValueError):
        return None


def build(spec: dict):
    """The engine for ``spec`` (in the child)."""
    from lectern.tts import engines as eng

    speed = float(spec.get("speed") or 1.0)
    if spec.get("engine") == eng.PIPER:
        p = spec["piper"]
        voice = eng.PiperVoiceInfo(p["id"], Path(p["path"]), p.get("language"),
                                   int(p["sample_rate"]), p.get("quality"))  # fmt: skip
        return eng.PiperSynth(voice, speed)
    if spec.get("engine") == eng.KOKORO:
        synth = eng.KokoroSynth(str(spec.get("voice")), speed)
        if (threads := _threads()) and "torch" in sys.modules:
            sys.modules["torch"].set_num_threads(threads)
        return synth
    raise eng.EngineUnavailable(f"no local synthesiser for {spec.get('engine')}")


def serve(make: Callable[[dict], object]) -> int:
    """The child's loop: build the engine with ``make``, then one sentence per request."""
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)  # whatever a library prints goes to stderr, not into the protocol
    sys.stdout = sys.stderr
    _confine()

    def reply(msg: dict, data: bytes = b"") -> None:
        out.write(json.dumps(msg).encode() + b"\n" + data)
        out.flush()

    from lectern.tts.engines import EngineUnavailable

    stdin = sys.stdin.buffer
    try:
        synth = make(json.loads(stdin.readline(MAX_LINE)))
    except EngineUnavailable as exc:
        reply({"ok": False, "kind": "unavailable", "error": str(exc)[:500]})
        return 0
    except Exception as exc:  # noqa: BLE001 - reported to the parent
        reply({"ok": False, "kind": "error", "error": f"{type(exc).__name__}: {exc}"[:500]})
        return 0
    reply({"ok": True, "sample_rate": int(synth.sample_rate)})
    for line in iter(lambda: stdin.readline(MAX_LINE), b""):
        try:
            pcm = synth.sentence(str(json.loads(line)["text"]))
        except MemoryError:
            raise  # dies; the parent reports it as over the memory limit
        except Exception as exc:  # noqa: BLE001 - reported to the parent
            reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]})
            continue
        reply({"ok": True, "bytes": len(pcm)}, pcm)
    return 0


if __name__ == "__main__":
    sys.exit(serve(build))
