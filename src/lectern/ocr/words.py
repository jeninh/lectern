"""Recognised words in the shape the text-layer reader of text PDFs produces.

A page is ``{"index", "w", "h", "x0", "y0", "rotation", "label", "chars", "map_errors",
"words": [[text, x0, y0, x1, y1, size, flags], …]}``: the page's crop box (size and lower
left corner, points), its ``/Rotate`` and page label, and every word with its box in PDF user
space (points, origin bottom left, before ``/Rotate``). ``flags``: 1 = the word ends in a
hyphen at the end of a line, 2 = a line break follows it, 4 = bold (OCR never sets it). This
is ``lectern.pdftext.extract``'s batch page, so the same layout step (running heads, page
numbers, columns, dehyphenation, Unicode) reads recognised and born-digital text alike.

From Tesseract's TSV (one row per page, block, paragraph, line, and word, with pixel boxes on
the rendered image): words come in Tesseract's reading order, and each takes its *line's*
top and bottom, so boxes on a line have even heights, like PDFium's loose character boxes.
``size`` is an estimate of the font size from the line height.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

FLAG_HYPHEN = 1
FLAG_BREAK = 2
FLAG_BOLD = 4
# Same caps as the text-layer reader: characters read from one page.
MAX_PAGE_CHARS = 30_000
MAX_PAGE_WORDS = 15_000
MAX_WORD_CHARS = 200
# Words Tesseract is less sure of than this (0-100) are noise: specks, pictures, ornaments.
MIN_CONFIDENCE = 15.0
# A line's height is about 1.2 times the size of its font.
LINE_TO_SIZE = 1 / 1.2
_HYPHENS = ("-", "\u2010", "\u2011", "\u00ac")

# (device x, device y) on the rendered image → (x, y) in PDF user space
ToPage = Callable[[float, float], tuple[float, float]]


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    return "".join(c for c in text if not c.isspace() and unicodedata.category(c) != "Cc")


def from_tsv(tsv: str, to_page: ToPage, pt_per_px: float) -> list[list]:
    """Words, in reading order, from Tesseract's TSV output for one page rendered at
    ``pt_per_px`` points per pixel."""
    lines: dict[tuple[int, int, int], tuple[int, int]] = {}
    rows: list[tuple[tuple[int, int, int], int, int, int, str]] = []
    for raw in tsv.splitlines()[1:]:
        cols = raw.split("\t")
        if len(cols) < 12:
            continue
        try:
            level, block, par, line = (int(c) for c in (cols[0], cols[2], cols[3], cols[4]))
            left, top, width, height = (int(c) for c in cols[6:10])
            conf = float(cols[10])
        except ValueError:
            continue
        key = (block, par, line)
        if level == 4:
            lines[key] = (top, top + height)
        elif level == 5:
            text = _clean(cols[11])[:MAX_WORD_CHARS]
            if text and conf >= MIN_CONFIDENCE:
                rows.append((key, left, left + width, top, text))
    words: list[list] = []
    chars = 0
    for i, (key, left, right, top, text) in enumerate(rows):
        if len(words) >= MAX_PAGE_WORDS or chars + len(text) > MAX_PAGE_CHARS:
            break
        chars += len(text)
        line_top, line_bottom = lines.get(key, (top, top))
        if line_bottom <= line_top:
            line_bottom = line_top + 1
        # both corners: rotation and crop box offsets are the mapping's business
        ax, ay = to_page(left, line_top)
        bx, by = to_page(right, line_bottom)
        x0, x1 = sorted((ax, bx))
        y0, y1 = sorted((ay, by))
        last = i + 1 == len(rows) or rows[i + 1][0] != key
        flags = 0
        if last:
            flags |= FLAG_BREAK
            if text.endswith(_HYPHENS) and len(text) > 1:
                flags |= FLAG_HYPHEN
                text = text.rstrip("".join(_HYPHENS)) + "-"
        size = (line_bottom - line_top) * pt_per_px * LINE_TO_SIZE
        words.append([text, *(round(v, 2) for v in (x0, y0, x1, y1, size)), flags])
    return words


# --- validation (pages from a worker, or read back from disk) ---------------------------------


class PageEntry(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    index: int = Field(ge=0, le=100_000)
    words: list[list] = Field(max_length=MAX_PAGE_WORDS)
    conf: float | None = Field(default=None, ge=0, le=100)

    @field_validator("words")
    @classmethod
    def _words(cls, v: list[list]) -> list[list]:
        chars = 0
        out = []
        for w in v:
            if len(w) != 7 or not isinstance(w[0], str):
                raise ValueError("a word is [text, x0, y0, x1, y1, size, flags]")
            text = _clean(w[0])[:MAX_WORD_CHARS]
            nums = w[1:6]
            if not all(isinstance(n, int | float) and not isinstance(n, bool) for n in nums):
                raise ValueError("word boxes are numbers")
            if not isinstance(w[6], int) or isinstance(w[6], bool) or not 0 <= w[6] < 8:
                raise ValueError("word flags are 0-7")
            if not all(abs(float(n)) <= 1e6 for n in nums):
                raise ValueError("word box out of range")
            x0, y0, x1, y1, size = (float(n) for n in nums)
            if not text or x1 < x0 or y1 < y0 or size < 0:
                raise ValueError("empty word or inverted box")
            chars += len(text)
            if chars > MAX_PAGE_CHARS:
                raise ValueError("too much text on a page")
            out.append([text, x0, y0, x1, y1, size, w[6] & ~FLAG_BOLD])
        return out


def validated_page(raw: dict, geometry: dict) -> dict:
    """A page as stored: the server's own geometry for that page (``geometry`` from the scan,
    never the worker's), and the given words, clipped to the page's crop box."""
    try:
        page = PageEntry.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(str(exc)[:300]) from exc
    left, bottom = float(geometry["x0"]), float(geometry["y0"])
    right, top = left + float(geometry["w"]), bottom + float(geometry["h"])
    words = []
    for text, x0, y0, x1, y1, size, flags in page.words:
        x0, x1 = min(max(x0, left), right), min(max(x1, left), right)
        y0, y1 = min(max(y0, bottom), top), min(max(y1, bottom), top)
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            continue
        words.append([text, *(round(v, 2) for v in (x0, y0, x1, y1, size)), flags])
    out = {
        "index": page.index,
        "w": geometry["w"],
        "h": geometry["h"],
        "x0": geometry["x0"],
        "y0": geometry["y0"],
        "rotation": geometry["rotation"],
        "label": geometry.get("label"),
        "chars": sum(len(w[0]) for w in words),
        "map_errors": 0,
        "words": words,
    }
    if page.conf is not None:
        out["conf"] = round(page.conf, 1)
    return out
