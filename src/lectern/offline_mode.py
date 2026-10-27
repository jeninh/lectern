"""Offline mode (docs/offline-mode.md): the switch, what would contact the internet, and
downloading models ahead of time so alignment and narration still work offline.

Offline mode is on when ``LECTERN_OFFLINE=1`` (then Admin cannot turn it off) or when the
owner turns it on in Admin (``instance_settings.offline_mode``). While it is on,
:mod:`lectern.egress` refuses every outbound connection; metadata suggestions and ElevenLabs
also say so up front instead of failing on the network.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session

from lectern import egress, instance, integrations
from lectern.config import Settings

KEY = "offline_mode"
HF = "huggingface.co"
PIPER_DEFAULT_VOICE = "en_US-lessac-medium"
# The voices piper's own downloader fetches (piper.download_voices), at the same pinned release.
PIPER_VOICE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/{family}/{code}/{name}/{quality}/{voice}{ext}"
_PIPER_VOICE = re.compile(r"^(?P<code>[a-z]{2,3}_[A-Z]{2})-(?P<name>[A-Za-z0-9_]+)-(?P<quality>x_low|low|medium|high)$")  # fmt: skip
PIPER_MAX_BYTES = 256 * 1024 * 1024
PREPARE_TIMEOUT_S = 6 * 3600


def forced(settings: Settings) -> bool:
    return settings.offline


def is_on(db: Session, settings: Settings) -> bool:
    return settings.offline or instance.get(db, KEY)


def apply(db: Session, settings: Settings) -> bool:
    """Make this process enforce what the settings say; returns whether offline mode is on."""
    on = is_on(db, settings)
    egress.enforce(on)
    return on


# --- what would contact the internet -------------------------------------------------------------


def models_dir(settings: Settings) -> Path:
    """Where the server's alignment models live (the local worker's ``download_root``)."""
    return settings.data_dir / "models"


def whisper_ready(settings: Settings, model: str) -> bool:
    """Whether faster-whisper ``model`` is in :func:`models_dir`, judged from the Hugging Face
    cache layout so that checking needs neither faster-whisper nor the network."""
    if Path(model).is_absolute():
        return (Path(model) / "model.bin").is_file()
    root = models_dir(settings)
    if "/" in model:
        repos = [root / f"models--{model.replace('/', '--')}"]
    else:
        name = (
            f"faster-distil-whisper-{model.removeprefix('distil-')}"
            if model.startswith("distil-")
            else f"faster-whisper-{model}"
        )
        repos = list(root.glob(f"models--*--{name}")) if root.is_dir() else []
    return any(any(r.glob("snapshots/*/model.bin")) for r in repos)


def _kokoro_files() -> list[str]:
    from lectern.tts import engines as eng

    return [*eng.KOKORO_FILES, *(f"voices/{v}.pt" for v in eng.KOKORO_VOICES)]


def kokoro_ready() -> bool:
    if importlib.util.find_spec("huggingface_hub") is None:
        return False
    from huggingface_hub import try_to_load_from_cache

    from lectern.tts import engines as eng

    return all(isinstance(try_to_load_from_cache(eng.KOKORO_REPO, f), str) for f in _kokoro_files())


def models(settings: Settings) -> dict:
    from lectern.align.jobs import local_engine_missing
    from lectern.align.transcribe import MODELS
    from lectern.tts import engines as eng

    local_align = local_engine_missing(settings) is None
    kokoro_local = settings.tts_kokoro_local and eng.kokoro_installed()
    return {
        "align": {
            "local": local_align,
            "model": settings.align_model,
            "ready": local_align and whisper_ready(settings, settings.align_model),
            "downloaded": [m for m in MODELS if whisper_ready(settings, m)] if local_align else [],
        },
        "kokoro": {"local": kokoro_local, "ready": kokoro_local and kokoro_ready()},
        "piper": {
            "installed": eng.piper_installed(),
            "voices": [v.id for v in eng.piper_voices(settings)],
        },
    }


def features(db: Session, settings: Settings, models_: dict | None = None) -> list[dict]:
    """Every server feature that can contact the internet, and whether it would right now.

    ``on``: the feature is set up to contact the internet (with offline mode off, it would);
    ``blocked``: it is on and offline mode refuses it."""
    m = models_ or models(settings)
    offline = egress.offline()
    el = integrations.status(db, settings, integrations.ELEVENLABS)
    align, kokoro = m["align"], m["kokoro"]
    rows = [
        {
            "id": "metadata_lookup",
            "name": "Metadata suggestions",
            "contacts": ["openlibrary.org", "www.googleapis.com"],
            "on": instance.get(db, "metadata_lookup_enabled"),
            "when": "When someone asks for suggestions in the book editor, or runs Fix metadata "
            "on books in the Library: the ISBN, or the title and first author, is sent.",
        },
        {
            "id": "elevenlabs",
            "name": "ElevenLabs narration",
            "contacts": ["api.elevenlabs.io"],
            "on": bool(el["configured"]),
            "when": "When the owner saves or tests the key, lists voices, previews, estimates, "
            "or narrates with ElevenLabs: the key and the chosen chapters' text are sent.",
        },
        {
            "id": "align_model",
            "name": "Alignment model download",
            "contacts": [HF],
            "on": align["local"] and not align["ready"],
            "when": (
                f"Once, at this server's first alignment: the {align['model']} model is "
                "downloaded into the data directory (a job that asks for another model "
                "downloads that one too)."
                if align["local"] and not align["ready"]
                else f"The {align['model']} model is downloaded; aligning needs no internet."
                if align["local"]
                else "This server does not align audiobooks itself."
            ),
        },
        {
            "id": "kokoro_model",
            "name": "Kokoro voice download",
            "contacts": [HF],
            "on": kokoro["local"] and not kokoro["ready"],
            "when": (
                "Once, at this server's first Kokoro narration: Kokoro's weights and voices "
                "are downloaded."
                if kokoro["local"] and not kokoro["ready"]
                else "Kokoro's weights and voices are downloaded; narrating needs no internet."
                if kokoro["local"]
                else "Kokoro does not run on this server."
            ),
        },
    ]
    for r in rows:
        r["blocked"] = r["on"] and offline
    return rows


def report(db: Session, settings: Settings) -> dict:
    m = models(settings)
    return {
        "on": egress.offline(),
        "forced": forced(settings),
        "features": features(db, settings, m),
        "models": m,
        "prepare": prepare_state(),
    }


# --- downloading ahead of time --------------------------------------------------------------------

Say = Callable[[str], None]


def _download_whisper(settings: Settings, model: str) -> str:
    from faster_whisper.utils import download_model

    path = download_model(model, cache_dir=str(models_dir(settings)))
    return f"ready ({path})"


def _download_kokoro() -> str:
    from huggingface_hub import hf_hub_download

    from lectern.tts import engines as eng

    files = _kokoro_files()
    for name in files:
        hf_hub_download(eng.KOKORO_REPO, name)
    return f"{len(files)} files ready"


def piper_voice_urls(voice: str) -> list[str]:
    m = _PIPER_VOICE.match(voice)
    if not m:
        raise ValueError(f"not a Piper voice name like {PIPER_DEFAULT_VOICE}: {voice!r}")
    parts = {**m.groupdict(), "family": m["code"].split("_", 1)[0], "voice": voice}
    return [PIPER_VOICE_URL.format(**parts, ext=ext) for ext in (".onnx", ".onnx.json")]


def _fetch(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Lectern (prepare-models)"})  # noqa: S310 - fixed https origin
    with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as out:  # noqa: S310
        total = 0
        while block := resp.read(1024 * 1024):
            total += len(block)
            if total > PIPER_MAX_BYTES:
                raise ValueError("the download is larger than any Piper voice")
            out.write(block)


def _download_piper(
    settings: Settings, voice: str, fetch: Callable[[str, Path], None] = _fetch
) -> str:
    from lectern.tts import engines as eng

    target = eng.piper_voices_dir(settings)
    target.mkdir(parents=True, exist_ok=True)
    names = [f"{voice}.onnx", f"{voice}.onnx.json"]
    if all((target / n).is_file() for n in names):
        return f"already in {target}"
    parts = []
    try:
        for url, name in zip(piper_voice_urls(voice), names, strict=True):
            part = target / f".{name}.part"
            parts.append(part)
            fetch(url, part)
        # the .json last: piper_voices() lists a voice once its .json is there
        for part, name in zip(parts, names, strict=True):
            part.replace(target / name)
    finally:
        for part in parts:
            part.unlink(missing_ok=True)
    return f"saved in {target}"


def prepare(
    settings: Settings,
    *,
    align_models: list[str] | None = None,
    piper_voices: list[str] | None = None,
    kokoro: bool | None = None,
    say: Say = print,
) -> bool:
    """Download what this server needs to align and narrate without the internet. Defaults:
    the configured alignment model when the server aligns, Kokoro when it narrates with Kokoro
    itself, and one Piper voice when Piper is installed without any. Returns success."""
    from lectern.align.transcribe import transcription_available
    from lectern.tts import engines as eng

    if egress.offline():
        say("offline mode is on: nothing can be downloaded (see docs/offline-mode.md)")
        return False
    ok = True

    def step(label: str, fn: Callable[[], str]) -> None:
        nonlocal ok
        say(f"{label}: downloading")
        try:
            say(f"{label}: {fn()}")
        except Exception as exc:  # noqa: BLE001 - reported, and the next download still runs
            ok = False
            say(f"{label}: failed: {exc}"[:500])

    if transcription_available():
        for m in align_models or [settings.align_model]:
            step(f"alignment model {m}", lambda m=m: _download_whisper(settings, m))
    elif align_models:
        ok = False
        say("alignment: faster-whisper is not installed here (build with LECTERN_EXTRAS=align)")
    else:
        say("alignment: not installed on this server, skipped")
    if kokoro is None:
        kokoro = settings.tts_kokoro_local and eng.kokoro_installed()
    if kokoro:
        step("Kokoro weights and voices", _download_kokoro)
    voices = piper_voices or (
        [PIPER_DEFAULT_VOICE] if eng.piper_installed() and not eng.piper_voices(settings) else []
    )
    for v in voices:
        step(f"Piper voice {v}", lambda v=v: _download_piper(settings, v))
    say("done" if ok else "finished with errors")
    return ok


# The Admin button runs `lectern prepare-models` in a child process (faster-whisper and PyTorch
# stay out of the server's memory) and keeps its last lines here.
_prep_lock = threading.Lock()
_prep: dict = {"running": False, "ok": None, "started_at": None, "finished_at": None, "log": []}


def prepare_state() -> dict:
    with _prep_lock:
        return {**_prep, "log": list(_prep["log"])}


def _iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def start_prepare(command: list[str] | None = None) -> bool:
    """Start downloading in the background; False if a download is already running."""
    with _prep_lock:
        if _prep["running"]:
            return False
        _prep.update(running=True, ok=None, started_at=_iso(), finished_at=None, log=[])
    argv = command or [sys.executable, "-m", "lectern.cli", "prepare-models"]

    def run() -> None:
        ok = False
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv: this interpreter and our module
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )  # fmt: skip
            assert proc.stdout is not None
            for line in proc.stdout:
                with _prep_lock:
                    _prep["log"] = [*_prep["log"], line.rstrip()[:500]][-30:]
            ok = proc.wait(timeout=PREPARE_TIMEOUT_S) == 0
        except Exception as exc:  # noqa: BLE001
            with _prep_lock:
                _prep["log"] = [*_prep["log"], f"could not run the download: {exc}"][-30:]
        finally:
            with _prep_lock:
                _prep.update(running=False, ok=ok, finished_at=_iso())

    threading.Thread(target=run, name="lectern-prepare-models", daemon=True).start()
    return True


# --- `lectern prepare-models` -------------------------------------------------------------------------


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--align-model",
        action="append",
        default=None,
        help="faster-whisper model to download (repeatable; default: LECTERN_ALIGN_MODEL)",
    )
    p.add_argument(
        "--piper-voice",
        action="append",
        default=None,
        help=f"Piper voice to download, e.g. {PIPER_DEFAULT_VOICE} (repeatable; default: that "
        "one when Piper is installed without a voice)",
    )
    p.add_argument(
        "--kokoro",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="download Kokoro's weights and voices (default: when LECTERN_TTS_KOKORO_LOCAL=1)",
    )


def run_cli(args: argparse.Namespace) -> None:
    from sqlalchemy.exc import OperationalError

    from lectern.config import get_settings
    from lectern.db import init_engine, session_factory

    settings = get_settings()
    on = settings.offline
    if not on:
        init_engine(settings)
        with session_factory()() as db:
            try:
                on = instance.get(db, KEY)
            except OperationalError:  # no database yet: nothing was turned on in Admin
                on = False
    if on:
        sys.exit(
            "offline mode is on, so nothing can be downloaded. Turn it off in Admin → Offline "
            "mode (or, when LECTERN_OFFLINE=1 sets it, run this one command with "
            "LECTERN_OFFLINE=0), download, then turn it back on."
        )
    ok = prepare(
        settings,
        align_models=args.align_model,
        piper_voices=args.piper_voice,
        kokoro=args.kokoro,
        say=lambda m: print(m, flush=True),
    )
    sys.exit(0 if ok else 1)
