"""Speech to timed words with faster-whisper (optional dependency, ``lectern[align]``).

Default model ``small`` with ``int8`` compute on CPU: on two x86 vCPUs it runs at 2-3x
realtime with ~1.4 GB of RSS (docs/alignment.md, "Benchmark"). Every knob is a
setting (``LECTERN_ALIGN_MODEL``, ``LECTERN_ALIGN_DEVICE``, ``LECTERN_ALIGN_COMPUTE_TYPE``,
``LECTERN_ALIGN_THREADS``) and a job may ask for a different model (``POST
/api/works/{id}/align {"model": "medium"}``), which is how a low-coverage result is retried.

Several tracks are transcribed one after the other and their words placed on the work
timeline by adding the track's ``offset_s``.
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("lectern.align")

DEFAULT_MODEL = "small"
MODELS = ("tiny", "base", "small", "medium", "large-v3", "large-v3-turbo", "distil-large-v3")
# Rough CPU cost relative to ``small``; used to order the "larger model" suggestion.
MODEL_ORDER = {m: i for i, m in enumerate(MODELS)}


@dataclass(frozen=True)
class Word:
    text: str
    start: float  # seconds on the work timeline
    end: float
    probability: float = 1.0


@dataclass(frozen=True)
class TrackAudio:
    path: Path
    offset_s: float
    duration_s: float


class TranscriptionUnavailable(RuntimeError):
    """faster-whisper is not installed in this environment."""


def transcription_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def larger_model(model: str) -> str | None:
    """The next bigger model to suggest after a low-coverage run, or ``None`` at the top."""
    i = MODEL_ORDER.get(model)
    if i is None:
        return "medium" if model != "medium" else "large-v3"
    for candidate in MODELS[i + 1 :]:
        if candidate in ("medium", "large-v3"):  # turbo and distil are faster, not better
            return candidate
    return None


class Transcriber:
    """Thin wrapper around ``faster_whisper.WhisperModel`` with lazy loading."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        device: str = "cpu",
        compute_type: str = "int8",
        threads: int = 0,
        download_root: Path | None = None,
        beam_size: int = 5,
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.threads = threads
        self.download_root = download_root
        self.beam_size = beam_size
        self._model = None

    @property
    def label(self) -> str:
        return f"faster-whisper-{self.model_name}-{self.compute_type}"

    def load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:  # pragma: no cover - exercised only without the extra
                raise TranscriptionUnavailable(
                    "faster-whisper is not installed; install lectern[align] or run an "
                    "external worker (lectern worker --server ...)"
                ) from exc
            kwargs = {"device": self.device, "compute_type": self.compute_type}
            if self.threads:
                kwargs["cpu_threads"] = self.threads
            if self.download_root:
                kwargs["download_root"] = str(self.download_root)
            log.info(
                "loading whisper model %s (%s/%s)", self.model_name, self.device, self.compute_type
            )
            self._model = WhisperModel(self.model_name, **kwargs)
        return self._model

    def transcribe(
        self,
        track: TrackAudio,
        language: str | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> tuple[list[Word], str | None]:
        """Timed words of one track on the work timeline, and the detected language."""
        model = self.load()
        segments, info = model.transcribe(
            str(track.path),
            language=language,
            beam_size=self.beam_size,
            word_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        words: list[Word] = []
        duration = track.duration_s or getattr(info, "duration", 0.0) or 0.0
        last = 0.0
        for seg in segments:
            for w in seg.words or ():
                start = max(float(w.start), last)
                end = max(float(w.end), start)
                last = start
                words.append(
                    Word(w.word, track.offset_s + start, track.offset_s + end, float(w.probability))
                )
            if on_progress and duration:
                on_progress(min(1.0, float(seg.end) / duration))
        return words, getattr(info, "language", None)


def transcribe_tracks(
    transcriber: Transcriber,
    tracks: Iterable[TrackAudio],
    language: str | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> tuple[list[Word], str | None]:
    """All tracks, in order, on one timeline. Progress is over the total duration."""
    tracks = list(tracks)
    total = sum(t.duration_s for t in tracks) or 1.0
    words: list[Word] = []
    detected: str | None = None
    for track in tracks:
        base = track.offset_s / total

        def _progress(frac: float, base: float = base, track: TrackAudio = track) -> None:
            if on_progress:
                on_progress(min(1.0, base + frac * track.duration_s / total))

        track_words, lang = transcriber.transcribe(track, language, _progress)
        detected = detected or lang
        words.extend(track_words)
    return words, detected
