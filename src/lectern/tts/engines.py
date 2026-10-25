"""Local speech engines: Piper (CPU, fits a Raspberry Pi) and Kokoro (better voices, wants a
faster machine). ElevenLabs is a remote service and lives in :mod:`lectern.tts.elevenlabs`.

Each engine synthesises **one sentence at a time** and returns 16-bit mono PCM. Sentence
timings are therefore exact: a sentence starts where the samples before it end. Nothing here
guesses a timing or aligns audio after the fact.

Piper voices are ``<voice>.onnx`` + ``<voice>.onnx.json`` files in ``LECTERN_PIPER_VOICES_DIR``
(default ``<data>/tts/piper``); ``lectern tts-voices`` lists them (see docs/tts.md for where to
get them). Kokoro runs in the server only when ``kokoro`` is installed (``lectern[kokoro]``)
and ``LECTERN_TTS_KOKORO_LOCAL=1``; otherwise Kokoro jobs wait for a worker that advertises
``tts-kokoro`` (the Mac worker).
"""

from __future__ import annotations

import importlib.util
import json
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from lectern.config import Settings

PIPER = "piper"
KOKORO = "kokoro"
ELEVENLABS = "elevenlabs"
ENGINES = (KOKORO, PIPER, ELEVENLABS)
KOKORO_MODEL = "kokoro-82m"
# Where KPipeline gets its weights and voices (Hugging Face); `lectern worker-doctor --prepare`
# downloads them ahead of the first job.
KOKORO_REPO = "hexgrad/Kokoro-82M"
KOKORO_FILES = ("config.json", "kokoro-v1_0.pth")
# Kokoro's English G2P (misaki) needs this spaCy pipeline, pinned in pyproject.toml (the
# `mac` and `kokoro` extras). Keep the URL in step with it.
SPACY_MODEL = "en_core_web_sm"
SPACY_MODEL_URL = (
    "https://github.com/explosion/spacy-models/releases/download/"
    "en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
)
KOKORO_SAMPLE_RATE = 24000
SPEED_MIN, SPEED_MAX = 0.5, 2.0

# Kokoro v1.0 voices: the first letter is the language, the second the voice's gender.
KOKORO_LANGS = {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi", "i": "it",
                "j": "ja", "p": "pt-br", "z": "zh"}  # fmt: skip
KOKORO_VOICES = (
    "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky", "am_adam", "am_michael",
    "am_fenrir", "bf_emma", "bf_isabella", "bm_george", "bm_lewis", "bm_fable", "ef_dora",
    "em_alex", "ff_siwis", "hf_alpha", "hm_omega", "if_sara", "im_nicola", "jf_alpha",
    "jm_kumo", "pf_dora", "pm_alex", "zf_xiaobei", "zm_yunjian",
)  # fmt: skip


class EngineUnavailable(RuntimeError):
    """The engine or voice is not installed here (not retryable on this machine)."""


class KokoroSetupMissing(EngineUnavailable):
    """Kokoro is installed without spaCy's English model (an install from before it was a
    dependency)."""


def forbid_runtime_installs() -> None:
    """Make a missing spaCy model an ordinary error instead of a run-time install.

    misaki (Kokoro's English G2P) calls ``spacy.cli.download`` when ``en_core_web_sm`` is
    missing. That shells out to ``pip`` or ``uv pip install``; in a uv tool environment or a
    container neither works ("No virtual environment found"), and spaCy then calls
    ``sys.exit()``, which used to take the whole worker down mid-job. The model is a pinned
    dependency now; this turns an install that predates it into a clear, catchable error."""
    try:
        import spacy.cli
    except ImportError:
        return

    def refuse(name: str = SPACY_MODEL, *_a, **_k) -> None:
        raise KokoroSetupMissing(
            f"spaCy model {name} is not installed, and Lectern does not install packages while "
            "a job runs. Run `lectern worker-doctor --prepare` (or reinstall with the kokoro / "
            "mac extra)."
        )

    spacy.cli.download = refuse


class Synth(Protocol):
    sample_rate: int

    def sentence(self, text: str) -> bytes:
        """16-bit little-endian mono PCM for one sentence, without trailing silence."""


@dataclass(frozen=True)
class PiperVoiceInfo:
    id: str
    path: Path
    language: str | None
    sample_rate: int
    quality: str | None

    def json(self) -> dict:
        return {"id": self.id, "name": self.id, "language": self.language,
                "sample_rate": self.sample_rate, "quality": self.quality}  # fmt: skip


def piper_voices_dir(settings: Settings) -> Path:
    return settings.piper_voices_dir or settings.data_dir / "tts" / "piper"


def piper_voices(settings: Settings) -> list[PiperVoiceInfo]:
    d = piper_voices_dir(settings)
    out = []
    for onnx in sorted(d.glob("*.onnx")) if d.is_dir() else []:
        cfg = onnx.with_name(onnx.name + ".json")
        try:
            meta = json.loads(cfg.read_text())
        except (OSError, ValueError):
            continue
        lang = (meta.get("language") or {}).get("code") or (meta.get("espeak") or {}).get("voice")
        out.append(
            PiperVoiceInfo(
                onnx.stem,
                onnx,
                lang,
                int((meta.get("audio") or {}).get("sample_rate") or 22050),
                (meta.get("audio") or {}).get("quality"),
            )
        )
    return out


def piper_installed() -> bool:
    return importlib.util.find_spec("piper") is not None


def kokoro_installed() -> bool:
    return importlib.util.find_spec("kokoro") is not None


def kokoro_lang(voice: str) -> str:
    return KOKORO_LANGS.get(voice[:1], "en-us")


class PiperSynth:
    """Piper (``piper-tts``, ONNX on the CPU): one voice loaded once per job."""

    def __init__(self, voice: PiperVoiceInfo, speed: float = 1.0) -> None:
        if not piper_installed():
            raise EngineUnavailable("Piper is not installed on this server (lectern[tts])")
        from piper import PiperVoice

        self._voice = PiperVoice.load(voice.path)
        self.sample_rate = int(getattr(self._voice.config, "sample_rate", voice.sample_rate))
        self._length_scale = 1.0 / max(SPEED_MIN, min(SPEED_MAX, speed))

    def sentence(self, text: str) -> bytes:
        try:  # piper-tts >= 1.3
            from piper import SynthesisConfig

            cfg = SynthesisConfig(length_scale=self._length_scale)
            return b"".join(c.audio_int16_bytes for c in self._voice.synthesize(text, cfg))
        except ImportError:  # piper-tts 1.2
            return b"".join(
                self._voice.synthesize_stream_raw(text, length_scale=self._length_scale)
            )


class KokoroSynth:
    """Kokoro-82M through the ``kokoro`` package (PyTorch; CPU works, a GPU is much faster)."""

    sample_rate = KOKORO_SAMPLE_RATE

    def __init__(self, voice: str, speed: float = 1.0) -> None:
        if not kokoro_installed():
            raise EngineUnavailable("Kokoro is not installed on this server (lectern[kokoro])")
        from kokoro import KPipeline

        forbid_runtime_installs()
        self._pipeline = KPipeline(lang_code=voice[:1] or "a", repo_id=KOKORO_REPO)
        self._voice = voice
        self._speed = max(SPEED_MIN, min(SPEED_MAX, speed))

    def sentence(self, text: str) -> bytes:
        out = array("h")
        for result in self._pipeline(text, voice=self._voice, speed=self._speed):
            audio = getattr(result, "audio", None)
            if audio is None and isinstance(result, tuple):
                audio = result[-1]
            if audio is None:
                continue
            samples = audio.tolist() if hasattr(audio, "tolist") else list(audio)
            out.extend(int(max(-1.0, min(1.0, float(x))) * 32767) for x in samples)
        return out.tobytes()


def silence(seconds: float, sample_rate: int) -> bytes:
    return b"\x00\x00" * max(0, round(seconds * sample_rate))
