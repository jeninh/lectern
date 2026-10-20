"""The alignment child process (``python -m lectern.align.child``), see ``lectern.align.isolate``.

Reads one job spec (JSON) from stdin, runs :func:`lectern.align.pipeline.align_work`, writes
the sentence map and statistics to ``spec["result_path"]``, and reports on stdout, one JSON
object per line: ``{"type": "progress", "progress", "message"}``, then ``{"type": "done"}`` or
``{"type": "error", "error", "retryable"}``. Logs go to stderr.

Exit status: 0 done, 1 error reported, 3 ``MemoryError``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

_out = sys.stdout  # the protocol stream; see _own_stdout


def _emit(msg: dict) -> None:
    _out.write(json.dumps(msg) + "\n")
    _out.flush()


def _own_stdout() -> None:
    """Keep the protocol on a private copy of fd 1 and point fd 1 (and ``sys.stdout``) at
    stderr, so whatever a native library prints lands in the log, not in a JSON line."""
    global _out
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.flush()
        _out = os.fdopen(os.dup(1), "w", buffering=1)
        os.dup2(2, 1)
        sys.stdout = sys.stderr


def _confine() -> None:
    """Best effort, per platform: be the out-of-memory killer's first choice, yield the CPU
    to the server, no core files, and do not outlive the server."""
    with contextlib.suppress(OSError):
        Path("/proc/self/oom_score_adj").write_text("1000")
    with contextlib.suppress(OSError, AttributeError):
        os.nice(10)
    with contextlib.suppress(ImportError, OSError, ValueError):
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    parent = os.getppid()

    def _watch() -> None:
        while True:
            time.sleep(2)
            if os.getppid() != parent:
                os._exit(4)

    threading.Thread(target=_watch, name="parent-watch", daemon=True).start()


def _recorded(path: Path):
    from lectern.align.transcribe import Word

    data = json.loads(path.read_text())
    words = [Word(w[0], float(w[1]), float(w[2]), float(w[3])) for w in data["words"]]

    def transcribe(_tracks, _language, progress):
        progress(1.0)
        return words, data.get("language")

    return transcribe


def run(spec: dict) -> None:
    from lectern.align.pipeline import TrackInput, align_work
    from lectern.align.transcribe import Transcriber, transcribe_tracks

    tracks = [
        TrackInput(
            int(t["position"]),
            Path(t["path"]),
            t["sha256"],
            float(t["duration_s"]),
            float(t["offset_s"]),
        )
        for t in spec["tracks"]
    ]
    if spec.get("transcript"):
        transcribe = _recorded(Path(spec["transcript"]))
        label = spec.get("model_label") or "recorded"
    else:
        opts = dict(spec["transcriber"])
        if opts.get("download_root"):
            opts["download_root"] = Path(opts["download_root"])
        tr = Transcriber(**opts)

        def transcribe(audio, language, progress):
            return transcribe_tracks(tr, audio, language, progress)

        label = tr.label
    last = [0.0]

    def progress(frac: float, message: str) -> None:
        now = time.monotonic()
        if now - last[0] >= 1.0 or frac >= 1.0:  # the parent heartbeats; no need to flood
            last[0] = now
            _emit({"type": "progress", "progress": round(frac, 4), "message": message})

    out = align_work(
        work_id=int(spec["work_id"]),
        epub_path=Path(spec["epub_path"]),
        epub_sha256=spec["epub_sha256"],
        tracks=tracks,
        model_label=label,
        transcribe=transcribe,
        language=spec.get("language"),
        on_progress=progress,
    )
    result = Path(spec["result_path"])
    tmp = result.with_suffix(".tmp")
    with tmp.open("w") as fh:
        fh.write('{"sentence_map": ')
        fh.write(out.sentence_map.model_dump_json())
        fh.write(', "stats": ')
        fh.write(json.dumps(out.stats))
        fh.write("}")
    tmp.replace(result)
    _emit({"type": "done"})


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LECTERN_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    _own_stdout()
    _confine()
    spec = json.loads(sys.stdin.read())
    from lectern.align.transcribe import TranscriptionUnavailable

    try:
        run(spec)
    except MemoryError:
        logging.getLogger("lectern.align.child").exception("out of memory")
        sys.exit(3)
    except TranscriptionUnavailable as exc:
        _emit({"type": "error", "error": f"{type(exc).__name__}: {exc}", "retryable": False})
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - reported to the supervisor
        logging.getLogger("lectern.align.child").exception("alignment failed")
        _emit({"type": "error", "error": f"{type(exc).__name__}: {exc}", "retryable": True})
        sys.exit(1)


if __name__ == "__main__":
    main()
