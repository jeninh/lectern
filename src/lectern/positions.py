"""Position conversions: text position <-> sentence <-> audio time <-> KOReader xpointer.

Everything positional in Lectern passes through sentence coordinates (``sentence_n`` plus a
character offset into that sentence), which is what ``work_progress`` stores. This module is
the single place that knows how to get into and out of those coordinates.

Status (Step 0): the coordinate types, the text normalisation that defines char offsets, and
the in-memory sentence index are implemented and tested. The xpointer conversions, which need
the EPUB's DOM, are declared here and implemented in milestone 4.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from lxml import etree

_XHTML_NS = "http://www.w3.org/1999/xhtml"
_XPOINTER_FRAGMENT = re.compile(r"^/body/DocFragment\[(\d+)\]")
_SKIP_TAGS = {"script", "style"}


@dataclass(frozen=True, order=True)
class TextPos:
    """A point in the work's text: spine item index and offset into its normalised text."""

    spine_index: int
    char: int


@dataclass(frozen=True)
class SentencePos:
    """Canonical position: a sentence and how far into it (characters)."""

    n: int
    offset: int = 0


class SentenceLike(Protocol):
    n: int
    spine_index: int
    char_start: int
    char_end: int
    audio_start_s: float | None
    audio_end_s: float | None
    matched: bool


def normalize_spine_text(xhtml: bytes) -> str:
    """The text a spine item contributes to the text timeline.

    Rules (frozen; changing them invalidates every stored sentence map):
    - text of ``<body>`` only; ``<script>`` and ``<style>`` contents are dropped
    - every run of whitespace (including newlines between block elements) becomes one space
    - leading and trailing whitespace is removed
    - no entity, case, or Unicode normalisation beyond what the XML parser does
    Offsets are Python ``str`` indices, i.e. code points.
    """
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=False)
    root = etree.fromstring(xhtml, parser)
    if root is None:
        return ""
    body = root.find(f".//{{{_XHTML_NS}}}body")
    if body is None:
        body = root.find(".//body")
    if body is None:
        body = root
    parts: list[str] = []
    _collect_text(body, parts)
    return " ".join("".join(parts).split())


def _collect_text(node, parts: list[str]) -> None:
    """Document-order text of ``node``'s subtree, without its own tail."""
    if not isinstance(node.tag, str):  # comment or processing instruction
        return
    if node.tag.rsplit("}", 1)[-1] in _SKIP_TAGS:
        return
    if node.text:
        parts.append(node.text)
    for child in node:
        _collect_text(child, parts)
        if child.tail:
            parts.append(child.tail)


class SentenceIndex:
    """In-memory lookups over one work's sentences (from the DB or a sentence map)."""

    def __init__(self, sentences: Iterable[SentenceLike]) -> None:
        self._s: Sequence[SentenceLike] = sorted(sentences, key=lambda s: s.n)
        if [s.n for s in self._s] != list(range(len(self._s))):
            raise ValueError("sentences must be numbered 0..n-1")
        self._text_keys = [(s.spine_index, s.char_start) for s in self._s]
        self._matched = [s for s in self._s if s.matched]
        self._audio_keys = [s.audio_start_s for s in self._matched]

    def __len__(self) -> int:
        return len(self._s)

    def sentence(self, n: int) -> SentenceLike:
        return self._s[n]

    def _clamp(self, s: SentenceLike, offset: int) -> SentencePos:
        return SentencePos(s.n, max(0, min(offset, s.char_end - s.char_start)))

    # --- text <-> sentence ---------------------------------------------------------------

    def at_text(self, pos: TextPos) -> SentencePos | None:
        """Sentence containing ``pos``. Between sentences, the next one at offset 0; after the
        last sentence, the last one at its end. ``None`` only for an empty index."""
        if not self._s:
            return None
        i = bisect_right(self._text_keys, (pos.spine_index, pos.char)) - 1
        if i >= 0:
            s = self._s[i]
            if s.spine_index == pos.spine_index and pos.char < s.char_end:
                return SentencePos(s.n, pos.char - s.char_start)
        nxt = i + 1
        if nxt < len(self._s):
            return SentencePos(self._s[nxt].n, 0)
        last = self._s[-1]
        return SentencePos(last.n, last.char_end - last.char_start)

    def to_text(self, pos: SentencePos) -> TextPos:
        s = self._s[pos.n]
        return TextPos(s.spine_index, s.char_start + self._clamp(s, pos.offset).offset)

    # --- audio <-> sentence --------------------------------------------------------------

    def at_time(self, t: float) -> SentencePos | None:
        """Matched sentence being spoken at ``t`` (work timeline), offset interpolated
        linearly within it. Before the first matched sentence: that sentence at 0. ``None``
        when nothing is matched."""
        if not self._matched:
            return None
        i = bisect_right(self._audio_keys, t) - 1
        if i < 0:
            return SentencePos(self._matched[0].n, 0)
        s = self._matched[i]
        start, end = s.audio_start_s, s.audio_end_s  # type: ignore[assignment]
        length = s.char_end - s.char_start
        if t >= end or end <= start:
            return SentencePos(s.n, length if t >= end else 0)
        return self._clamp(s, round(length * (t - start) / (end - start)))

    def to_time(self, pos: SentencePos) -> float | None:
        """Work-timeline seconds for a sentence position. For an unmatched sentence, the end
        of the nearest earlier matched sentence (or the start of the first matched one);
        ``None`` when nothing is matched."""
        if not self._matched:
            return None
        s = self._s[pos.n]
        if s.matched:
            start, end = s.audio_start_s, s.audio_end_s  # type: ignore[assignment]
            length = s.char_end - s.char_start
            frac = self._clamp(s, pos.offset).offset / length if length else 0.0
            return start + (end - start) * frac
        for prev in reversed(self._matched):
            if prev.n < s.n:
                return prev.audio_end_s
        return self._matched[0].audio_start_s


# --- KOReader xpointers (milestone 4) ------------------------------------------------------
#
# KOReader's EPUB positions look like ``/body/DocFragment[5]/body/p[3]/text().0``: DocFragment
# is the 1-based spine index, the rest is a path inside that item's DOM plus a char offset.


def xpointer_spine_index(xpointer: str) -> int | None:
    """0-based spine index named by a KOReader xpointer, or ``None`` if it is not one."""
    m = _XPOINTER_FRAGMENT.match(xpointer)
    return int(m.group(1)) - 1 if m else None


def xpointer_to_text(xpointer: str, spine_item_xhtml: bytes) -> TextPos:
    """Resolve a KOReader xpointer against the spine item it names.

    Implemented in milestone 4: walks the DOM path, then maps the node's text offset onto the
    normalised-text offset defined by :func:`normalize_spine_text`.
    """
    raise NotImplementedError("xpointer_to_text lands in milestone 4")


def text_to_xpointer(pos: TextPos, spine_item_xhtml: bytes) -> str:
    """Inverse of :func:`xpointer_to_text`. Implemented in milestone 4."""
    raise NotImplementedError("text_to_xpointer lands in milestone 4")
