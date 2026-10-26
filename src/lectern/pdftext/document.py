"""The canonical text of a PDF: what alignment, narration, and read-along read.

One JSON document per PDF file (``<work dir>/derived/pdf-text-<sha256 prefix>.json``), built by
:func:`build_document` from the words :mod:`lectern.pdftext.extract` read and the reading
order :mod:`lectern.pdftext.layout` found. It is deterministic: the same PDF and the same
:data:`DOCUMENT_VERSION` give the same bytes, and its SHA-256 (``text_sha256``) is what jobs,
sentence maps, and alignments record as their text input, exactly as they record an EPUB's
SHA-256. A new extractor version is a new text, so an alignment of the old one goes stale.

Coordinates (docs/pdf-read-along.md)
    text   ``(section, char)``: a section is to a PDF what a spine item is to an EPUB; the
           character offset counts code points of the section's ``text``
    page   0-based page index; boxes are ``[x0, y0, x1, y1]`` in PDF user space (points,
           origin bottom left, before ``/Rotate``), as pdf.js's
           ``viewport.convertToViewportRectangle`` expects

Shape (version 1)::

    {"version": 1, "pdf_sha256": "…", "status": "ready" | "needs_ocr",
     "pages": [{"w", "h", "x0", "y0", "rotation", "label", "text": bool}],
     "sections": [{"title", "page_start", "page_end", "from_outline",
                   "text", "blocks": [[start, kind]],
                   "words": [[start, end, page, x0, y0, x1, y1]]}],
     "stats": {"pages", "pages_with_text", "chars", "columns", "margin_lines_removed",
               "truncated"}}
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from lectern.pdftext.extract import RawDoc
from lectern.pdftext.layout import NEEDS_OCR_SHARE, layout, plan_sections

DOCUMENT_VERSION = 1


@dataclass(frozen=True)
class TextBlock:
    kind: str  # "p" or "h"
    start: int
    end: int


class SectionText:
    """A section's text with the block structure :mod:`lectern.align.text` and the narration
    script use (the same interface as :class:`lectern.textmap.SpineText` for that purpose)."""

    __slots__ = ("_kinds", "_starts", "text")

    def __init__(self, text: str, blocks: list[list]) -> None:
        self.text = text
        self._starts = [int(b[0]) for b in blocks] or [0]
        self._kinds = [str(b[1]) for b in blocks] or ["p"]

    def block_starts(self) -> list[int]:
        return list(self._starts)

    def block_at(self, char: int) -> int:
        return max(0, bisect_right(self._starts, char) - 1)

    def blocks(self) -> list[TextBlock]:
        ends = self._starts[1:] + [len(self.text)]
        out = []
        for start, end, kind in zip(self._starts, ends, self._kinds, strict=True):
            # blocks are joined by one space: the space belongs to no block
            stop = end - 1 if end < len(self.text) else end
            if stop > start:
                out.append(TextBlock("h2" if kind == "h" else "p", start, stop))
        return out


def build_document(raw: RawDoc, pdf_sha256: str) -> dict:
    lay = layout(raw)
    plans = plan_sections(lay.blocks, raw.toc, len(raw.pages))
    sections = []
    for k, plan in enumerate(plans):
        last = (plans[k + 1].first_block if k + 1 < len(plans) else len(lay.blocks)) - 1
        text_parts: list[str] = []
        blocks: list[list] = []
        words: list[list] = []
        pos = 0
        for b in lay.blocks[plan.first_block : last + 1]:
            if text_parts:
                text_parts.append(" ")
                pos += 1
            blocks.append([pos, b.kind])
            text_parts.append(b.text)
            for f in b.fragments:
                words.append([pos + f.start, pos + f.end, f.page, *(round(v, 2) for v in f.box)])
            pos += len(b.text)
        sections.append(
            {
                "title": plan.title,
                "page_start": plan.page_start,
                "page_end": plan.page_end,
                "from_outline": plan.from_outline,
                "text": "".join(text_parts),
                "blocks": blocks,
                "words": words,
            }
        )
    with_text = sum(lay.pages_with_text)
    pages = len(raw.pages)
    status = "ready" if pages and with_text / pages >= NEEDS_OCR_SHARE and sections else "needs_ocr"
    return {
        "version": DOCUMENT_VERSION,
        "pdf_sha256": pdf_sha256,
        "status": status,
        "pages": [
            {
                "w": p.w,
                "h": p.h,
                "x0": p.x0,
                "y0": p.y0,
                "rotation": p.rotation,
                "label": p.label,
                "text": has,
            }
            for p, has in zip(raw.pages, lay.pages_with_text, strict=True)
        ],
        "sections": sections if status == "ready" else [],
        "stats": {
            "pages": pages,
            "pages_with_text": with_text,
            "chars": sum(len(s["text"]) for s in sections),
            "columns": lay.columns,
            "margin_lines_removed": lay.margin_lines_removed,
            "truncated": raw.truncated,
        },
    }


def dumps(doc: dict) -> bytes:
    """Canonical bytes: their SHA-256 is the document's identity."""
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_text_document(data: bytes) -> bool:
    return data[:1] == b"{"


def section_texts(doc: dict) -> list[SectionText]:
    return [SectionText(s["text"], s["blocks"]) for s in doc.get("sections") or []]


# --- sentence boxes -------------------------------------------------------------------------------


def sentence_boxes(
    section: dict, start: int, end: int, starts: list[int] | None = None
) -> list[list[float]]:
    """Highlight rectangles for the characters ``[start, end)`` of a section: one per line
    piece, ``[page, x0, y0, x1, y1]``. A word cut by the span (a sentence that ends inside a
    word's box) contributes the matching share of its width. ``starts`` (the words' first
    characters) is built once per section by a caller that asks for every sentence."""
    words = section["words"]
    if starts is None:
        starts = [w[0] for w in words]
    i = max(0, bisect_left(starts, start) - 1)
    rects: list[list[float]] = []
    while i < len(words) and words[i][0] < end:
        ws, we, page, x0, y0, x1, y1 = words[i]
        i += 1
        a, b = max(ws, start), min(we, end)
        if a >= b:
            continue
        if we > ws and (a > ws or b < we):
            width = x1 - x0
            x0, x1 = x0 + width * (a - ws) / (we - ws), x0 + width * (b - ws) / (we - ws)
        if rects:
            r = rects[-1]
            same_line = r[0] == page and min(r[4], y1) - max(r[2], y0) >= 0.5 * min(
                r[4] - r[2], y1 - y0
            )
            if same_line and x0 >= r[1] - 0.5:
                r[2], r[3], r[4] = min(r[2], y0), max(r[3], x1), max(r[4], y1)
                continue
        rects.append([page, x0, y0, x1, y1])
    return [[int(r[0]), *(round(v, 1) for v in r[1:])] for r in rects]
