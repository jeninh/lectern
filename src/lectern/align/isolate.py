"""Run one alignment in a supervised child process (``python -m lectern.align.child``).

The local worker lives in the web server. Transcription is the one step that can use
gigabytes or crash in native code (CTranslate2, PyAV, ONNX Runtime), so the server never runs
it in its own process: it starts a child, feeds it the job on stdin, reads progress lines from
its stdout, and keeps the lease and heartbeats itself. Whatever happens to the child, the
server keeps serving and the job ends in a defined state.

Limits (all best effort, so they work wherever the platform allows):

- the child asks the kernel to pick it first when memory runs out (``oom_score_adj`` 1000 on
  Linux), so an out-of-memory kill takes the alignment and not the server;
- the supervisor watches the child's resident memory (``/proc/<pid>/status``) and kills it
  above ``memory_limit_mb``;
- the child runs at a lower CPU priority (``nice`` +10), never dumps core, and exits when the
  server that started it goes away.

Outcomes (:class:`Outcome.kind`): ``ok``; ``error`` (the pipeline raised; retryable as decided
by the child); ``oom`` (the memory cap, a ``MemoryError``, or a ``SIGKILL`` nobody here sent,
which on Linux is the out-of-memory killer): **not retryable**, since the same input, model,
and limit would fail the same way again; ``crash`` (any other signal or exit code): retryable
under the job's attempts and backoff; ``stopped`` and ``abandoned`` when the supervisor ended
it (server shutdown, lost lease). The child's stderr is kept in a log file under
``<data>/logs/align/`` and its tail is quoted in the job's error.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("lectern.align.isolate")

CHILD_MODULE = "lectern.align.child"
EXIT_ERROR = 1
EXIT_MEMORY = 3
POLL_S = 0.2
TERM_GRACE_S = 3.0
KEEP_LOGS = 50
# Of the memory available to the server, what the child may use by default.
AUTO_LIMIT_FRACTION = 0.75


@dataclass
class Outcome:
    kind: str  # ok | error | oom | crash | stopped | abandoned
    result: dict | None = None  # {"sentence_map": {...}, "stats": {...}} when ok
    error: str | None = None
    retryable: bool = False
    returncode: int | None = None
    peak_rss_mb: float | None = None
    log_path: Path | None = None


def default_command() -> list[str]:
    return [sys.executable, "-m", CHILD_MODULE]


# --- memory -------------------------------------------------------------------------------------


def available_memory_mb() -> float | None:
    """Physical memory, or the cgroup's limit when lower (containers); ``None`` if unknown."""
    values = []
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                values.append(int(line.split()[1]) / 1024)
                break
    except (OSError, ValueError, IndexError):
        pass
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < 1 << 60:
            values.append(int(raw) / 2**20)
    return min(values) if values else None


def resolve_limit_mb(configured: int | None) -> int | None:
    """``None`` (unset) → 75 % of :func:`available_memory_mb`; ``0`` → no cap."""
    if configured is not None:
        return configured or None
    total = available_memory_mb()
    return int(total * AUTO_LIMIT_FRACTION) if total else None


def rss_mb(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


# --- logs -----------------------------------------------------------------------------------------


def log_path_for(data_dir: Path, job_id: int, attempt: int) -> Path:
    d = data_dir / "logs" / "align"
    d.mkdir(parents=True, exist_ok=True)
    logs = sorted(d.glob("job-*.log"), key=lambda p: p.stat().st_mtime)
    for old in logs[: max(0, len(logs) - KEEP_LOGS + 1)]:
        old.unlink(missing_ok=True)
    return d / f"job-{job_id}-attempt-{attempt}.log"


def _tail(path: Path | None, lines: int = 12, chars: int = 1200) -> str:
    if path is None:
        return ""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 16384))
            text = fh.read().decode(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])[-chars:]


# --- running ------------------------------------------------------------------------------------


def run(
    spec: dict,
    *,
    workdir: Path,
    log_path: Path | None,
    on_progress: Callable[[float, str], None],
    interrupt: Callable[[], str | None],
    memory_limit_mb: int | None,
    command: list[str] | None = None,
) -> Outcome:
    """Run the child for ``spec`` until it exits or ``interrupt()`` returns ``"stop"`` (hand
    the job back) or ``"abandon"`` (the lease is gone)."""
    workdir.mkdir(parents=True, exist_ok=True)
    result_path = workdir / f"align-result-{os.getpid()}-{threading.get_ident()}.json"
    result_path.unlink(missing_ok=True)
    spec = {**spec, "result_path": str(result_path)}
    stderr = log_path.open("ab") if log_path else subprocess.DEVNULL
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv: this interpreter and our module
            command or default_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            close_fds=True,
            start_new_session=True,  # a Ctrl-C to the server's terminal is the server's
        )
    finally:
        if log_path:
            stderr.close()  # type: ignore[union-attr]
    messages: dict = {"error": None, "retryable": True, "done": False}

    def _read() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            kind = msg.get("type") if isinstance(msg, dict) else None
            if kind == "progress":
                with contextlib.suppress(TypeError, ValueError):
                    on_progress(float(msg["progress"]), str(msg.get("message", ""))[:200])
            elif kind == "error":
                messages["error"] = str(msg.get("error", ""))[:1500]
                messages["retryable"] = bool(msg.get("retryable", True))
            elif kind == "done":
                messages["done"] = True

    reader = threading.Thread(target=_read, name="lectern-align-child-out", daemon=True)
    reader.start()
    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(spec).encode())
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass  # the child died at once; its exit status says how

    peak = 0.0
    ended_by: str | None = None
    while proc.poll() is None:
        reason = interrupt()
        if reason is not None:
            ended_by = reason
            _terminate(proc, graceful=reason == "stop")
            break
        rss = rss_mb(proc.pid)
        if rss is not None:
            peak = max(peak, rss)
            if memory_limit_mb and rss > memory_limit_mb:
                ended_by = "memory"
                log.warning(
                    "alignment child %d uses %.0f MiB, over the %d MiB limit: killing it",
                    proc.pid,
                    rss,
                    memory_limit_mb,
                )
                _terminate(proc, graceful=False)
                break
        time.sleep(POLL_S)
    rc = proc.wait()
    reader.join(timeout=5)
    peak_mb = round(peak, 1) if peak else None
    try:
        return _classify(rc, ended_by, messages, result_path, log_path, peak_mb, memory_limit_mb)
    finally:
        result_path.unlink(missing_ok=True)


def _terminate(proc: subprocess.Popen, *, graceful: bool) -> None:
    with contextlib.suppress(ProcessLookupError):
        if graceful:
            proc.terminate()
            try:
                proc.wait(TERM_GRACE_S)
                return
            except subprocess.TimeoutExpired:
                pass
        proc.kill()


def _signal_name(rc: int) -> str:
    try:
        return signal.Signals(-rc).name
    except ValueError:
        return f"signal {-rc}"


def _classify(
    rc: int,
    ended_by: str | None,
    messages: dict,
    result_path: Path,
    log_path: Path | None,
    peak_mb: float | None,
    limit_mb: int | None,
) -> Outcome:
    common = {"returncode": rc, "peak_rss_mb": peak_mb, "log_path": log_path}
    if ended_by in ("stop", "abandon"):
        return Outcome("stopped" if ended_by == "stop" else "abandoned", **common)
    if rc == 0 and messages["done"]:
        try:
            return Outcome("ok", result=json.loads(result_path.read_text()), **common)
        except (OSError, ValueError) as exc:
            return Outcome(
                "crash", error=f"alignment child gave no readable result: {exc}", retryable=True,
                **common,
            )  # fmt: skip
    tail = _tail(log_path)
    where = f" (log: {log_path.name})" if log_path else ""
    seen = f"; peak {peak_mb:.0f} MiB" if peak_mb else ""
    advice = (
        " Lower LECTERN_ALIGN_WINDOW_SECONDS, use a smaller model, raise "
        "LECTERN_ALIGN_MEMORY_LIMIT_MB, or run `lectern worker` on a bigger machine."
    )
    if ended_by == "memory":
        error = f"out of memory: the alignment used more than {limit_mb} MiB{seen}.{advice}"
        return Outcome("oom", error=_with_tail(error + where, tail), **common)
    if rc == EXIT_MEMORY:
        error = f"out of memory: MemoryError in the alignment{seen}.{advice}"
        return Outcome("oom", error=_with_tail(error + where, tail), **common)
    if rc == -signal.SIGKILL:
        error = (
            "out of memory: the alignment was killed (SIGKILL), most likely by the system's "
            f"out-of-memory killer{seen}.{advice}"
        )
        return Outcome("oom", error=_with_tail(error + where, tail), **common)
    if rc == EXIT_ERROR and messages["error"]:
        return Outcome("error", error=messages["error"], retryable=messages["retryable"], **common)
    how = f"killed by {_signal_name(rc)}" if rc < 0 else f"exited with status {rc}"
    error = f"alignment process crashed: {how}{seen}{where}"
    return Outcome("crash", error=_with_tail(error, tail), retryable=True, **common)


def _with_tail(error: str, tail: str) -> str:
    return f"{error}\n--- last log lines ---\n{tail}" if tail else error
