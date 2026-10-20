"""The sentence map: the JSON artifact an alignment worker produces for one work.

It is stored at ``<data_dir>/align/<work_id>.json`` and is the source of truth from which the
``sentences`` and ``alignments`` rows are (re)built. This module is the schema; the server
validates every uploaded map with :func:`SentenceMap.model_validate_json` before accepting it
(see docs/contracts.md, "Worker protocol").

Coordinate systems
    text   (spine_index, char offset) into the normalised text of a spine item, see
           :func:`lectern.positions.normalize_spine_text`
    audio  seconds on the work timeline: tracks in ``position`` order laid end to end, so a
           time inside track *i* is ``track.offset_s + seconds into that file``
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SENTENCE_MAP_VERSION = 1
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
# Track offsets are derived from durations; allow for container rounding.
OFFSET_TOLERANCE_S = 0.05


def audio_fingerprint(track_sha256s: Iterable[str]) -> str:
    """Fingerprint of a work's audio: SHA-256 over the track digests in play order.

    Any change to the set, order, or content of the tracks changes the fingerprint, which is
    what makes an alignment (and any in-flight job result) stale.
    """
    return hashlib.sha256("\n".join(track_sha256s).encode()).hexdigest()


def map_path(data_dir: Path, work_id: int) -> Path:
    return data_dir / "align" / f"{work_id}.json"


class TrackRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: int = Field(ge=0)
    sha256: str = Field(pattern=_HEX64.pattern)
    duration_s: float = Field(ge=0)
    offset_s: float = Field(ge=0)


class SentenceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n: int = Field(ge=0)
    spine_index: int = Field(ge=0)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=1)  # exclusive
    audio_start_s: float | None = Field(default=None, ge=0)
    audio_end_s: float | None = Field(default=None, ge=0)
    matched: bool = False
    # Optional copy of the sentence text, for hand-checking and debugging; never authoritative.
    text: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def _spans(self) -> SentenceEntry:
        if self.char_end <= self.char_start:
            raise ValueError(f"sentence {self.n}: char_end must be > char_start")
        if (self.audio_start_s is None) != (self.audio_end_s is None):
            raise ValueError(f"sentence {self.n}: audio_start_s and audio_end_s go together")
        if self.audio_end_s is not None and self.audio_end_s < self.audio_start_s:  # type: ignore[operator]
            raise ValueError(f"sentence {self.n}: audio_end_s must be >= audio_start_s")
        if self.matched and self.audio_start_s is None:
            raise ValueError(f"sentence {self.n}: matched sentences need audio times")
        if self.text is not None and len(self.text) != self.char_end - self.char_start:
            raise ValueError(f"sentence {self.n}: text length does not match the char span")
        return self


class SentenceMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    work_id: int = Field(ge=1)
    epub_sha256: str = Field(pattern=_HEX64.pattern)
    audio_fingerprint: str = Field(pattern=_HEX64.pattern)
    model: str = Field(min_length=1, max_length=64)
    language: str | None = Field(default=None, max_length=16)
    generated_at: datetime
    tracks: list[TrackRef] = Field(min_length=1)
    sentences: list[SentenceEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> SentenceMap:
        expected_offset = 0.0
        for i, t in enumerate(self.tracks):
            if t.position != i:
                raise ValueError("track positions must be 0..n-1 in order")
            if abs(t.offset_s - expected_offset) > OFFSET_TOLERANCE_S:
                raise ValueError(f"track {i}: offset_s {t.offset_s} != {expected_offset:.3f}")
            expected_offset += t.duration_s
        if self.audio_fingerprint != audio_fingerprint(t.sha256 for t in self.tracks):
            raise ValueError("audio_fingerprint does not match the track list")
        total = expected_offset + OFFSET_TOLERANCE_S

        prev: SentenceEntry | None = None
        last_audio = -1.0
        for i, s in enumerate(self.sentences):
            if s.n != i:
                raise ValueError("sentence n must be 0..n-1 in order")
            if prev is not None:
                if (s.spine_index, s.char_start) < (prev.spine_index, prev.char_start):
                    raise ValueError(f"sentence {s.n}: out of reading order")
                if s.spine_index == prev.spine_index and s.char_start < prev.char_end:
                    raise ValueError(f"sentence {s.n}: overlaps the previous sentence")
            prev = s
            if s.audio_start_s is not None:
                if s.audio_end_s > total:  # type: ignore[operator]
                    raise ValueError(f"sentence {s.n}: audio_end_s beyond the last track")
                if s.audio_start_s < last_audio:
                    raise ValueError(f"sentence {s.n}: audio goes backwards")
                last_audio = s.audio_start_s
        return self

    @property
    def coverage(self) -> float:
        return sum(1 for s in self.sentences if s.matched) / len(self.sentences)

    @property
    def total_duration_s(self) -> float:
        return sum(t.duration_s for t in self.tracks)


def load_sentence_map(path: Path) -> SentenceMap:
    return SentenceMap.model_validate_json(path.read_bytes())


def write_sentence_map(path: Path, sentence_map: SentenceMap) -> None:
    """Atomic replace so a reader never sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sentence_map.model_dump(mode="json"), indent=1) + "\n")
    os.replace(tmp, path)
