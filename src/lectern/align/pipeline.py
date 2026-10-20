"""The whole alignment of one work: EPUB + tracks in, sentence map + statistics out.

``align_work`` is what both the in-process worker and ``lectern worker`` run. Transcription
is injected (``transcribe``) so the matcher can be exercised with a recorded transcript in
tests and benchmarks without loading a model.
"""

from __future__ import annotations

import resource
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lectern.align.matcher import AudioToken, MatchResult, TextToken, match
from lectern.align.text import SentenceSpan, iter_spine_items, segment_items
from lectern.align.tokens import tokenize
from lectern.align.transcribe import TrackAudio, Word
from lectern.sentence_map import SentenceEntry, SentenceMap, TrackRef, audio_fingerprint

ProgressFn = Callable[[float, str], None]
TranscribeFn = Callable[
    [Sequence[TrackAudio], str | None, Callable[[float], None]], tuple[list[Word], str | None]
]


@dataclass(frozen=True)
class TrackInput:
    position: int
    path: Path
    sha256: str
    duration_s: float
    offset_s: float


@dataclass
class AlignOutput:
    sentence_map: SentenceMap
    stats: dict
    sentences: list[SentenceSpan]
    result: MatchResult


def align_work(
    *,
    work_id: int,
    epub_path: Path,
    epub_sha256: str,
    tracks: Sequence[TrackInput],
    model_label: str,
    transcribe: TranscribeFn,
    language: str | None = None,
    on_progress: ProgressFn | None = None,
) -> AlignOutput:
    started = time.monotonic()
    progress = on_progress or (lambda _f, _m: None)

    progress(0.0, "reading text")
    sentences = segment_items(iter_spine_items(epub_path))
    if not sentences:
        raise ValueError("the EPUB has no text to align")
    text_tokens: list[TextToken] = []
    for s in sentences:
        text_tokens += [TextToken(t.norm, s.n) for t in tokenize(s.text)]
    text_done = time.monotonic()

    def _transcribe_progress(frac: float) -> None:
        progress(0.02 + 0.9 * frac, "transcribing")

    audio = [TrackAudio(t.path, t.offset_s, t.duration_s) for t in tracks]
    words, detected = transcribe(audio, language, _transcribe_progress)
    transcribe_done = time.monotonic()

    progress(0.93, "matching")
    audio_tokens: list[AudioToken] = []
    for w in words:
        audio_tokens += [AudioToken(t.norm, w.start, w.end) for t in tokenize(w.text)]
    total_audio = sum(t.duration_s for t in tracks)
    result = match(text_tokens, audio_tokens, [len(s.text) for s in sentences], total_audio)
    match_done = time.monotonic()

    entries = [
        SentenceEntry(
            n=s.n,
            spine_index=s.spine_index,
            char_start=s.char_start,
            char_end=s.char_end,
            audio_start_s=_round(tm.start),
            audio_end_s=_round(tm.end),
            matched=tm.matched,
        )
        for s, tm in zip(sentences, result.timings, strict=True)
    ]
    sentence_map = SentenceMap(
        version=1,
        work_id=work_id,
        epub_sha256=epub_sha256,
        audio_fingerprint=audio_fingerprint(t.sha256 for t in tracks),
        model=model_label[:64],
        language=(language or detected or None),
        generated_at=datetime.now(UTC),
        tracks=[
            TrackRef(
                position=t.position, sha256=t.sha256, duration_s=t.duration_s, offset_s=t.offset_s
            )
            for t in tracks
        ],
        sentences=entries,
    )
    elapsed = time.monotonic() - started
    stats = dict(result.stats)
    stats.update(
        {
            "model": model_label,
            "language": sentence_map.language,
            "words": len(words),
            "text_s": round(text_done - started, 3),
            "transcribe_s": round(transcribe_done - text_done, 3),
            "match_s": round(match_done - transcribe_done, 3),
            "elapsed_s": round(elapsed, 3),
            # audio seconds processed per wall-clock second: the number the ETA is built from
            "speed": round(total_audio / elapsed, 4) if elapsed > 0 and total_audio > 0 else None,
            "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        }
    )
    progress(1.0, "done")
    return AlignOutput(sentence_map, stats, sentences, result)


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 3)
