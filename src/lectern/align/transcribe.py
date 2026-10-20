"""Speech to timed words with faster-whisper (optional dependency, ``lectern[align]``).

Default model ``small`` with ``int8`` compute on CPU: on two x86 vCPUs it runs at 2-3x
realtime with ~1.4 GB of RSS (docs/alignment.md, "Benchmark"). Every knob is a
setting (``LECTERN_ALIGN_MODEL``, ``LECTERN_ALIGN_DEVICE``, ``LECTERN_ALIGN_COMPUTE_TYPE``,
``LECTERN_ALIGN_THREADS``) and a job may ask for a different model (``POST
/api/works/{id}/align {"model": "medium"}``), which is how a low-coverage result is retried.

Bounded memory
    Handing faster-whisper a file path makes it decode the *whole* file into memory (int16
    buffer, then a float32 copy, then a VAD-filtered copy, then mel features of all of it),
    about 6-12 bytes per 16 kHz sample: gigabytes for a single-file audiobook. Instead each
    track is decoded here, as a stream (PyAV, the decoder faster-whisper itself uses), and cut
    into windows of ``window_s`` seconds that overlap by ``overlap_s``. Each window is
    transcribed on its own as a NumPy array, with the same VAD and word-timestamp options, and
    its words are shifted onto the work timeline. Peak memory is then the model plus one
    window, independent of the track's length.

    Consecutive windows both transcribe their overlap; :func:`merge_window_words` keeps one
    copy. The seam is placed at the longest pause the earlier window heard inside the middle
    of the overlap (or the overlap's midpoint when it heard nothing there): earlier-window
    words that start before the seam are kept, later-window words that start at or after it.
    Words the later window heard after the seam that pair, in order, with words kept from the
    earlier window (the same word timed on the other side of the seam) are dropped; a word
    said twice pairs twice, so it stays twice, and a different word never pairs. Words
    near a window's edge, where audio is cut mid-phrase, are never used. The result depends
    only on the two windows' words, so it is deterministic.

Several tracks are transcribed one after the other and their words placed on the work
timeline by adding the track's ``offset_s``.
"""

from __future__ import annotations

import gc
import importlib.util
import itertools
import logging
import math
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lectern.align.tokens import tokenize

if TYPE_CHECKING:  # numpy comes with faster-whisper (``lectern[align]``), not the server
    import numpy as np

log = logging.getLogger("lectern.align")

DEFAULT_MODEL = "small"
MODELS = ("tiny", "base", "small", "medium", "large-v3", "large-v3-turbo", "distil-large-v3")
# Rough CPU cost relative to ``small``; used to order the "larger model" suggestion.
MODEL_ORDER = {m: i for i, m in enumerate(MODELS)}

SAMPLING_RATE = 16_000  # what Whisper models take
# faster-whisper needs ~70 bytes of transient memory per 16 kHz sample of what it is given
# (float32 audio, VAD, and the STFT and mel features of all of it): ~1.1 MB per second.
# Five-minute windows add ~350 MB to the model, whatever the length of the book
# (docs/alignment.md, "Long recordings"); the overlap costs 5 % more transcription.
DEFAULT_WINDOW_S = 300.0
DEFAULT_OVERLAP_S = 15.0
MIN_WINDOW_S = 10.0
# Two words this close (start times) with the same normalised text are one word heard twice.
DUPLICATE_WORD_S = 1.0
# How far before the seam the two windows' words are aligned to find repeated words.
SEAM_CONTEXT_S = 5.0


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


def check_window(window_s: float, overlap_s: float) -> tuple[float, float]:
    """Validated ``(window_s, overlap_s)``: the overlap must leave each window a middle that
    no other window covers, so every instant has one owner."""
    window_s, overlap_s = float(window_s), float(overlap_s)
    if window_s < MIN_WINDOW_S:
        raise ValueError(f"window must be at least {MIN_WINDOW_S:g} s")
    if overlap_s < 0 or overlap_s > window_s / 3:
        raise ValueError("overlap must be between 0 and a third of the window")
    return window_s, overlap_s


# --- streaming decode ------------------------------------------------------------------------


def iter_pcm(path: Path, sampling_rate: int = SAMPLING_RATE) -> Iterator[np.ndarray]:
    """Mono int16 PCM at ``sampling_rate``, a few seconds per block, decoded as a stream.

    The same PyAV steps as ``faster_whisper.audio.decode_audio`` (corrupt frames skipped,
    timestamps ignored, s16 mono resampling), without collecting the whole file.
    """
    import av

    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=sampling_rate)
    fifo = av.audio.fifo.AudioFifo()
    group = sampling_rate * 4  # resample ~4 s at a time (decoder rate)
    try:
        with av.open(str(path), mode="r", metadata_errors="ignore") as container:
            frames = container.decode(audio=0)
            while True:
                try:
                    frame = next(frames)
                except StopIteration:
                    break
                except av.error.InvalidDataError:
                    continue
                frame.pts = None
                fifo.write(frame)
                if fifo.samples >= group:
                    for out in resampler.resample(fifo.read()):
                        yield out.to_ndarray().reshape(-1)
            if fifo.samples:
                for out in resampler.resample(fifo.read()):
                    yield out.to_ndarray().reshape(-1)
            for out in resampler.resample(None):
                yield out.to_ndarray().reshape(-1)
    finally:
        # PyAV keeps resampler buffers alive until a collection (faster-whisper issue #390).
        del resampler, fifo
        gc.collect()


def iter_windows(
    blocks: Iterable[np.ndarray],
    window_s: float,
    overlap_s: float,
    sampling_rate: int = SAMPLING_RATE,
) -> Iterator[tuple[float, np.ndarray]]:
    """``(start_s, float32 samples)`` windows of ``window_s`` seconds, each starting
    ``window_s - overlap_s`` after the previous one; the last one may be shorter. At most one
    window (plus one decoded block) is held at a time."""
    import numpy as np

    size = int(round(window_s * sampling_rate))
    step = size - int(round(overlap_s * sampling_rate))
    buf = np.empty(size, dtype=np.int16)
    filled = 0
    start = 0  # sample index of buf[0] in the track
    emitted = False
    for block in blocks:
        pos = 0
        while pos < len(block):
            take = min(size - filled, len(block) - pos)
            buf[filled : filled + take] = block[pos : pos + take]
            filled += take
            pos += take
            if filled == size:
                yield start / sampling_rate, buf.astype(np.float32) / 32768.0
                emitted = True
                buf[: size - step] = buf[step:size]
                filled = size - step
                start += step
    # The tail: unless it is only the overlap already sent in full with the previous window.
    if filled and not (emitted and filled <= size - step):
        yield start / sampling_rate, buf[:filled].astype(np.float32) / 32768.0


# --- merging overlapping windows ---------------------------------------------------------------


def _norm(text: str) -> tuple[str, ...]:
    return tuple(t.norm for t in tokenize(text))


def seam(earlier: Sequence[Word], lo: float, hi: float) -> float:
    """Where to switch from the earlier window's words to the later one's inside the overlap
    ``[lo, hi)``: the middle of the longest pause the earlier window heard within the central
    half of the overlap, else the overlap's midpoint. Ties go to the earliest pause."""
    mid = (lo + hi) / 2
    q = (hi - lo) / 4
    inner_lo, inner_hi = lo + q, hi - q
    best: tuple[float, float] | None = None  # (gap, cut)
    inside = [w for w in earlier if w.end > inner_lo and w.start < inner_hi]
    if not inside:
        return mid
    # Pauses: between the inner edge and the first word, between words, after the last one.
    edges = [(inner_lo, inside[0].start)]
    edges += [(a.end, b.start) for a, b in itertools.pairwise(inside)]
    edges.append((inside[-1].end, inner_hi))
    for a, b in edges:
        a, b = max(a, inner_lo), min(b, inner_hi)
        gap = b - a
        if gap > 0 and (best is None or gap > best[0]):
            best = (gap, (a + b) / 2)
    return best[1] if best is not None else mid


def merge_window_words(
    kept: list[Word], earlier_tail: list[Word], later: list[Word], lo: float, hi: float
) -> tuple[list[Word], list[Word]]:
    """Join two overlapping windows ``[.., hi)`` and ``[lo, ..)`` (``lo < hi``, absolute
    times). ``earlier_tail`` is the earlier window's words that start at or after ``lo``
    (not yet final); ``kept`` receives the final words. Returns ``(kept, later_words)`` with
    the later window's words from the seam on, which become the next ``earlier`` window."""
    cut = seam(earlier_tail, lo, hi)
    kept.extend(w for w in earlier_tail if w.start < cut)
    dropped = [w for w in earlier_tail if w.start >= cut]
    first = next((i for i, w in enumerate(later) if w.start >= cut), len(later))
    # Where the later window has its own copy of the first word the earlier one drops, the
    # later window's words from there on are certainly new.
    anchor = _closest(later, dropped[0]) if dropped else None
    if anchor is not None and anchor <= first:
        return kept, later[anchor:]  # the later window timed that word before the cut
    stop = len(later) if anchor is None else anchor
    # Later-window words between the cut and the anchor may repeat words kept from the
    # earlier window (timed on the other side of the cut). Align the two windows' words near
    # the cut in order and drop exactly those that pair with a kept word: a word said twice
    # ("no, no") pairs twice, so both copies stay; a different word never pairs.
    since = max(lo, cut - SEAM_CONTEXT_S)
    k = len(kept)
    while k > 0 and kept[k - 1].start >= since:
        k -= 1
    j0 = next((i for i, w in enumerate(later) if w.start >= since - DUPLICATE_WORD_S), stop)
    j0 = min(j0, first)
    paired = _paired(kept[k:], later[j0:stop])
    head = [w for i, w in enumerate(later[first:stop], first) if i - j0 not in paired]
    return kept, head + later[stop:]


def _same(a: Word, b: Word) -> bool:
    return abs(a.start - b.start) < DUPLICATE_WORD_S and _norm(a.text) == _norm(b.text)


def _paired(a: Sequence[Word], b: Sequence[Word]) -> set[int]:
    """Indexes into ``b`` of an order-preserving pairing of ``a`` and ``b`` (same text, starts
    within ``DUPLICATE_WORD_S``) with the most pairs, then the least total time difference."""
    n, m = len(a), len(b)
    best = [[(0, 0.0)] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            s = max(best[i + 1][j], best[i][j + 1])
            if _same(a[i], b[j]):
                hit = best[i + 1][j + 1]
                s = max(s, (hit[0] + 1, hit[1] - abs(a[i].start - b[j].start)))
            best[i][j] = s
    out: set[int] = set()
    i = j = 0
    while i < n and j < m:
        hit = best[i + 1][j + 1]
        if _same(a[i], b[j]) and best[i][j] == (
            hit[0] + 1,
            hit[1] - abs(a[i].start - b[j].start),
        ):
            out.add(j)
            i, j = i + 1, j + 1
        elif best[i][j] == best[i + 1][j]:
            i += 1
        else:
            j += 1
    return out


def _closest(words: Sequence[Word], word: Word) -> int | None:
    """Index of the word in ``words`` with ``word``'s text starting closest to it (within
    ``DUPLICATE_WORD_S``; the first on a tie), or ``None``."""
    best: tuple[float, int] | None = None
    for i, w in enumerate(words):
        if _same(w, word) and (best is None or abs(w.start - word.start) < best[0]):
            best = (abs(w.start - word.start), i)
    return best[1] if best is not None else None


# --- the model --------------------------------------------------------------------------------


class Transcriber:
    """Thin wrapper around ``faster_whisper.WhisperModel`` with lazy loading and windowed,
    streaming transcription (see the module docstring)."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        device: str = "cpu",
        compute_type: str = "int8",
        threads: int = 0,
        download_root: Path | None = None,
        beam_size: int = 5,
        window_s: float = DEFAULT_WINDOW_S,
        overlap_s: float = DEFAULT_OVERLAP_S,
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.threads = threads
        self.download_root = download_root
        self.beam_size = beam_size
        self.window_s, self.overlap_s = check_window(window_s, overlap_s)
        self._model = None
        # Decoder hook: tests feed synthetic PCM without audio files.
        self.decode: Callable[[Path], Iterable[np.ndarray]] = iter_pcm

    @property
    def label(self) -> str:
        return f"faster-whisper-{self.model_name}-{self.compute_type}"

    def with_model(self, model: str) -> Transcriber:
        """The same settings with another model (a job may ask for one)."""
        if model == self.model_name:
            return self
        other = Transcriber(
            model,
            device=self.device,
            compute_type=self.compute_type,
            threads=self.threads,
            download_root=self.download_root,
            beam_size=self.beam_size,
            window_s=self.window_s,
            overlap_s=self.overlap_s,
        )
        other.decode = self.decode
        return other

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

    def _window_words(
        self, samples: np.ndarray, base_s: float, language: str | None, on_segment
    ) -> tuple[list[Word], str | None]:
        """Words of one window at absolute times ``base_s + t``, clamped non-decreasing."""
        model = self.load()
        segments, info = model.transcribe(
            samples,
            language=language,
            beam_size=self.beam_size,
            word_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        words: list[Word] = []
        last = 0.0
        for seg in segments:
            for w in seg.words or ():
                start, end, p = float(w.start), float(w.end), float(w.probability)
                if not math.isfinite(start):
                    continue  # a word with no time cannot be placed (and would poison max())
                start = max(start, last)
                end = max(end, start) if math.isfinite(end) else start
                last = start
                p = p if math.isfinite(p) else 0.0
                words.append(Word(w.word, base_s + start, base_s + end, p))
            on_segment(float(seg.end))
        return words, getattr(info, "language", None)

    def transcribe(
        self,
        track: TrackAudio,
        language: str | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> tuple[list[Word], str | None]:
        """Timed words of one track on the work timeline, and the detected language."""
        duration = track.duration_s
        kept: list[Word] = []
        pending: list[Word] = []  # the previous window's words not yet final
        prev_end: float | None = None  # end of the previous window, track-relative
        detected: str | None = None
        for start, samples in iter_windows(self.decode(track.path), self.window_s, self.overlap_s):
            end = start + len(samples) / SAMPLING_RATE

            def _seg(t: float, start: float = start) -> None:
                if on_progress and duration:
                    on_progress(min(1.0, (start + t) / duration))

            # Language is fixed after the first window that heard speech, as a whole-file
            # run would detect it once from the first speech.
            words, lang = self._window_words(
                samples, track.offset_s + start, language or detected, _seg
            )
            del samples
            if words and detected is None and language is None:
                detected = lang
            if prev_end is None:
                pending = words
            else:
                lo, hi = track.offset_s + start, track.offset_s + prev_end
                final = [w for w in pending if w.start < lo]
                kept.extend(final)
                tail = pending[len(final) :]
                kept, pending = merge_window_words(kept, tail, words, lo, hi)
            prev_end = end
        kept.extend(pending)
        _clamp(kept)
        if on_progress:
            on_progress(1.0)
        return kept, detected


def _clamp(words: list[Word]) -> None:
    """Start times non-decreasing across window seams, as within one window."""
    last = 0.0
    for i, w in enumerate(words):
        if w.start < last:
            words[i] = Word(w.text, last, max(w.end, last), w.probability)
        last = words[i].start


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

        track_words, lang = transcriber.transcribe(track, language or detected, _progress)
        detected = detected or lang
        words.extend(track_words)
    return words, detected
