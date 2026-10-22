"""Transcription on the Apple Silicon GPU with mlx-whisper, falling back to faster-whisper.

``lectern worker`` on a Mac with Apple Silicon (``pip install 'lectern[mac]'``) transcribes
with `mlx-whisper <https://github.com/ml-explore/mlx-examples/tree/main/whisper>`_, which runs
Whisper on the GPU through MLX. Everything around it is unchanged: the same streaming decode,
the same overlapping windows and seam merge (:mod:`lectern.align.transcribe`), the same matcher.

Differences from faster-whisper, and how they are handled:

- mlx-whisper has no Silero VAD. ``hallucination_silence_threshold`` (word timestamps on)
  skips silent stretches where Whisper would otherwise invent words; the matcher ignores
  words that match no text anyway.
- No beam search: decoding is greedy (with temperature fallback).
- Models are the ``mlx-community`` conversions on Hugging Face (``MLX_REPOS``), cached in the
  Hugging Face cache (``HF_HOME``).

Fallback: :func:`worker_transcriber` picks MLX when it is installed on Apple Silicon. If the
MLX model cannot be loaded (no Metal device, a broken install, the download is blocked), or
MLX fails during a job, the transcriber switches to faster-whisper on the CPU for the rest of
that job and logs why; the next job tries MLX again. The sentence map's ``model`` names the
engine that started the job. Only the conversions in ``MLX_REPOS`` are ever loaded.
"""

from __future__ import annotations

import importlib.util
import logging
import math
import platform
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from lectern.align.transcribe import (
    DEFAULT_MODEL,
    DEFAULT_OVERLAP_S,
    DEFAULT_WINDOW_S,
    Transcriber,
    TranscriptionUnavailable,
    Word,
    transcription_available,
)

if TYPE_CHECKING:
    import numpy as np

log = logging.getLogger("lectern.align.mlx")

MLX_REPOS = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "distil-large-v3": "mlx-community/distil-whisper-large-v3",
}
ENGINES = ("auto", "mlx", "cpu")
# Seconds of silence after which mlx-whisper drops words it hears there (it has no VAD).
HALLUCINATION_SILENCE_S = 2.0


def apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def mlx_available() -> bool:
    return apple_silicon() and importlib.util.find_spec("mlx_whisper") is not None


class MLXTranscriber(Transcriber):
    """:class:`Transcriber` with mlx-whisper doing each window. ``fallback`` (a CPU
    faster-whisper transcriber) takes over when MLX cannot load or fails."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        window_s: float = DEFAULT_WINDOW_S,
        overlap_s: float = DEFAULT_OVERLAP_S,
        fallback: Transcriber | None = None,
        repo: str | None = None,
    ) -> None:
        super().__init__(model, device="gpu", compute_type="mlx", window_s=window_s,
                         overlap_s=overlap_s)  # fmt: skip
        # only the known conversions: a job's model name never picks an arbitrary repo
        self.repo = repo or MLX_REPOS.get(model)
        self.fallback = fallback
        self.using_fallback = False
        self._mlx = None

    @property
    def label(self) -> str:
        if self.using_fallback and self.fallback is not None:
            return self.fallback.label
        return f"mlx-whisper-{self.model_name}"

    def with_model(self, model: str) -> MLXTranscriber:
        if model == self.model_name:
            return self
        other = MLXTranscriber(
            model,
            window_s=self.window_s,
            overlap_s=self.overlap_s,
            fallback=self.fallback.with_model(model) if self.fallback else None,
        )
        other.decode = self.decode
        return other

    def begin_job(self) -> None:
        """A new job tries MLX again: a fallback lasts for the job it happened in (a GPU
        hiccup or a blocked download need not cost every later book its speed)."""
        self.using_fallback = False

    def _switch(self, why: str) -> None:
        if self.fallback is None:
            raise TranscriptionUnavailable(f"mlx-whisper failed ({why}) and no CPU fallback")
        log.warning("mlx-whisper unavailable (%s); using faster-whisper on the CPU", why)
        self.using_fallback = True

    def load(self):
        if self.using_fallback:
            return self.fallback.load()
        if self._mlx is None:
            try:
                if self.repo is None:
                    raise ValueError(f"no MLX conversion of {self.model_name!r}")
                import mlx_whisper
                from mlx_whisper.load_models import load_model

                load_model(self.repo)  # downloads once; transcribe() reuses its cache
                self._mlx = mlx_whisper
                log.info("loaded mlx-whisper %s (%s)", self.model_name, self.repo)
            except Exception as exc:  # noqa: BLE001 - any failure means: use the CPU
                self._switch(f"{type(exc).__name__}: {exc}"[:300])
                return self.fallback.load()
        return self._mlx

    def _window_words(
        self, samples: np.ndarray, base_s: float, language: str | None, on_segment
    ) -> tuple[list[Word], str | None]:
        mlx = self.load()
        if self.using_fallback:
            return self.fallback._window_words(samples, base_s, language, on_segment)
        try:
            result = mlx.transcribe(
                samples,
                path_or_hf_repo=self.repo,
                language=language,
                word_timestamps=True,
                condition_on_previous_text=False,
                hallucination_silence_threshold=HALLUCINATION_SILENCE_S,
                verbose=None,
            )
        except Exception as exc:  # noqa: BLE001 - redo this window on the CPU
            self._switch(f"{type(exc).__name__}: {exc}"[:300])
            return self.fallback._window_words(samples, base_s, language, on_segment)
        return words_from_segments(result.get("segments") or [], base_s, on_segment), result.get(
            "language"
        )


def words_from_segments(
    segments: list[dict], base_s: float, on_segment: Callable[[float], None]
) -> list[Word]:
    """mlx-whisper's ``segments[].words[]`` as :class:`Word` at absolute times, starts clamped
    non-decreasing and words without a finite time dropped (as for faster-whisper)."""
    words: list[Word] = []
    last = 0.0
    for seg in segments:
        for w in seg.get("words") or ():
            start, end = float(w.get("start", math.nan)), float(w.get("end", math.nan))
            p = float(w.get("probability", 0.0))
            if not math.isfinite(start):
                continue
            start = max(start, last)
            end = max(end, start) if math.isfinite(end) else start
            last = start
            words.append(Word(str(w.get("word", "")), base_s + start, base_s + end,
                              p if math.isfinite(p) else 0.0))  # fmt: skip
        on_segment(float(seg.get("end") or 0.0))
    return words


def worker_transcriber(
    engine: str = "auto",
    model: str = DEFAULT_MODEL,
    *,
    threads: int = 0,
    download_root: Path | None = None,
    window_s: float = DEFAULT_WINDOW_S,
    overlap_s: float = DEFAULT_OVERLAP_S,
    device: str = "cpu",
    compute_type: str = "int8",
) -> Transcriber | None:
    """The transcriber a worker uses: MLX on Apple Silicon when installed (``auto``/``mlx``),
    with faster-whisper on the CPU behind it; faster-whisper alone otherwise. ``None`` when
    neither engine is installed (the worker then does not advertise ``align``)."""
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {', '.join(ENGINES)}")
    cpu = (
        Transcriber(
            model,
            device=device,
            compute_type=compute_type,
            threads=threads,
            download_root=download_root,
            window_s=window_s,
            overlap_s=overlap_s,
        )  # fmt: skip
        if transcription_available()
        else None
    )
    if engine != "cpu" and mlx_available():
        return MLXTranscriber(model, window_s=window_s, overlap_s=overlap_s, fallback=cpu)
    if engine == "mlx":
        log.warning("mlx-whisper needs Apple Silicon and lectern[mac]; using the CPU engine")
    return cpu
