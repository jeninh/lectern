"""Reading-order text from the words of a PDF (pure Python, deterministic).

Input is what :mod:`lectern.pdftext.extract` reads: every page's words with their boxes in PDF
user space. Output is a :class:`lectern.pdftext.document.TextDocument`: sections of normalised
text, block (paragraph) boundaries, and for every piece of every word its page and box.

The steps, each one small and tested on its own (tests/test_pdf_text.py):

1. **Unicode.** Each word is NFC-normalised; ligatures and other presentation forms
   (``U+FB00–U+FDFF``, ``U+FE70–U+FEFF``, fullwidth and halfwidth forms) are expanded with
   NFKC (``ﬁ`` → ``fi``), zero-width and private-use characters dropped, a soft hyphen inside a
   word removed. Nothing else is folded: curly quotes, dashes, and ``…`` stay as printed.
2. **Pages without text.** A page whose characters mostly failed to map to Unicode (a broken
   font) is treated as having no text.
3. **Running headers, footers, and page numbers.** The first and last two lines of a page that
   lie in its top or bottom 12 % are candidates. A candidate is removed if it is a page number
   (``12``, ``- 12 -``, ``Page 12``, ``12 of 300``, ``xii``) or if the same text with digits
   ignored, at the same font size, is a candidate on at least two pages and 40 % of the pages
   (so alternating left/right headers go too, and a large title that happens to repeat the
   running header does not).
   Headings are short paragraphs set larger than the body text, or entirely in bold.
4. **Columns.** Recursive XY-cut: a region splits at a wide horizontal gap (more than 1.5 line
   heights, e.g. under a title), else at a vertical gutter that no word crosses and that has
   lines on both sides (columns, read left to right), else it is a leaf. Leaves are read in
   order; inside one, words form lines by vertical overlap.
5. **Paragraphs.** A new paragraph starts at a larger gap than the region's usual line gap, a
   change of font size, or an indented line after a finished sentence. A paragraph that ends
   without closing punctuation continues in the next region or on the next page.
6. **Hyphenation.** A line ending in a hyphen followed by a line starting with a lowercase
   letter is joined into one word, unless the hyphen belongs to the word: the hyphenated form
   occurs elsewhere in the document mid-line, the first part already has a hyphen, or it is
   one of a few prefixes that keep theirs (``self-``, ``well-``, ``twenty-`` … ``ninety-``).
   Before a capital or a digit (``Anglo-`` / ``Saxon``, ``1914-`` / ``18``) the hyphen stays
   and no space is added; so does a dash attached to a word (``word—`` / ``next``).
7. **Sections.** From the outline when there is one (top-level entries, the section starting at
   the paragraph on the entry's page whose text begins with the entry's title, else at the
   first paragraph that starts on that page), otherwise from page ranges of
   :data:`PAGE_RANGE` pages. Sections never split a paragraph: a paragraph belongs to the
   section it starts in. Sections are what alignment, narration, and read-along call spine
   items, and what a generated audiobook makes one track of.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from lectern.pdftext.extract import FLAG_BOLD, FLAG_HYPHEN, RawDoc, RawPage

MARGIN_BAND = 0.12
REPEAT_SHARE = 0.4
PAGE_RANGE = 10
MIN_PAGE_CHARS = 40  # a page with fewer body characters has no usable text
NEEDS_OCR_SHARE = 0.5  # below this share of pages with text, the PDF needs OCR
MAP_ERROR_SHARE = 0.3
TITLE_MAX = 120

_ZERO_WIDTH = frozenset("\u200b\u200c\u200d\u2060\ufeff")
_HYPHENS = ("-", "\u2010", "\u00ad")
_TERMINAL = tuple(".!?…:;")
_CLOSERS = "\"'”’»)]"
_PAGE_NUMBER = re.compile(
    r"^(?:page\s+|p\.\s*)?[-–—\s]*(?:\d{1,5}|[ivxlcdm]{1,8})[-–—\s]*"
    r"(?:(?:of|/)\s*\d{1,5})?$",
    re.I,
)
_KEEP_PREFIXES = frozenset(
    "self well ill half all ex quasi twenty thirty forty fifty sixty seventy eighty ninety".split()
)
_WORD_CORE = re.compile(r"[^\W_][\w'’-]*[^\W_]|[^\W_]", re.UNICODE)


def normalize_word(text: str) -> str:
    """Step 1: NFC, presentation forms expanded, invisible and private-use characters dropped."""
    out: list[str] = []
    for ch in unicodedata.normalize("NFC", text):
        cp = ord(ch)
        if ch in _ZERO_WIDTH:
            continue
        if 0xFB00 <= cp <= 0xFDFF or 0xFE70 <= cp <= 0xFEFF or 0xFF00 <= cp <= 0xFFEF:
            out.append(unicodedata.normalize("NFKC", ch))
            continue
        if ch == "\u00ad":
            out.append(ch)  # decided below: a soft hyphen at the end marks a line-end break
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs"):
            continue
        out.append(ch)
    s = "".join(out)
    if "\u00ad" in s[:-1]:
        s = s[:-1].replace("\u00ad", "") + s[-1]
    return s


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    flags: int
    page: int

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 0.01)


@dataclass
class Line:
    words: list[Word]

    @property
    def x0(self) -> float:
        return min(w.x0 for w in self.words)

    @property
    def x1(self) -> float:
        return max(w.x1 for w in self.words)

    @property
    def y0(self) -> float:
        return min(w.y0 for w in self.words)

    @property
    def y1(self) -> float:
        return max(w.y1 for w in self.words)

    @property
    def size(self) -> float:
        sizes = sorted(w.size for w in self.words)
        return sizes[len(sizes) // 2]

    @property
    def page(self) -> int:
        return self.words[0].page

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class Paragraph:
    lines: list[Line]
    kind: str = "p"  # "p" or "h"

    @property
    def page(self) -> int:
        return self.lines[0].page

    @property
    def size(self) -> float:
        return statistics.median(ln.size for ln in self.lines)


@dataclass
class Fragment:
    start: int
    end: int
    page: int
    box: tuple[float, float, float, float]


@dataclass
class Block:
    kind: str
    page: int
    text: str
    fragments: list[Fragment] = field(default_factory=list)


# --- step 2 and 3: pages and margins ------------------------------------------------------------


def _words(page: RawPage) -> list[Word]:
    if page.chars and page.map_errors / page.chars > MAP_ERROR_SHARE:
        return []  # glyphs without a usable Unicode mapping: as good as no text
    out = []
    for text, x0, y0, x1, y1, size, flags in page.words:
        t = normalize_word(str(text))
        if t.endswith("\u00ad"):
            t, flags = t[:-1] + "-", flags | FLAG_HYPHEN
        if not t.strip():
            if flags and out:
                out[-1].flags |= flags
            continue
        size = float(size) or float(y1) - float(y0)
        out.append(
            Word(t, float(x0), float(y0), float(x1), float(y1), size, int(flags), page.index)
        )
    return out


def group_lines(words: list[Word]) -> list[Line]:
    """Words into lines by vertical overlap, top to bottom, each line left to right."""
    lines: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (-w.cy, w.x0)):
        for ln in reversed(lines[-3:]):
            ref = ln[0]
            overlap = min(ref.y1, w.y1) - max(ref.y0, w.y0)
            if overlap >= 0.5 * min(ref.height, w.height):
                ln.append(w)
                break
        else:
            lines.append([w])
    out = [Line(sorted(ln, key=lambda w: w.x0)) for ln in lines]
    out.sort(key=lambda ln: -(ln.y0 + ln.y1) / 2)
    return out


def _margin_key(line: Line, band: str) -> tuple[str, str, int]:
    text = re.sub(r"\d+", "#", line.text.casefold())
    text = re.sub(r"\b[ivxlcdm]{1,8}\b", "#", text)
    return band, " ".join(text.split()), round(line.size)


def _margin_candidates(words: list[Word], page: RawPage) -> list[tuple[Line, tuple]]:
    if not words:
        return []
    lines = group_lines(words)
    top = page.y0 + page.h
    out = []
    for ln in lines[:2]:
        if ln.y0 >= top - MARGIN_BAND * page.h:
            out.append((ln, _margin_key(ln, "top")))
    for ln in lines[-2:]:
        if ln.y1 <= page.y0 + MARGIN_BAND * page.h and all(ln is not c for c, _ in out):
            out.append((ln, _margin_key(ln, "bottom")))
    return out


def strip_margins(pages: list[tuple[RawPage, list[Word]]]) -> tuple[list[list[Word]], int]:
    """Step 3. Returns every page's words without its running headers, footers, and page
    numbers, and how many lines were removed."""
    candidates = [_margin_candidates(words, page) for page, words in pages]
    with_words = sum(1 for _, words in pages if words)
    counts = Counter(key for cands in candidates for key in {k for _, k in cands})
    need = max(2, REPEAT_SHARE * with_words)
    out: list[list[Word]] = []
    removed = 0
    for (_page, words), cands in zip(pages, candidates, strict=True):
        drop: set[int] = set()
        for line, key in cands:
            if _PAGE_NUMBER.match(line.text.strip()) or counts[key] >= need:
                drop.update(id(w) for w in line.words)
                removed += 1
        out.append([w for w in words if id(w) not in drop])
    return out, removed


# --- step 4: columns -----------------------------------------------------------------------------


def _gaps(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Empty ``(start, end)`` stretches between the merged intervals, in order."""
    out = []
    end = None
    for a, b in sorted(intervals):
        if end is not None and a > end:
            out.append((end, a))
        end = b if end is None else max(end, b)
    return out


def xy_cut(words: list[Word], depth: int = 0) -> list[list[Word]]:
    """Step 4: regions of a page in reading order."""
    if len(words) < 2 or depth > 40:
        return [words] if words else []
    sizes = sorted(w.size for w in words)
    size = sizes[len(sizes) // 2] or 10.0
    heights = sorted(w.height for w in words)
    line_h = heights[len(heights) // 2]
    # A wide horizontal gap: under a title, around a figure, between header and body.
    h_gaps = _gaps([(-w.y1, -w.y0) for w in words])
    big = [g for g in h_gaps if g[1] - g[0] > 1.5 * line_h]
    if big:
        widest = max(big, key=lambda g: g[1] - g[0])
        cut = -(widest[0] + widest[1]) / 2
        above = [w for w in words if w.cy > cut]
        below = [w for w in words if w.cy <= cut]
        return xy_cut(above, depth + 1) + xy_cut(below, depth + 1)
    # A vertical gutter with several lines on both sides: columns.
    v_gaps = [g for g in _gaps([(w.x0, w.x1) for w in words]) if g[1] - g[0] >= 1.2 * size]
    best = None
    for g in sorted(v_gaps, key=lambda g: g[0] - g[1]):
        mid = (g[0] + g[1]) / 2
        left = [w for w in words if w.x1 <= mid]
        right = [w for w in words if w.x0 >= mid]
        if len(group_lines(left)) >= 2 and len(group_lines(right)) >= 2:
            best = (left, right)
            break
    if best is not None:
        return xy_cut(best[0], depth + 1) + xy_cut(best[1], depth + 1)
    return [words]


# --- step 5: paragraphs -------------------------------------------------------------------------


def _ends_sentence(text: str) -> bool:
    return text.rstrip(_CLOSERS).endswith(_TERMINAL)


def paragraphs_of(region: list[Word], body_size: float) -> list[Paragraph]:
    lines = group_lines(region)
    if not lines:
        return []
    gaps = [a.y0 - b.y1 for a, b in zip(lines, lines[1:], strict=False)]
    usual = statistics.median(gaps) if gaps else 0.0
    left = min(ln.x0 for ln in lines)
    out: list[Paragraph] = [Paragraph([lines[0]])]
    for prev, ln, gap in zip(lines, lines[1:], gaps, strict=False):
        size = ln.size
        new = (
            gap > usual + 0.45 * size
            or abs(size - prev.size) > 0.15 * max(size, prev.size)
            or (ln.x0 - left > 1.0 * size and _ends_sentence(prev.text))
        )
        if new:
            out.append(Paragraph([ln]))
        else:
            out[-1].lines.append(ln)
    for p in out:
        words = [w for ln in p.lines for w in ln.words]
        if len(p.lines) <= 3 and (
            p.size >= 1.15 * body_size
            or (
                p.size >= 0.95 * body_size
                and all(w.flags & FLAG_BOLD for w in words)
                and not _ends_sentence(words[-1].text)
            )
        ):
            p.kind = "h"
    return out


def _continues(prev: Paragraph, nxt: Paragraph) -> bool:
    """A paragraph cut by a column or page break: no closing punctuation before the break (or
    a line-end hyphen), the same kind and size of type on both sides."""
    if prev.kind != "p" or nxt.kind != "p":
        return False
    if abs(prev.size - nxt.size) > 0.1 * max(prev.size, nxt.size):
        return False
    last = prev.lines[-1].words[-1]
    first = nxt.lines[0].words[0].text
    if last.flags & FLAG_HYPHEN or last.text.endswith(_HYPHENS):
        return first[:1].islower()
    if not _ends_sentence(last.text):
        # a lone line before a capital is a byline, a caption, or an unstyled heading
        return not (len(prev.lines) == 1 and first[:1].isupper())
    return first[:1].islower()


# --- step 6: words into text --------------------------------------------------------------------


def _core(text: str) -> str:
    m = _WORD_CORE.search(text)
    return m.group(0).casefold() if m else ""


def vocabulary(lines: list[Line]) -> Counter:
    """Word forms seen anywhere but at a hyphenated line end (for step 6)."""
    vocab: Counter = Counter()
    for ln in lines:
        for i, w in enumerate(ln.words):
            if i == len(ln.words) - 1 and (w.flags & FLAG_HYPHEN or w.text.endswith(_HYPHENS)):
                continue
            core = _core(w.text)
            if core:
                vocab[core] += 1
    return vocab


def _join_hyphenated(first: Word, nxt: Word, vocab: Counter) -> bool | None:
    """How a line ending in ``first`` joins the next line's ``nxt``: ``True`` = one word, the
    hyphen dropped; ``False`` = no space, the hyphen (or dash) kept; ``None`` = a space."""
    text = first.text
    if len(text) >= 2 and text.endswith(("—", "–")) and text[-2].isalnum():
        return False  # "word—" at a line end runs on into the next word
    if len(text) < 2 or not text.endswith(_HYPHENS) or not text[-2].isalnum():
        return None
    if not nxt.text[:1].isalnum():
        return None
    if not nxt.text[:1].islower():
        return False  # "Anglo-" / "Saxon", "1914-" / "1918": the hyphen belongs to the word
    base = text[:-1]
    head = _core(base)
    tail = _core(nxt.text)
    if not head or not tail:
        return None
    if "-" in base or head in _KEEP_PREFIXES or vocab.get(f"{head}-{tail}", 0):
        return False
    return True


def paragraph_block(p: Paragraph, vocab: Counter) -> Block:
    """One paragraph as text plus a fragment (page and box) per word piece."""
    words = [w for ln in p.lines for w in ln.words]
    ends = {id(ln.words[-1]) for ln in p.lines[:-1]}
    parts: list[str] = []
    frags: list[Fragment] = []
    pos = 0
    join_next = None  # what joins words[i] and words[i+1]
    for i, w in enumerate(words):
        text = w.text
        if join_next is True:
            pass
        elif i > 0:
            parts.append(" ")
            pos += 1
        join_next = None
        if id(w) in ends and i + 1 < len(words):
            join_next = _join_hyphenated(w, words[i + 1], vocab)
            if join_next is True:
                text = text[:-1]
        parts.append(text)
        frags.append(Fragment(pos, pos + len(text), w.page, (w.x0, w.y0, w.x1, w.y1)))
        pos += len(text)
        if join_next is False:
            join_next = True  # keep the hyphen, but no space before the next part
    return Block(p.kind, p.page, "".join(parts), frags)


# --- everything ----------------------------------------------------------------------------------


@dataclass
class Layout:
    blocks: list[Block]
    pages_with_text: list[bool]
    margin_lines_removed: int
    columns: int  # pages read as more than one column


def layout(raw: RawDoc) -> Layout:
    words_by_page = [(p, _words(p)) for p in raw.pages]
    body, removed = strip_margins(words_by_page)
    sizes = [w.size for page in body for w in page for _ in range(len(w.text))]
    body_size = statistics.median(sizes) if sizes else 10.0
    pages_with_text = [sum(len(w.text) for w in page) >= MIN_PAGE_CHARS for page in body]
    paragraphs: list[Paragraph] = []
    columns = 0
    for page_words in body:
        regions = xy_cut(page_words)
        if _column_count(regions) > 1:
            columns += 1
        for region in regions:
            for p in paragraphs_of(region, body_size):
                if paragraphs and _continues(paragraphs[-1], p):
                    paragraphs[-1].lines.extend(p.lines)
                else:
                    paragraphs.append(p)
    vocab = vocabulary([ln for p in paragraphs for ln in p.lines])
    blocks = [b for b in (paragraph_block(p, vocab) for p in paragraphs) if b.text.strip()]
    return Layout(blocks, pages_with_text, removed, columns)


def _column_count(regions: list[list[Word]]) -> int:
    """Regions side by side (overlapping vertically, not horizontally): the page's columns."""
    boxes = [
        (min(w.x0 for w in r), max(w.x1 for w in r), min(w.y0 for w in r), max(w.y1 for w in r))
        for r in regions
        if r
    ]
    best = 1
    for a in boxes:
        side = 1 + sum(1 for b in boxes if b is not a and (b[0] >= a[1] or b[1] <= a[0])
                       and min(a[3], b[3]) - max(a[2], b[2]) > 0)  # fmt: skip
        best = max(best, side)
    return best


@dataclass
class SectionPlan:
    title: str
    first_block: int
    page_start: int  # 0-based
    page_end: int  # inclusive
    from_outline: bool


def plan_sections(
    blocks: list[Block], toc: list[tuple[int, str, int]], pages: int
) -> list[SectionPlan]:
    """Step 7."""
    if not blocks:
        return []
    starts: list[tuple[int, str]] = []
    entries = _outline_entries(toc)
    if entries:
        used = -1
        for title, page in entries:
            at = _block_for_entry(blocks, title, page, after=used)
            if at is None or at <= used:
                continue
            starts.append((at, title))
            used = at
    from_outline = bool(starts)
    if from_outline:
        if starts[0][0] > 0:
            starts.insert(0, (0, ""))
    else:
        page = -1
        for i, b in enumerate(blocks):
            if b.page // PAGE_RANGE > page:
                page = b.page // PAGE_RANGE
                starts.append((i, ""))
    plans = []
    for k, (first, title) in enumerate(starts):
        last = (starts[k + 1][0] if k + 1 < len(starts) else len(blocks)) - 1
        p0 = blocks[first].page
        p1 = max(f.page for b in blocks[first : last + 1] for f in b.fragments)
        if not title:
            if from_outline:
                lead = blocks[first].text
                title = lead if len(lead) <= TITLE_MAX // 2 else "Opening pages"
            else:
                title = f"Pages {p0 + 1}–{p1 + 1}" if p1 > p0 else f"Page {p0 + 1}"
        plans.append(SectionPlan(title[:TITLE_MAX], first, p0, p1, from_outline))
    return plans


def _outline_entries(toc: list[tuple[int, str, int]]) -> list[tuple[str, int]]:
    """Top-level outline entries in page order; the next level when the top has only one (a
    book whose outline is a single root holding the chapters)."""
    if not toc:
        return []
    levels = sorted({lv for lv, _, _ in toc})
    for lv in levels[:2]:
        entries = [(t, p) for level, t, p in toc if level == lv]
        if len(entries) >= 2:
            return sorted(entries, key=lambda e: e[1])
    return []


_ENUMERATOR = re.compile(r"^(?:(?:chapter|part|section|book)\s+)?(?:\d+(?:\.\d+)*|[ivxlc]+)\s+")


def _norm_title(s: str) -> str:
    """Casefolded words; a leading section number ("2", "3.1", "IV", "Chapter 2") is dropped
    so "2 Background" in the text matches the outline's "Background" and the other way round."""
    words = " ".join(re.sub(r"[^\w\s]", " ", s.casefold()).split())
    return _ENUMERATOR.sub("", words, count=1)


def _block_for_entry(blocks: list[Block], title: str, page: int, after: int) -> int | None:
    want = _norm_title(title)
    first_on_page = None
    for i, b in enumerate(blocks):
        if i <= after:
            continue
        if b.page < page:
            continue
        if b.page > page:
            break
        if first_on_page is None:
            first_on_page = i
        if want and _norm_title(b.text[: 3 * len(title) + 20]).startswith(want):
            return i
    if first_on_page is not None:
        return first_on_page
    # Nothing starts on that page (it continues a paragraph): the next block that starts later.
    for i, b in enumerate(blocks):
        if i > after and b.page > page:
            return i
    return None
