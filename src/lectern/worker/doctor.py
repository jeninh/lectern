"""``lectern worker-doctor``: can this machine run what it would offer the server?

For every capability a worker can advertise it checks, in order, that the engine imports,
that its models are on disk, and that a tiny job works end to end:

- ``align``: the transcriber (MLX on Apple Silicon, else faster-whisper) loads its model and
  transcribes a second of audio.
- ``tts-kokoro``: Kokoro and its English G2P import, spaCy's ``en_core_web_sm`` is installed
  (misaki would otherwise try to ``pip install`` it in the middle of a job), the weights and a
  voice are cached, ffmpeg is on the PATH, and one sentence is synthesized.
- ``transcode``: ffmpeg encodes a fraction of a second of AAC.

``--prepare`` downloads what is missing first, with progress: the alignment model, Kokoro's
weights and every voice Lectern offers, and (with a working ``uv`` or ``pip``) the pinned
spaCy model, which repairs an install made before it was a dependency. Without
``--prepare`` nothing is downloaded (``HF_HUB_OFFLINE=1``), so a missing model is reported.

The worker agent runs the same checks with ``--prepare`` in a child process when it starts
(:func:`check_isolated`): an engine that exits or crashes the process fails its check instead
of the worker, and only capabilities that pass are offered. Those that fail are reported to
the server with the reason (Admin → Workers).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

CAPABILITIES = ("align", "tts-kokoro", "transcode")
# Long enough for a first download of the alignment model and Kokoro on a slow link.
CHILD_TIMEOUT_S = 3600.0
SMOKE_TEXT = "Hello from Lectern."

Say = Callable[[str], None]


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Result:
    capability: str
    steps: list[Step] = field(default_factory=list)
    engine: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)

    @property
    def reason(self) -> str | None:
        bad = next((s for s in self.steps if not s.ok), None)
        return None if bad is None else f"{bad.name}: {bad.detail}"[:200]

    def json(self) -> dict:
        return {**asdict(self), "ok": self.ok, "reason": self.reason}

    @classmethod
    def from_json(cls, data: dict) -> Result:
        return cls(data["capability"], [Step(**s) for s in data.get("steps") or []],
                   data.get("engine"))  # fmt: skip


class _Failed(Exception):
    pass


def _step(result: Result, name: str, fn: Callable[[], str | None]) -> bool:
    """Run one check. Any failure, ``SystemExit`` included, fails the step, not the caller."""
    try:
        detail = fn() or ""
    except KeyboardInterrupt:
        raise
    except _Failed as exc:
        result.steps.append(Step(name, False, str(exc)[:300]))
        return False
    except SystemExit as exc:
        result.steps.append(Step(name, False, f"called sys.exit({exc.code!r})"))
        return False
    except BaseException as exc:  # noqa: BLE001 - every failure is the check's result
        result.steps.append(Step(name, False, f"{type(exc).__name__}: {exc}"[:300]))
        return False
    result.steps.append(Step(name, True, detail))
    return True


def _installer() -> list[str] | None:
    """How to install into this interpreter's environment (a uv tool env has no pip)."""
    import importlib.util

    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install"]
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    if Path(uv).is_file():
        return [uv, "pip", "install", "--python", sys.executable]
    return None


def fix_spacy_command() -> str:
    from lectern.tts.engines import SPACY_MODEL_URL

    return f"uv pip install --python {sys.executable} {SPACY_MODEL_URL}"


# --- checks -----------------------------------------------------------------------------------


def check_kokoro(*, prepare: bool, install: bool, say: Say) -> Result:
    import importlib

    from lectern.tts import engines as eng

    r = Result("tts-kokoro", engine=eng.KOKORO_MODEL)

    def imports() -> str:
        try:
            importlib.import_module("kokoro")
            importlib.import_module("misaki.en")
        except ImportError as exc:
            raise _Failed(f"{exc}; reinstall with the mac (or kokoro) extra") from exc
        return "kokoro, misaki"

    def spacy_model() -> str:
        import spacy.util

        if spacy.util.is_package(eng.SPACY_MODEL):
            return eng.SPACY_MODEL
        cmd = _installer()
        if not install or cmd is None:
            raise _Failed(f"{eng.SPACY_MODEL} is not installed: run `lectern worker-doctor "
                          f"--prepare` or `{fix_spacy_command()}`")  # fmt: skip
        say(f"installing {eng.SPACY_MODEL} (Kokoro's English text analysis)")
        done = subprocess.run([*cmd, eng.SPACY_MODEL_URL], check=False)  # noqa: S603
        importlib.invalidate_caches()
        if done.returncode != 0 or not spacy.util.is_package(eng.SPACY_MODEL):
            raise _Failed(f"installing {eng.SPACY_MODEL} failed; run `{fix_spacy_command()}`")
        return f"{eng.SPACY_MODEL} installed"

    def ffmpeg() -> str:
        found = shutil.which("ffmpeg")
        if not found:
            raise _Failed("ffmpeg is not on the PATH (brew install ffmpeg)")
        return found

    def weights() -> str:
        from huggingface_hub import hf_hub_download

        voices = eng.KOKORO_VOICES if prepare else ("af_heart",)
        files = [*eng.KOKORO_FILES, *(f"voices/{v}.pt" for v in voices)]
        for i, name in enumerate(files, 1):
            if prepare:
                say(f"Kokoro {i}/{len(files)}: {name}")
            try:
                hf_hub_download(eng.KOKORO_REPO, name)
            except Exception as exc:  # noqa: BLE001
                if prepare:
                    raise _Failed(f"could not download {name}: {exc}"[:300]) from exc
                raise _Failed(f"{name} is not downloaded yet: run `lectern worker-doctor "
                              "--prepare`") from exc  # fmt: skip
        return f"{len(files)} files in the Hugging Face cache"

    def synthesis() -> str:
        import numpy as np

        from lectern.worker.runners import KOKORO_SAMPLE_RATE, kokoro_synthesizer

        started = time.monotonic()
        audio = np.asarray(kokoro_synthesizer("af_heart", 1.0, "en-us")(SMOKE_TEXT))
        if audio.size < KOKORO_SAMPLE_RATE // 5 or not np.isfinite(audio).all():
            raise _Failed(f"synthesis returned {audio.size} samples")
        if float(np.abs(audio).max()) < 1e-3:
            raise _Failed("synthesis returned silence")
        return f"{audio.size / KOKORO_SAMPLE_RATE:.1f} s of speech in {time.monotonic() - started:.1f} s"

    for name, fn in (("import", imports), ("spaCy model", spacy_model), ("ffmpeg", ffmpeg),
                     ("weights", weights), ("synthesis", synthesis)):  # fmt: skip
        if not _step(r, name, fn):
            break
    return r


def check_align(*, engine: str, model: str, say: Say, prepare: bool) -> Result:
    r = Result("align")
    state: dict = {}

    def load_engine() -> str:
        from lectern.align.mlx import worker_transcriber

        tr = worker_transcriber(engine, model)
        if tr is None:
            raise _Failed("no transcription engine installed (the mac or align extra)")
        state["tr"] = tr
        return type(tr).__name__

    def load_model() -> str:
        tr = state["tr"]
        if prepare:
            say(f"alignment model {model}: loading (downloads once)")
        tr.load()
        r.engine = tr.label
        if getattr(tr, "using_fallback", False):
            return f"{tr.label} (MLX would not load: using the CPU)"
        return tr.label

    def transcription() -> str:
        import numpy as np

        started = time.monotonic()
        state["tr"]._window_words(np.zeros(16000, dtype=np.float32), 0.0, "en", lambda _t: None)
        r.engine = state["tr"].label
        return f"1 s transcribed in {time.monotonic() - started:.1f} s"

    for name, fn in (("engine", load_engine), ("model", load_model),
                     ("transcription", transcription)):  # fmt: skip
        if not _step(r, name, fn):
            break
    return r


def check_transcode(*, say: Say) -> Result:
    r = Result("transcode", engine="ffmpeg")

    def encode() -> str:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise _Failed("ffmpeg is not on the PATH (brew install ffmpeg)")
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "t.m4a"
            done = subprocess.run(  # noqa: S603
                [ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                 "sine=frequency=440:duration=0.2", "-c:a", "aac", "-b:a", "64k", str(out)],
                capture_output=True, timeout=60, check=False,
            )  # fmt: skip
            if done.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
                raise _Failed(done.stderr.decode(errors="replace")[-300:] or "ffmpeg failed")
        return "AAC encode works"

    _step(r, "encode", encode)
    return r


def check(caps, *, prepare: bool = False, install: bool = False, engine: str = "auto",
          model: str = "small", say: Say = lambda _m: None) -> dict[str, Result]:  # fmt: skip
    out: dict[str, Result] = {}
    for cap in caps:
        say(f"checking {cap}")
        if cap == "align":
            out[cap] = check_align(engine=engine, model=model, say=say, prepare=prepare)
        elif cap == "tts-kokoro":
            out[cap] = check_kokoro(prepare=prepare, install=install, say=say)
        elif cap == "transcode":
            out[cap] = check_transcode(say=say)
        else:
            out[cap] = Result(cap, [Step("known", False, "this worker cannot run it")])
    return out


def check_isolated(caps, *, prepare: bool = True, engine: str = "auto", model: str = "small",
                   timeout_s: float = CHILD_TIMEOUT_S) -> dict[str, Result]:  # fmt: skip
    """:func:`check` in a child process (its progress goes to this process's stderr, the
    worker log). A child that dies fails every capability it had not reported."""
    caps = list(caps)
    if not caps:
        return {}
    argv = [sys.executable, "-m", "lectern.worker.doctor", "--json", "--capabilities",
            ",".join(caps), "--engine", engine, "--model", model]  # fmt: skip
    if prepare:
        argv.append("--prepare")
    why = None
    try:
        done = subprocess.run(argv, stdout=subprocess.PIPE, timeout=timeout_s, check=False)  # noqa: S603
        lines = done.stdout.decode(errors="replace").strip().splitlines()
        data = json.loads(lines[-1]) if lines else None
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            got = {d["capability"]: Result.from_json(d) for d in data["results"]}
            return {c: got.get(c) or Result(c, [Step("self-test", False, "no result")])
                    for c in caps}  # fmt: skip
        why = f"the self-test process exited with {done.returncode}"
    except subprocess.TimeoutExpired:
        why = f"the self-test took longer than {timeout_s / 60:.0f} min"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        why = f"the self-test could not run: {exc}"
    return {c: Result(c, [Step("self-test", False, why)]) for c in caps}


# --- command line -----------------------------------------------------------------------------


def _network() -> None:
    """Downloads go through the worker's proxy and extra CAs, as in the agent."""
    from lectern.worker import config as wcfg
    from lectern.worker.conditions import apply_network_env

    home = wcfg.worker_home()
    try:
        cfg = wcfg.load_config(home)
    except (OSError, ValueError):
        return
    if cfg.ca_bundle and not Path(cfg.ca_bundle).expanduser().is_file():
        return
    apply_network_env(cfg.proxy, cfg.ca_bundle, home)


def build_parser(p: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    p = p or argparse.ArgumentParser(
        prog="lectern worker-doctor", description=__doc__.splitlines()[0]
    )
    p.add_argument("--prepare", action="store_true",
                   help="download models and voices, and install the spaCy model, first")  # fmt: skip
    p.add_argument("--capabilities", default=",".join(CAPABILITIES),
                   help="comma-separated (default: align,tts-kokoro,transcode)")  # fmt: skip
    p.add_argument("--engine", choices=("auto", "mlx", "cpu"), default=None,
                   help="alignment engine (default: the worker's config)")  # fmt: skip
    p.add_argument("--model", default=None, help="alignment model (default: the worker's config)")
    p.add_argument("--json", action="store_true", help="one JSON line on stdout")
    p.add_argument("--no-restart", action="store_true",
                   help="do not restart the worker service after --prepare")  # fmt: skip
    return p


def run(args: argparse.Namespace) -> int:
    from lectern.worker import config as wcfg

    if not args.prepare:
        os.environ["HF_HUB_OFFLINE"] = "1"  # check what is there; download nothing
    for name in ("httpx", "httpx2", "httpcore"):  # one line per request drowns the progress
        logging.getLogger(name).setLevel(logging.WARNING)
    _network()
    cfg = wcfg.load_config()
    caps = [c.strip() for c in args.capabilities.split(",") if c.strip()]

    def say(msg: str) -> None:
        print(f"==> {msg}", file=sys.stderr, flush=True)

    results = check(caps, prepare=args.prepare, install=args.prepare,
                    engine=args.engine or cfg.engine, model=args.model or cfg.model, say=say)  # fmt: skip
    if args.json:
        print(json.dumps({"results": [r.json() for r in results.values()]}), flush=True)
    else:
        for r in results.values():
            print(
                f"{'ok  ' if r.ok else 'FAIL'} {r.capability}"
                + (f"  ({r.engine})" if r.engine else "")
            )
            for s in r.steps:
                print(
                    f"       {'✓' if s.ok else '✗'} {s.name}"
                    + (f": {s.detail}" if s.detail else "")
                )
        if args.prepare and not args.no_restart and sys.platform == "darwin":
            from lectern.worker.agent import restart_service

            if restart_service():
                print("restarted the worker: it offers what passed")
    return 0 if all(r.ok for r in results.values()) else 1


def main(argv: list[str] | None = None) -> None:
    sys.exit(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    main()
