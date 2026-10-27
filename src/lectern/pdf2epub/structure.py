"""What a PDF's pages hold beyond running text, found from the text document and the probe.

Input: the canonical text document (:mod:`lectern.pdftext.document`: words with their pages
and boxes, in reading order) and what :func:`lectern.pdf2epub.probe.scan` read from the same
PDF (styled runs, image boxes, path boxes). Output: a JSON-able ``structure`` the EPUB builder
(:mod:`lectern.pdf2epub.build`) and the review sheet use. Everything is a pure function of its
inputs, so the same PDF analyses the same way every time.

What is found, per page:

- **Inline styles**: italic, bold, small caps, and raised or lowered small type (superscripts,
  subscripts, footnote markers), as character spans of the document's words.
- **Drop caps**: a one- or two-letter word set at least twice the body size, rejoined to the
  word it begins (``T`` + ``he`` → ``The``).
- **Figures**: embedded images, and clusters of vector paths (drawings, diagrams) with the
  words inside them (labels). A caption is the line just below or above that starts with
  "Figure", "Fig.", "Plate", "Table", "Map", … .
- **Tables**: ruled grids. A grid whose every rule spans the whole table is *simple* and
  becomes an HTML table; anything else is *complex* and becomes an image of its region, its
  text as alt text.
- **Maths**: lines mostly in a maths font (Symbol, Computer Modern maths, STIX, …) or of
  maths operators, rasterized with their text as alt text.
- **Footnotes**: small type at the foot of a page, split at a leading marker (``1``, ``*``,
  ``†``), kept only when the same marker is raised in the page's body text.
- **Confidence**: how much a page's conversion can be trusted, 0–1, with the reasons; pages
  below :data:`FLAG_BELOW` are flagged and offered as page images.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass

from lectern.pdf2epub.probe import BOLD, ITALIC, MATHFONT, SMALLCAPS
from lectern.pdftext.layout import normalize_word

STRUCTURE_VERSION = 1
SUP = 16
SUB = 32
STYLE_MASK = ITALIC | BOLD | SMALLCAPS | SUP | SUB
FLAG_BELOW = 0.6

_CAPTION = re.compile(
    r"^(fig(ure)?s?\.?|plate|table|tab\.|illustration|map|chart|diagram|photo(graph)?|exhibit)"
    r"\s*([0-9]+|[ivxlc]+\b)",
    re.I,
)
_MARKER = re.compile(r"^(?:\d{1,3}|[*†‡§¶]{1,3})[.)]?$")
_BULLET = frozenset("•◦▪▫‣∙·○●■□–—-*")
_OPERATORS = frozenset("∑∏∫∮√∞≤≥≠±∓×÷∂∇∈∉⊂⊃⊆⊇∪∩∀∃→←↔⇒⇔≈≡∝∠⊥∥∘⊕⊗′″")
_COPYRIGHT = re.compile(r"©|\bcopyright\b|\bisbn\b|all rights reserved", re.I)


@dataclass
class W:
    """A word of the text document."""

    g: int  # global index in reading order
    s: int  # section
    i: int  # index in the section's words
    start: int
    end: int
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float = 0.0
    style: int = 0  # the style of most of its characters

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return max(self.y1 - self.y0, 0.01)


def doc_words(doc: dict) -> list[W]:
    out: list[W] = []
    for s, sec in enumerate(doc.get("sections") or []):
        text = sec["text"]
        for i, w in enumerate(sec.get("words") or []):
            start, end, page, x0, y0, x1, y1 = w
            out.append(W(len(out), s, i, int(start), int(end), int(page), float(x0), float(y0),
                         float(x1), float(y1), text[int(start) : int(end)]))  # fmt: skip
    return out


def block_of(doc: dict) -> list[list[int]]:
    """Per section, its blocks' start offsets (for :func:`bisect_right`)."""
    return [[int(b[0]) for b in s.get("blocks") or [[0, "p"]]] for s in doc.get("sections") or []]


def block_index(starts: list[list[int]], w: W) -> int:
    return max(0, bisect_right(starts[w.s], w.start) - 1)


# --- boxes -----------------------------------------------------------------------------------


def _inside(x: float, y: float, box, pad: float = 1.0) -> bool:
    return box[0] - pad <= x <= box[2] + pad and box[1] - pad <= y <= box[3] + pad


def _union(boxes) -> list[float]:
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes),
            max(b[3] for b in boxes)]  # fmt: skip


def _touch(a, b, pad: float) -> bool:
    return a[0] - pad <= b[2] and b[0] - pad <= a[2] and a[1] - pad <= b[3] and b[1] - pad <= a[3]


def _clusters(boxes: list[list[float]], pad: float) -> list[list[int]]:
    """Indices of boxes grouped by touching (within ``pad``), transitively."""
    parent = list(range(len(boxes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    order = sorted(range(len(boxes)), key=lambda k: boxes[k][0])
    for a_pos, a in enumerate(order):
        for b in order[a_pos + 1 :]:
            if boxes[b][0] - pad > boxes[a][2]:
                break
            if _touch(boxes[a], boxes[b], pad):
                parent[find(a)] = find(b)
    groups: dict[int, list[int]] = {}
    for k in range(len(boxes)):
        groups.setdefault(find(k), []).append(k)
    return sorted(groups.values(), key=lambda g: min(g))


def _merge_close(values: list[float], tol: float) -> list[float]:
    out: list[float] = []
    for v in sorted(values):
        if out and v - out[-1] <= tol:
            continue
        out.append(v)
    return out


# --- styles ------------------------------------------------------------------------------------


def _weighted_median(pairs: list[tuple[float, int]], default: float) -> float:
    total = sum(n for _, n in pairs)
    if not total:
        return default
    seen = 0
    for value, n in sorted(pairs):
        seen += n
        if seen * 2 >= total:
            return value
    return default


def _raised(runs: list[list]) -> list[int]:
    """Per run, SUP or SUB when it is set smaller than, and above or below, the larger type
    beside it on its line; else 0."""
    out = [0] * len(runs)
    by_x = sorted(range(len(runs)), key=lambda k: runs[k][1])
    for k, r in enumerate(runs):
        size = r[5]
        ref = None
        for j in by_x:
            o = runs[j]
            if (
                not size * 1.18 <= o[5] <= size * 2.2
                or o[1] > r[3] + 3 * o[5]
                or o[3] < r[1] - 3 * o[5]
            ):
                continue
            if min(o[4], r[4]) - max(o[2], r[2]) <= 0:
                continue  # not on the same line
            if ref is None or o[5] > ref[5]:
                ref = o
        if ref is None:
            continue
        if r[7] > ref[7] + 0.15 * ref[5]:
            out[k] = SUP
        elif r[7] < ref[7] - 0.08 * ref[5]:
            out[k] = SUB
    return out


def _word_spans(word: W, runs: list[tuple[list, int]]) -> list[list[int]]:
    """``[[offset, length, style], …]`` over the word's characters from its runs (left to
    right). Runs whose text matches the word character for character map exactly; otherwise
    the characters are shared out in proportion to each run's length."""
    n = len(word.text)
    if not n or not runs:
        return []
    texts = [normalize_word(r[0]) for r, _ in runs]
    total = sum(len(t) for t in texts) or 1
    spans: list[list[int]] = []
    pos = 0
    exact = "".join(texts) == word.text
    acc = 0
    for (r, extra), t in zip(runs, texts, strict=True):
        acc += len(t)
        stop = acc if exact else round(n * acc / total)
        stop = min(n, max(pos, stop))
        style = (int(r[6]) | extra) & (STYLE_MASK | MATHFONT)
        if stop > pos:
            if spans and spans[-1][2] == style:
                spans[-1][1] += stop - pos
            else:
                spans.append([pos, stop - pos, style])
        pos = stop
    if pos < n:
        if spans:
            spans[-1][1] += n - pos
        else:
            spans.append([0, n, 0])
    return spans


# --- lines -------------------------------------------------------------------------------------


@dataclass
class Line:
    words: list[W]

    @property
    def box(self) -> list[float]:
        return _union([(w.x0, w.y0, w.x1, w.y1) for w in self.words])

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def size(self) -> float:
        sizes = sorted(w.size for w in self.words)
        return sizes[len(sizes) // 2] if sizes else 0.0


def reading_lines(words: list[W]) -> list[Line]:
    """Lines in reading order: consecutive words on one page stay on a line while they overlap
    vertically and run left to right (so two columns never share a line)."""
    lines: list[Line] = []
    for w in words:
        if lines:
            last = lines[-1].words[-1]
            overlap = min(last.y1, w.y1) - max(last.y0, w.y0)
            if last.page == w.page and w.x0 >= last.x0 - 0.5 and overlap >= 0.4 * min(last.h, w.h):
                lines[-1].words.append(w)
                continue
        lines.append(Line([w]))
    return lines


# --- the analysis ------------------------------------------------------------------------------


def analyse(doc: dict, probe_pages: list[dict] | None) -> dict:
    """The document's structure (see the module docstring)."""
    words = doc_words(doc)
    pages = doc.get("pages") or []
    probe_by_page = {int(p["index"]): p for p in probe_pages or []}
    all_runs = [r for p in probe_by_page.values() for r in p.get("runs") or []]
    body = _weighted_median([(float(r[5]), len(r[0])) for r in all_runs], 10.0)

    by_page: dict[int, list[W]] = {}
    for w in words:
        by_page.setdefault(w.page, []).append(w)

    styles: dict[str, list[list[int]]] = {}
    for p, pw in by_page.items():
        _style_page(pw, probe_by_page.get(p), styles, body)

    starts = block_of(doc)
    dropcaps = _dropcaps(words, body, starts)
    zones: list[dict] = []
    notes: list[dict] = []
    for p in sorted(by_page):
        info = pages[p] if p < len(pages) else {}
        probe = probe_by_page.get(p) or {}
        zones += _page_zones(p, by_page[p], probe, info, body, styles)
        notes += _page_notes(p, by_page[p], zones, body, styles, info)
    for p, probe in probe_by_page.items():
        if p not in by_page:  # pictures on a page without words
            zones += _page_zones(p, [], probe, pages[p] if p < len(pages) else {}, body, styles)
    zones.sort(key=lambda z: (z["page"], -z["box"][3], z["box"][0]))
    zoned = {g for z in zones for g in z["words"] + z.get("caption", [])}
    headings: dict[str, float] = {}
    for w in words:  # each heading block's type size (heading levels), outside figures
        key = f"{w.s}:{block_index(starts, w)}"
        if key in headings or w.g in zoned:
            continue
        if doc["sections"][w.s]["blocks"][block_index(starts, w)][1] == "h":
            headings[key] = round(w.size, 2)
    ocr = isinstance(doc.get("ocr"), dict)
    per_page = [_confidence(p, pages[p], by_page.get(p, []), probe_by_page.get(p), zones, ocr)
                for p in range(len(pages))]  # fmt: skip
    return {
        "version": STRUCTURE_VERSION,
        "body_size": round(body, 2),
        "probed": probe_pages is not None,
        "styles": styles,
        "dropcaps": dropcaps,
        "headings": headings,
        "zones": zones,
        "notes": notes,
        "pages": per_page,
        "matter": _matter(doc, by_page, probe_by_page),
    }


def _style_page(pw: list[W], probe: dict | None, styles: dict, body: float) -> None:
    runs = (probe or {}).get("runs") or []
    if not runs:
        for w in pw:
            w.size = body
        return
    raised = _raised(runs)
    buckets: dict[int, list[int]] = {}
    for k, r in enumerate(runs):
        for b in range(int(r[2] // 6), int(r[4] // 6) + 1):
            buckets.setdefault(b, []).append(k)
    for w in pw:
        cand: set[int] = set()
        for b in range(int(w.y0 // 6), int(w.y1 // 6) + 1):
            cand.update(buckets.get(b, ()))
        mine = []
        for k in cand:
            r = runs[k]
            cx, cy = (r[1] + r[3]) / 2, (r[2] + r[4]) / 2
            if _inside(cx, cy, (w.x0, w.y0, w.x1, w.y1), 1.5):
                mine.append((r, raised[k]))
        mine.sort(key=lambda m: m[0][1])
        if not mine:
            w.size = body
            continue
        main = max(mine, key=lambda m: len(m[0][0]))
        w.size = float(max(m[0][5] for m in mine if not m[1]) if any(not m[1] for m in mine)
                       else main[0][5])  # fmt: skip
        spans = _word_spans(w, mine)
        if spans:
            w.style = max(spans, key=lambda s: s[1])[2]
        if any(s[2] for s in spans):
            styles[f"{w.s}:{w.i}"] = spans


def _dropcaps(words: list[W], body: float, starts: list[list[int]]) -> list[list[int]]:
    """``[[drop cap word, word it begins], …]`` (global word indices)."""
    out = []
    for d in words:
        if not (d.size >= 2.0 * body and 1 <= len(d.text) <= 2 and d.text.isalpha()):
            continue
        blk = block_index(starts, d)
        best = None
        for w in words[max(0, d.g - 80) : d.g + 80]:
            if w.g == d.g or w.page != d.page or w.s != d.s or block_index(starts, w) != blk:
                continue
            if w.x0 >= d.x1 - 2 and abs(w.y1 - d.y1) <= 1.2 * body and w.size < 1.5 * body:
                if best is None or w.g < best.g:
                    best = w
        if best is not None:
            out.append([d.g, best.g])
    return out


def _caption_lines(zone_box, lines: list[Line], body: float, prefer_above: bool) -> list[Line]:
    """The caption of a figure or table: the nearest line just below (or above, for tables)
    that reads like a caption, plus the lines of the same paragraph after it."""
    best: tuple[float, int] | None = None
    for k, ln in enumerate(lines):
        b = ln.box
        if b[2] < zone_box[0] - 2 or b[0] > zone_box[2] + 2:
            continue
        below = zone_box[1] - b[3]
        above = b[1] - zone_box[3]
        dist = below if 0 <= below <= 3 * body else above if 0 <= above <= 3 * body else None
        if dist is None:
            continue
        styled = all(w.style & ITALIC for w in ln.words) or ln.size < 0.95 * body
        if not (_CAPTION.match(ln.text) or (styled and below >= 0 and len(ln.words) <= 30)):
            continue
        if not _CAPTION.match(ln.text) and prefer_above:
            continue
        rank = dist - (2 * body if (above >= 0) == prefer_above else 0)
        if best is None or rank < best[0]:
            best = (rank, k)
    if best is None:
        return []
    k = best[1]
    out = [lines[k]]
    for ln in lines[k + 1 : k + 4]:
        prev = out[-1].box
        b = ln.box
        if b[3] > prev[1] or prev[1] - b[3] > 0.8 * (prev[3] - prev[1]) or _CAPTION.match(ln.text):
            break
        if abs(ln.size - out[0].size) > 0.1 * out[0].size:
            break
        out.append(ln)
    return out


def _page_zones(p: int, pw: list[W], probe: dict, info: dict, body: float,
                styles: dict) -> list[dict]:  # fmt: skip
    """Tables, figures, and display maths on page ``p``."""
    zones: list[dict] = []
    if not probe:
        return zones
    pw_area = max(float(info.get("w") or probe.get("w") or 1) * float(info.get("h") or
                  probe.get("h") or 1), 1.0)  # fmt: skip
    used: set[int] = set()
    lines = reading_lines(pw)

    def claim(box, pad=1.0) -> list[int]:
        got = [w.g for w in pw if w.g not in used and _inside((w.x0 + w.x1) / 2, w.cy, box, pad)]
        used.update(got)
        return got

    # Tables: ruled grids.
    paths = [list(b) for b in probe.get("paths") or []]
    thin_h = [k for k, b in enumerate(paths) if b[3] - b[1] <= 2.5 and b[2] - b[0] >= 15]
    thin_v = [k for k, b in enumerate(paths) if b[2] - b[0] <= 2.5 and b[3] - b[1] >= 8]
    rules = thin_h + thin_v
    in_table: set[int] = set()
    for group in _clusters([paths[k] for k in rules], 3.0):
        ks = [rules[i] for i in group]
        hs = [k for k in ks if k in thin_h]
        vs = [k for k in ks if k in thin_v]
        if len(hs) < 2 or len(vs) < 2:
            continue
        in_table.update(ks)
        box = _union([paths[k] for k in ks])
        xs = _merge_close([(paths[k][0] + paths[k][2]) / 2 for k in vs], 3.0)
        ys = _merge_close([(paths[k][1] + paths[k][3]) / 2 for k in hs], 3.0)
        full = all(paths[k][1] <= ys[0] + 3 and paths[k][3] >= ys[-1] - 3 for k in vs) and all(
            paths[k][0] <= xs[0] + 3 and paths[k][2] >= xs[-1] - 3 for k in hs
        )
        simple = full and len(xs) >= 2 and len(ys) >= 2 and (len(xs) - 1) * (len(ys) - 1) <= 400
        cap = _caption_lines(box, [ln for ln in lines if not any(w.g in used for w in ln.words)],
                             body, prefer_above=True)  # fmt: skip
        zone = {"id": f"t{p + 1}-{len(zones) + 1}", "page": p, "kind": "table",
                "box": [round(v, 2) for v in box], "complex": not simple}  # fmt: skip
        if simple:
            rows = []
            for r in range(len(ys) - 1, 0, -1):  # top row first
                y_top, y_bot = ys[r], ys[r - 1]
                row = []
                for c in range(len(xs) - 1):
                    cell = (xs[c], y_bot, xs[c + 1], y_top)
                    row.append(claim(cell, 0.0))
                rows.append(row)
            zone["cells"] = rows
            zone["words"] = [g for row in rows for cell in row for g in cell] + claim(box)
        else:
            zone["words"] = claim(box)
        zone["caption"] = [w.g for ln in cap for w in ln.words if w.g not in used]
        used.update(zone["caption"])
        zones.append(zone)

    # Figures: images, and clusters of drawing paths that are not table rules or lone rules.
    shapes = [b for k, b in enumerate(paths) if k not in in_table]
    shape_boxes = [_union([shapes[i] for i in g]) for g in _clusters(shapes, 8.0)]
    images = [list(b) for b in probe.get("images") or []]
    text_chars = sum(len(w.text) for w in pw)
    candidates = []
    for b in images:
        area = (b[2] - b[0]) * (b[3] - b[1])
        if b[2] - b[0] < 24 or b[3] - b[1] < 24 or area < 0.01 * pw_area:
            continue
        inside = sum(len(w.text) for w in pw if _inside((w.x0 + w.x1) / 2, w.cy, b, 0))
        if area >= 0.7 * pw_area and inside >= 40:
            continue  # a background (or a scan under its text layer), not a figure
        candidates.append((b, True))
    for b in shape_boxes:
        w_, h_ = b[2] - b[0], b[3] - b[1]
        if w_ >= 30 and h_ >= 30 and w_ * h_ >= 0.015 * pw_area:
            inside = sum(len(w.text) for w in pw if _inside((w.x0 + w.x1) / 2, w.cy, b, 0))
            if inside <= max(60, 0.25 * text_chars) or w_ * h_ < 0.5 * pw_area:
                candidates.append((b, False))
    merged = _clusters([b for b, _ in candidates], 2.0)
    for group in merged:
        box = _union([candidates[i][0] for i in group])
        if any(_touch(box, z["box"], 0) and z["kind"] == "table" for z in zones):
            continue
        photo = any(candidates[i][1] for i in group)
        words_in = claim(box)
        cap = _caption_lines(box, [ln for ln in lines if not any(w.g in used for w in ln.words)],
                             body, prefer_above=False)  # fmt: skip
        caption = [w.g for ln in cap for w in ln.words if w.g not in used]
        used.update(caption)
        zones.append({"id": f"f{p + 1}-{len(zones) + 1}", "page": p, "kind": "figure",
                      "box": [round(v, 2) for v in box], "photo": photo, "words": words_in,
                      "caption": caption})  # fmt: skip

    # Display maths: lines mostly in a maths font or of operators.
    run_of_math: list[Line] = []

    def flush_math() -> None:
        if run_of_math:
            ws = [w for ln in run_of_math for w in ln.words]
            box = _union([(w.x0, w.y0, w.x1, w.y1) for w in ws])
            used.update(w.g for w in ws)
            zones.append({"id": f"m{p + 1}-{len(zones) + 1}", "page": p, "kind": "math",
                          "box": [round(v - 2 if i < 2 else v + 2, 2) for i, v in enumerate(box)],
                          "words": [w.g for w in ws], "caption": []})  # fmt: skip
            run_of_math.clear()

    for ln in lines:
        if any(w.g in used for w in ln.words):
            flush_math()
            continue
        chars = math = 0
        for w in ln.words:
            spans = styles.get(f"{w.s}:{w.i}") or [[0, len(w.text), 0]]
            for off, n, st in spans:
                for ch in w.text[off : off + n]:
                    chars += 1
                    if st & MATHFONT or ch in _OPERATORS:
                        math += 1
        if chars >= 3 and math >= 0.5 * chars:
            run_of_math.append(ln)
        else:
            flush_math()
    flush_math()
    return zones


def _page_notes(p: int, pw: list[W], zones: list[dict], body: float, styles: dict,
                info: dict) -> list[dict]:  # fmt: skip
    """Footnotes at the foot of page ``p``, each linked to its raised marker in the body."""
    zoned = {g for z in zones if z["page"] == p for g in z["words"] + z.get("caption", [])}
    lines = [ln for ln in reading_lines(pw) if not any(w.g in zoned for w in ln.words)]
    if not lines:
        return []
    y_floor = float(info.get("y0") or 0) + 0.45 * float(info.get("h") or 0)
    foot: list[Line] = []
    for ln in sorted(lines, key=lambda ln: ln.box[1]):  # bottom first
        if ln.size <= 0.9 * body and ln.box[3] <= y_floor:
            foot.append(ln)
        else:
            break
    if not foot:
        return []
    foot.sort(key=lambda ln: -ln.box[3])  # top to bottom
    top = foot[0].box[3]
    raw: list[dict] = []
    for ln in foot:
        first = ln.words[0].text
        if _MARKER.match(first):
            raw.append({"marker": first.rstrip(".)"), "words": [w.g for w in ln.words]})
        elif raw:
            raw[-1]["words"] += [w.g for w in ln.words]
    if not raw:
        return []
    # Raised markers in the body above the notes.
    refs: list[tuple[str, int, int, int]] = []  # (marker, word, offset, length)
    for w in pw:
        if w.y0 < top or w.g in zoned:
            continue
        for off, n, st in styles.get(f"{w.s}:{w.i}") or []:
            if st & SUP:
                refs.append((w.text[off : off + n].strip(), w.g, off, n))
    out = []
    taken: set[int] = set()
    for k, note in enumerate(raw):
        for j, (m, g, off, n) in enumerate(refs):
            if j not in taken and m == note["marker"]:
                taken.add(j)
                out.append({"id": f"n{p + 1}-{k + 1}", "page": p, "marker": note["marker"],
                            "words": note["words"], "ref": [g, off, n]})  # fmt: skip
                break
    return out


def _confidence(
    p: int, info: dict, pw: list[W], probe: dict | None, zones: list[dict], ocr: bool = False
) -> dict:
    """``{"page", "confidence", "reasons", "figures", "tables", "maths", "content"}``."""
    mine = [z for z in zones if z["page"] == p]
    pictures = sum((z["box"][2] - z["box"][0]) * (z["box"][3] - z["box"][1])
                   for z in mine if z["kind"] == "figure")  # fmt: skip
    area = max(float(info.get("w") or 1) * float(info.get("h") or 1), 1.0)
    images = (probe or {}).get("images") or []
    reasons: list[str] = []
    conf = 1.0
    has_text = bool(info.get("text"))
    content = bool(pw or images or (probe or {}).get("paths"))
    if not has_text:
        if images or pictures:
            conf, reasons = 0.2, ["little or no text: mostly pictures"]
        elif pw:
            conf, reasons = 0.4, ["very little text"]
        else:
            conf, reasons = 1.0, ["blank page"]
    else:
        chars = int((probe or {}).get("chars") or 0)
        errors = int((probe or {}).get("map_errors") or 0)
        if chars and errors / chars > 0.02:
            conf -= min(0.6, 2 * errors / chars)
            reasons.append("characters without a Unicode mapping")
        text = "".join(w.text for w in pw)
        odd = sum(1 for ch in text if ch == "\ufffd" or unicodedata.category(ch) in ("Co", "So"))
        if text and odd / len(text) > 0.02:
            conf -= min(0.5, 3 * odd / len(text))
            reasons.append("unusual or unreadable characters")
        if pw and sum(len(w.text) for w in pw) / len(pw) < 2.2:
            conf -= 0.4
            reasons.append("letters spaced apart or broken words")
        if any(z["kind"] == "table" and z.get("complex") for z in mine):
            conf -= 0.15
            reasons.append("a complex table (kept as an image)")
        if any(z["kind"] == "math" for z in mine):
            conf -= 0.1
            reasons.append("maths (kept as images)")
        if pictures / area > 0.5:
            conf -= 0.2
            reasons.append("mostly pictures")
        if ocr:  # recognised, not read: never as sure as a text layer
            conf -= 0.15
            reasons.append("text from OCR")
    conf = round(max(0.0, min(1.0, conf)), 2)
    return {
        "page": p + 1,
        "confidence": conf,
        "flagged": content and conf < FLAG_BELOW,
        "reasons": reasons,
        "figures": sum(1 for z in mine if z["kind"] == "figure"),
        "tables": sum(1 for z in mine if z["kind"] == "table"),
        "maths": sum(1 for z in mine if z["kind"] == "math"),
        "content": content,
    }


def _matter(doc: dict, by_page: dict[int, list[W]], probe_by_page: dict) -> list[dict]:
    """Front and back matter worth a decision: a picture cover, copyright pages, an index."""
    pages = doc.get("pages") or []
    out = []
    first = probe_by_page.get(0) or {}
    if pages and first.get("images"):
        info = pages[0]
        area = max(float(info.get("w") or 1) * float(info.get("h") or 1), 1.0)
        big = max((b[2] - b[0]) * (b[3] - b[1]) for b in first["images"])
        if big >= 0.5 * area and len(by_page.get(0, [])) < 60:
            out.append({"page": 1, "kind": "cover"})
    for p in range(min(8, len(pages))):
        text = " ".join(w.text for w in by_page.get(p, []))
        if text and _COPYRIGHT.search(text) and len(by_page.get(p, [])) < 400:
            out.append({"page": p + 1, "kind": "copyright"})
    for sec in doc.get("sections") or []:
        if re.fullmatch(r"(general\s+)?index", (sec.get("title") or "").strip(), re.I):
            for p in range(int(sec["page_start"]), int(sec["page_end"]) + 1):
                out.append({"page": p + 1, "kind": "index"})
    return out
