"""EPUB text extraction and sentence segmentation.

The whole spine becomes a list of :class:`SpineItem` (normalised text plus source map) and
one numbered list of :class:`SentenceSpan` in reading order. Sentence spans are what the
sentence map stores, so segmentation is deterministic: same bytes in, same spans out.
"""

from __future__ import annotations

import re
import zipfile
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from lectern.ingest.epub import EpubError, read_spine
from lectern.textmap import SpineText

MAX_ITEM_BYTES = 8 * 1024 * 1024
# Uncompressed spine bytes read per book; items past it contribute no text.
MAX_TOTAL_BYTES = 64 * 1024 * 1024
# A sentence longer than this is split again at semicolons/colons: finer granularity makes
# interpolation and highlighting more useful, and such run-ons are common in older prose.
LONG_SENTENCE_CHARS = 500

_TERMINATOR = re.compile(r"[.!?…]+[\"”’')\]]*")
_SECONDARY = re.compile(r"[;:]\s")
_ABBREVIATIONS = frozenset(
    """mr mrs ms dr st prof sr jr vs etc no mt ft inc ltd co vol ch pp p fig ed eds viz cf
    rev hon gen col capt lt sgt messrs mme mlle m""".split()
)
_WORD_BEFORE = re.compile(r"([^\W\d_]+)\.$")


@dataclass(frozen=True)
class SpineItem:
    index: int
    href: str
    text: SpineText


@dataclass(frozen=True)
class SentenceSpan:
    n: int
    spine_index: int
    char_start: int
    char_end: int  # exclusive
    text: str


def iter_spine_items(epub: Path) -> Iterator[SpineItem]:
    """Every spine item of the EPUB, parsed one at a time, in spine order.

    Real EPUBs are messy, and one bad item must not sink the whole book, so an item
    contributes no text (but keeps its index) when its entry is missing, damaged, empty,
    larger than ``MAX_ITEM_BYTES``, or not parseable, and once the items read so far add up
    to ``MAX_TOTAL_BYTES``. Server and worker use this same function, so both agree on every
    item's text length. Only an unreadable package (no OPF/spine) raises.
    """
    hrefs = read_spine(epub, strict=False)
    total = 0
    with zipfile.ZipFile(epub) as zf:
        for i, href in enumerate(hrefs):
            raw = b""
            try:
                info = zf.getinfo(href)
                if info.file_size <= MAX_ITEM_BYTES and total + info.file_size <= MAX_TOTAL_BYTES:
                    raw = zf.read(info)  # ZipExtFile stops at the declared file_size
            except (KeyError, zipfile.BadZipFile, zlib.error, OSError, RuntimeError, ValueError):
                raw = b""
            total += len(raw)
            yield SpineItem(i, href, SpineText(raw))


def read_spine_items(epub: Path) -> list[SpineItem]:
    """All of :func:`iter_spine_items` in memory (tests and small books)."""
    return list(iter_spine_items(epub))


def read_spine_item(epub: Path, spine_index: int) -> bytes:
    """Raw bytes of one spine item (``b""`` when the index or entry does not exist or cannot
    be read)."""
    hrefs = read_spine(epub, strict=False)
    if not 0 <= spine_index < len(hrefs):
        return b""
    with zipfile.ZipFile(epub) as zf:
        try:
            info = zf.getinfo(hrefs[spine_index])
            return zf.read(info) if info.file_size <= MAX_ITEM_BYTES else b""
        except (KeyError, zipfile.BadZipFile, zlib.error, OSError, RuntimeError, ValueError):
            return b""


def spine_lengths(epub: Path) -> list[int]:
    """Normalised text length of every spine item (what ``char_end`` is checked against).
    Items are parsed one at a time and dropped, so memory stays at one item's worth."""
    try:
        return [len(item.text.text) for item in iter_spine_items(epub)]
    except (EpubError, zipfile.BadZipFile, OSError):
        return []


def segment_items(items: Iterable[SpineItem]) -> list[SentenceSpan]:
    """Number the sentences of the whole spine in reading order (consumes ``items`` once, so
    a generator keeps only one parsed item alive at a time)."""
    out: list[SentenceSpan] = []
    for item in items:
        for start, end in segment_text(item.text.text, item.text.block_starts()):
            out.append(SentenceSpan(len(out), item.index, start, end, item.text.text[start:end]))
    return out


def segment_text(text: str, block_starts: list[int] | None = None) -> list[tuple[int, int]]:
    """Sentence spans ``[start, end)`` of a normalised text.

    A sentence never crosses a block boundary (paragraph, heading, list item); inside a
    block it ends at ``.``, ``!``, ``?`` or ``…`` (plus closing quotes/brackets) followed by
    whitespace and something that is not a lowercase letter, unless the word before the
    period is a common abbreviation or an initial. Spans are trimmed of whitespace and never
    empty.
    """
    bounds = sorted({0, len(text), *(b for b in (block_starts or []) if 0 < b < len(text))})
    spans: list[tuple[int, int]] = []
    for b0, b1 in zip(bounds, bounds[1:], strict=False):
        spans.extend(_segment_block(text, b0, b1))
    return spans


def _segment_block(text: str, b0: int, b1: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = b0
    for m in _TERMINATOR.finditer(text, b0, b1):
        end = m.end()
        if end < b1 and not text[end].isspace():
            continue
        nxt = _next_non_space(text, end, b1)
        if nxt is not None and text[nxt].islower():
            continue
        if _is_abbreviation(text, m.start(), start):
            continue
        spans.extend(_trimmed(text, start, end))
        start = end
    spans.extend(_trimmed(text, start, b1))
    return spans


def _next_non_space(text: str, i: int, limit: int) -> int | None:
    while i < limit and text[i].isspace():
        i += 1
    return i if i < limit else None


def _is_abbreviation(text: str, punct_at: int, start: int) -> bool:
    if text[punct_at] != ".":
        return False
    word = _WORD_BEFORE.search(text, start, punct_at + 1)
    if word is None:
        return False
    w = word.group(1)
    if len(w) == 1 and w.isupper():
        return True  # an initial: "O. Henry", "J. R. R."
    return w.lower() in _ABBREVIATIONS


def _trimmed(text: str, start: int, end: int) -> list[tuple[int, int]]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end:
        return []
    if end - start <= LONG_SENTENCE_CHARS:
        return [(start, end)]
    pieces: list[tuple[int, int]] = []
    piece_start = start
    for m in _SECONDARY.finditer(text, start, end):
        if m.start() + 1 - piece_start >= LONG_SENTENCE_CHARS // 2:
            pieces.append((piece_start, m.start() + 1))
            piece_start = m.end()
    pieces.append((piece_start, end))
    return [p for piece in pieces for p in _trimmed_simple(text, *piece)]


def _trimmed_simple(text: str, start: int, end: int) -> list[tuple[int, int]]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return [(start, end)] if start < end else []
