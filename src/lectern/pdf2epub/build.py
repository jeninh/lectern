"""Build a reflowable EPUB 3 from a PDF's canonical text document.

The input is the document :func:`lectern.pdftext.document.build_document` stored next to the
PDF: its sections are the chapters (PDF outline, else detected headings), its blocks are
paragraphs and headings, its word map gives every character's page, and its page labels are
the PDF's ``/PageLabels``. Optionally (issue #63) also the page structure
:func:`lectern.pdf2epub.structure.analyse` found (styles, figures, tables, maths, footnotes,
drop caps, page confidence), the confirmed :class:`~lectern.pdf2epub.rules.Rules`, and the
images the sandboxed renderer made (:func:`regions` lists what to render). Everything here is
a pure function of those inputs, and the zip is deterministic (fixed timestamps and order), so
the same PDF and the same inputs convert to the same bytes.

Page numbers are metadata, not text (issue #28): printed page numbers and running
headers/footers are already stripped from the text flow by :mod:`lectern.pdftext.layout`, so
this module only re-attaches the pagination as ``epub:type="pagebreak"`` markers — one empty
``span`` before the first thing each page contributes — plus a ``page-list`` nav,
``dc:source``, and ``pageBreakSource``.

What a block of text becomes:

- a heading (``h2``–``h4`` by type size), or a paragraph with its inline styles (``i``,
  ``b``, small caps, ``sup``/``sub``) and a drop cap rejoined to its word;
- a bulleted or numbered **list** when its lines start with bullets or enumerators;
- **verse** (``p class="verse"`` with the line breaks kept) when its lines end short of the
  measure and mostly at punctuation, or on pages the rules mark as verse;
- a **block quote** when every line is indented on both sides;
- figures, tables, and maths (:mod:`lectern.pdf2epub.structure`'s zones) are placed before
  the first text below them (or after the paragraph that runs past them), as ``figure`` with
  an image and ``figcaption``, an HTML ``table``, or an image of the region with its text as
  ``alt``; footnotes become ``noteref`` links and ``aside epub:type="footnote"`` at the
  chapter's end (or right after their paragraph);
- a page the rules keep as an image replaces all of that page's text.

A scanned PDF converts once OCR has given it a text document (issue #62: the document then
carries ``"ocr": {"engine", "language"}``). Its edition says so everywhere a person or a
reader looks: the package description and a ``lectern:source`` meta, a note in the
navigation document, and the report (``source: "ocr"`` and a warning to expect recognition
errors).
"""

from __future__ import annotations

import io
import re
import zipfile
from bisect import bisect_left
from xml.sax.saxutils import escape, quoteattr

from lectern.pdf2epub.probe import BOLD, ITALIC, SMALLCAPS
from lectern.pdf2epub.rules import Rules
from lectern.pdf2epub.structure import SUB, SUP, W, doc_words, reading_lines

MODIFIED = "1980-01-01T00:00:00Z"  # deterministic, like the zip timestamps
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
CONVERTED_NAME = "book-converted-from-pdf.epub"
REPORT_NAME = "pdf2epub-report.json"
NAV_NAME = "nav.xhtml"
CSS_NAME = "style.css"

# Characters XML 1.0 forbids outright (PDF-sourced text and /PageLabels can carry them).
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TERMINAL = re.compile(r"[.!?…:;][\"'”’»)\]]*$")
_LINE_END_PUNCT = re.compile(r"[,;:.!?…—–][\"'”’»)\]]*$")
_BULLETS = frozenset("•◦▪▫‣∙○●■□–—-*")
_ENUM = re.compile(r"^\(?(\d{1,3}|[a-z])[.)]$")
ALT_MAX = 1500


def _xml_text(text: str) -> str:
    """PDF-sourced text made XML-safe: control characters dropped, the rest escaped."""
    return escape(_XML_BAD.sub("", text))


def _attr(text: str) -> str:
    return quoteattr(_XML_BAD.sub("", text))


XHTML_HEAD = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    "<!DOCTYPE html>\n"
    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"'
    ' xml:lang="{lang}" lang="{lang}">\n<head>\n<title>{title}</title>\n'
    '<link rel="stylesheet" type="text/css" href="style.css"/>\n</head>\n<body>\n'
)

CSS = b"""\
.sc { font-variant: small-caps; }
.dropcap { font-weight: bold; }
p.verse { margin-left: 2em; text-indent: 0; }
blockquote { margin: 1em 2em; }
figure { margin: 1em 0; text-align: center; }
figure img, div.math img { max-width: 100%; }
figure.page-image img { max-height: 95vh; }
figcaption, p.caption { font-size: 0.9em; font-style: italic; }
table { border-collapse: collapse; margin: 1em auto; }
th, td { border: 1px solid; padding: 0.2em 0.5em; }
div.math { text-align: center; margin: 1em 0; }
aside { font-size: 0.9em; }
"""

PDF2EPUB = "pdf2epub"  # the job kind (lectern.pdf2epub.jobs registers it)


class ConversionError(ValueError):
    """The document cannot be converted (``needs_ocr``, no text)."""


def page_labels(doc: dict) -> list[str]:
    """Every page's label: the PDF's own ``/PageLabels`` (roman numerals, offsets), else the
    1-based physical page number."""
    out = []
    for i, p in enumerate(doc.get("pages") or []):
        label = (p.get("label") or "").strip()
        out.append(label[:32] if label else str(i + 1))
    return out


def _labels(doc: dict, rules: Rules) -> list[str]:
    labels = page_labels(doc)
    off = rules.page_numbers.offset
    return [str(int(lb) + off) if off and lb.isdigit() else lb for lb in labels]


def zone_name(z: dict) -> str:
    return f"{z['id']}.{'jpg' if z.get('photo') else 'png'}"


def page_name(page: int) -> str:
    return f"page-{page + 1}.jpg"


def _zone_mode(z: dict, rules: Rules) -> str | None:
    """How a zone is converted: ``image``, ``table`` (HTML), ``drop`` (the picture left out,
    its caption kept), or None (its words stay ordinary text)."""
    if z["kind"] == "figure":
        return "image" if rules.figures == "keep" else "drop"
    if z["kind"] == "table":
        if rules.tables == "text":
            return None
        return "table" if rules.tables == "auto" and not z.get("complex") else "image"
    if z["kind"] == "math":
        return "image" if rules.maths == "image" else None
    return None


def regions(doc: dict, structure: dict | None, rules: Rules) -> list[list]:
    """What the renderer must draw for this conversion: ``[page, x0, y0, x1, y1, name,
    kind]`` per zone kept as an image and per page kept whole (an empty box)."""
    if not structure:
        return []
    npages = len(doc.get("pages") or [])
    drop = {p - 1 for p in rules.drop_pages if p <= npages}
    whole = sorted({p - 1 for p in rules.page_images if p <= npages} - drop)
    out: list[list] = [[p, 0, 0, 0, 0, page_name(p), "page"] for p in whole]
    for z in structure.get("zones") or []:
        if z["page"] in drop or z["page"] in whole or _zone_mode(z, rules) != "image":
            continue
        out.append([z["page"], *z["box"], zone_name(z), z["kind"]])
    return out


# --- the converter ------------------------------------------------------------------------------


class _Converter:
    def __init__(self, doc: dict, structure: dict | None, rules: Rules,
                 available: set[str] | None, preview: bool) -> None:  # fmt: skip
        self.doc, self.rules, self.preview = doc, rules, preview
        st = structure or {}
        self.sections = doc.get("sections") or []
        self.words = doc_words(doc)
        self.labels = _labels(doc, rules)
        self.body = float(st.get("body_size") or 10.0)
        self.styles = (st.get("styles") or {}) if rules.styles else {}
        have = (lambda _n: True) if preview else (lambda n: n in (available or ()))
        npages = len(doc.get("pages") or [])
        self.drop = {p - 1 for p in rules.drop_pages if p <= npages}
        self.page_img = {p - 1 for p in rules.page_images if p <= npages and
                         have(page_name(p - 1))} - self.drop  # fmt: skip
        self.verse_pages = {p - 1 for p in rules.verse_pages}
        gone = self.drop | self.page_img

        self.zones: list[tuple[dict, str]] = []
        for z in st.get("zones") or []:
            if z["page"] in gone:
                continue
            mode = _zone_mode(z, rules)
            if mode == "image" and not have(zone_name(z)):
                mode = "drop" if z["kind"] == "figure" and not z["words"] else None
            if mode is not None:
                self.zones.append((z, mode))

        suppressed: set[int] = {w.g for w in self.words if w.page in gone}
        for z, _mode in self.zones:
            suppressed.update(z["words"])
            suppressed.update(z.get("caption") or [])
        self.refs: dict[int, list] = {}
        if rules.footnotes != "inline":
            for n in st.get("notes") or []:
                g = n["ref"][0]
                if n["page"] in gone or g in suppressed:
                    continue
                suppressed.update(n["words"])
                self.refs.setdefault(g, []).append(n)
        self.drops: dict[int, str] = {}
        for d, target in st.get("dropcaps") or []:
            if d not in suppressed and target not in suppressed:
                self.drops[target] = self.words[d].text
                suppressed.add(d)
        self.suppressed = suppressed

        sizes = sorted({float(v) for v in (st.get("headings") or {}).values()}, reverse=True)
        self.heading_size = st.get("headings") or {}
        self.levels = {s: 2 + min(2, k) for k, s in enumerate(sizes)}

        # Where every zone and kept page goes: ("before" | "after", word) in reading order.
        self.before: dict[int, list] = {}
        self.after: dict[int, list] = {}
        self.orphans: list = []
        visible = [w for w in self.words if w.g not in suppressed]
        self._lines: dict[int, list] = {}
        for z, mode in self.zones:
            self._place((z, mode), self._zone_anchor(z, visible))
        for p in sorted(self.page_img):
            first = next((w for w in self.words if w.page == p), None)
            item = ({"id": f"pg{p + 1}", "page": p, "kind": "page", "box": [0, 0, 0, 0],
                     "words": []}, "page")  # fmt: skip
            if first is not None:
                self._place(item, ("before", first.g))
            else:
                self._place(item, self._zone_anchor(item[0], visible))
        self.cols = self._columns(visible)

        rules_by = {c.index: c for c in rules.chapters}
        self.groups: list[list[int]] = []
        self.titles: list[str] = []
        self.skip_title_block: set[int] = set()
        for k, sec in enumerate(self.sections):
            rule = rules_by.get(k)
            if rule and rule.merge and self.groups:
                self.groups[-1].append(k)
                continue
            self.groups.append([k])
            title = (rule.title if rule and rule.title else None) or sec.get("title") or ""
            self.titles.append(title or f"Section {len(self.groups)}")

        self.claimed: set[int] = set()
        self.page_list: list[tuple[str, str, str]] = []
        self.items: list[tuple[int, str]] = []
        self.stats = {"figures": 0, "tables": 0, "table_images": 0, "maths": 0, "footnotes": 0,
                      "verse": 0, "lists": 0, "quotes": 0, "dropcaps": 0, "page_images": 0}  # fmt: skip

    # --- placement -------------------------------------------------------------------------

    def _zone_anchor(self, z: dict, visible: list[W]) -> tuple[str, int] | None:
        p, box = z["page"], z["box"]
        if p not in self._lines:
            self._lines[p] = reading_lines([w for w in visible if w.page == p])
        for ln in self._lines[p]:  # the first line below it, in reading order
            b = ln.box
            if b[3] <= box[1] + 2 and (not any(box) or (b[2] > box[0] and b[0] < box[2])):
                return ("before", ln.words[0].g)
        last = None
        for w in visible:
            if w.page <= p:
                last = w
        if last is not None:
            return ("after", last.g)
        return ("before", visible[0].g) if visible else None

    def _place(self, item, anchor) -> None:
        if anchor is None:
            self.orphans.append(item)
        else:
            (self.before if anchor[0] == "before" else self.after).setdefault(anchor[1], []).append(
                item
            )

    def _columns(self, visible: list[W]) -> dict[int, tuple[float, float]]:
        by_page: dict[int, list[W]] = {}
        for w in visible:
            by_page.setdefault(w.page, []).append(w)
        out = {}
        for p, ws in by_page.items():
            lines = reading_lines(ws)
            full = [ln for ln in lines if len(ln.words) >= 5] or lines
            out[p] = (min(ln.box[0] for ln in full), max(ln.box[2] for ln in full))
        return out

    # --- pages -----------------------------------------------------------------------------

    def marker(self, page: int, file: str) -> str:
        if not self.rules.page_numbers.keep or page in self.claimed or page in self.drop:
            return ""
        self.claimed.add(page)
        self.page_list.append((file, f"page-{page + 1}", self.labels[page]))
        return (
            f'<span epub:type="pagebreak" id="page-{page + 1}" title={_attr(self.labels[page])}/>'
        )

    # --- inline text -----------------------------------------------------------------------

    def _word_segments(self, w: W, heading: bool, file: str, notes: list) -> list[tuple[int, str]]:
        n = len(w.text)
        styles = [0] * n
        for off, length, st in self.styles.get(f"{w.s}:{w.i}") or []:
            for k in range(off, min(n, off + length)):
                styles[k] = st & (ITALIC | BOLD | SMALLCAPS | SUP | SUB)
        if heading:
            styles = [s & ~BOLD for s in styles]
        refs = {r["ref"][1]: r for r in self.refs.get(w.g, [])}
        segs: list[tuple[int, str]] = []
        if w.g in self.drops:
            segs.append((0, f'<span class="dropcap">{_xml_text(self.drops[w.g])}</span>'))
            self.stats["dropcaps"] += 1
        k = 0
        while k < n:
            note = refs.get(k)
            if note is not None:
                notes.append(note)
                self.stats["footnotes"] += 1
                segs.append((
                    styles[k] & ~(SUP | SUB),
                    f'<sup><a epub:type="noteref" href="#{note["id"]}" id="r{note["id"]}">'
                    f"{_xml_text(w.text[k : k + note['ref'][2]])}</a></sup>",
                ))  # fmt: skip
                k += max(1, note["ref"][2])
                continue
            j = k
            while j < n and styles[j] == styles[k] and j not in refs:
                j += 1
            segs.append((styles[k], _xml_text(w.text[k:j])))
            k = j
        return segs

    def inline(self, words: list[W], file: str, notes: list, heading: bool = False) -> str:
        segs: list[tuple[int, str]] = []
        prev: W | None = None
        for w in words:
            mine = self._word_segments(w, heading, file, notes)
            if prev is not None:
                gap = (
                    self.sections[w.s]["text"][prev.end : w.start]
                    if w.g == prev.g + 1 and w.s == prev.s
                    else " "
                )
                if gap:
                    left = segs[-1][0] if segs else 0
                    right = mine[0][0] if mine else 0
                    segs.append((left & right, _xml_text(gap)))
            mark = self.marker(w.page, file)
            if mark:
                segs.append((mine[0][0] if mine else 0, mark))
            segs += mine
            prev = w
        out: list[str] = []
        k = 0
        while k < len(segs):
            j = k
            while j < len(segs) and segs[j][0] == segs[k][0]:
                j += 1
            out.append(_wrap(segs[k][0], "".join(s for _, s in segs[k:j])))
            k = j
        return "".join(out)

    # --- blocks ----------------------------------------------------------------------------

    def _classify(self, seg: list[W]) -> tuple[str, list]:
        """``("list", items)``, ``("verse", lines)``, ``("quote", lines)``, or ``("p", [])``."""
        lines = reading_lines(seg)
        first = lines[0].words[0].text if lines else ""
        if len(lines) >= 2 and (first in _BULLETS or _ENUM.match(first)):
            items: list[list[W]] = []
            numbered = bool(_ENUM.match(first))
            for ln in lines:
                lead = ln.words[0].text
                if lead in _BULLETS or (numbered and _ENUM.match(lead)):
                    items.append(list(ln.words[1:]))
                else:
                    items[-1] += ln.words
            if len(items) >= 2 and all(items):
                return ("ol" if numbered else "ul", items)
        page = seg[0].page
        left, right = self.cols.get(page, (0.0, 0.0))
        forced = any(w.page in self.verse_pages for w in seg) and len(lines) >= 2
        if forced or (self.rules.verse == "auto" and len(lines) >= 3):
            fits = punct = 0
            for ln, nxt in zip(lines, lines[1:], strict=False):
                w0 = nxt.words[0]
                char = (w0.x1 - w0.x0) / max(1, len(w0.text))
                if ln.box[2] + char + (w0.x1 - w0.x0) <= right + 1:
                    fits += 1
                if _LINE_END_PUNCT.search(ln.words[-1].text):
                    punct += 1
            n = len(lines) - 1
            if forced or (fits >= 0.75 * n and punct >= 0.5 * n):
                return ("verse", lines)
        if (
            len(lines) >= 2
            and right > left
            and all(ln.box[0] >= left + 1.5 * self.body for ln in lines)
            and all(ln.box[2] <= right - 1.5 * self.body for ln in lines)
        ):
            return ("quote", lines)
        return ("p", [])

    def text_block(self, seg: list[W], kind: str, key: str, file: str, notes: list) -> str:
        if not seg:
            return ""
        if kind == "h":
            size = self.heading_size.get(key)
            level = self.levels.get(float(size), 2) if size is not None else 2
            return f"<h{level}>{self.inline(seg, file, notes, heading=True)}</h{level}>"
        shape, parts = self._classify(seg)
        if shape in ("ul", "ol"):
            self.stats["lists"] += 1
            items = "".join(f"<li>{self.inline(i, file, notes)}</li>" for i in parts)
            first = self.marker(seg[0].page, file)  # a bullet word may hold the page's start
            return f"{first}<{shape}>{items}</{shape}>"
        if shape == "verse":
            self.stats["verse"] += 1
            return '<p class="verse">' + "<br/>".join(
                self.inline(ln.words, file, notes) for ln in parts
            ) + "</p>"  # fmt: skip
        if shape == "quote":
            self.stats["quotes"] += 1
            return f"<blockquote><p>{self.inline(seg, file, notes)}</p></blockquote>"
        return f"<p>{self.inline(seg, file, notes)}</p>"

    def _img(self, name: str, alt: str) -> str:
        alt = " ".join(alt.split())[:ALT_MAX]
        if self.preview:
            return f'<span class="ph">{_xml_text(alt or "image")}</span>'
        return f'<img src="images/{name}" alt={_attr(alt)}/>'

    def _words_text(self, gs: list[int]) -> str:
        return " ".join(self.words[g].text for g in gs)

    def zone(self, z: dict, mode: str, file: str, notes: list) -> str:
        mark = self.marker(z["page"], file)
        caption = [self.words[g] for g in z.get("caption") or []]
        cap_html = self.inline(caption, file, notes) if caption else ""
        cap_text = self._words_text(z.get("caption") or [])
        zid = z["id"]
        if mode == "page":
            self.stats["page_images"] += 1
            p = z["page"]
            text = " ".join(w.text for w in self.words if w.page == p)
            alt = f"Page {self.labels[p]}" + (f": {text}" if text else "")
            return (f'<figure class="page-image" id={_attr(zid)}>{mark}'
                    f"{self._img(page_name(p), alt)}</figure>")  # fmt: skip
        if mode == "drop":
            return f'{mark}<p class="caption">{cap_html}</p>' if cap_html else mark
        if mode == "table":
            self.stats["tables"] += 1
            rows = z.get("cells") or []
            body = []
            for r, row in enumerate(rows):
                cells = [[self.words[g] for g in cell] for cell in row]
                head = r == 0 and all(all(self._bold(w) for w in c) for c in cells if c)
                tag = "th" if head else "td"
                html = "".join(f"<{tag}>{self.inline(c, file, notes, heading=head)}</{tag}>"
                               for c in cells)  # fmt: skip
                body.append((head, f"<tr>{html}</tr>"))
            thead = "".join(h for is_head, h in body if is_head)
            tbody = "".join(h for is_head, h in body if not is_head)
            return (
                f"{mark}<table id={_attr(zid)}>"
                + (f"<caption>{cap_html}</caption>" if cap_html else "")
                + (f"<thead>{thead}</thead>" if thead else "")
                + f"<tbody>{tbody}</tbody></table>"
            )
        alt_words = self._words_text(z["words"])
        if z["kind"] == "math":
            self.stats["maths"] += 1
            return f'<div class="math" id={_attr(zid)}>{mark}{self._img(zone_name(z), alt_words)}</div>'
        if z["kind"] == "table":
            self.stats["table_images"] += 1
        else:
            self.stats["figures"] += 1
        alt = alt_words or cap_text or ("Table" if z["kind"] == "table" else "Figure")
        if z["kind"] == "table" and cap_text:
            alt = f"{cap_text} {alt_words}".strip()
        fig = f"<figcaption>{cap_html}</figcaption>" if cap_html else ""
        return f"<figure id={_attr(zid)}>{mark}{self._img(zone_name(z), alt)}{fig}</figure>"

    def _bold(self, w: W) -> bool:
        spans = self.styles.get(f"{w.s}:{w.i}") or []
        return bool(spans) and all(s[2] & BOLD for s in spans)

    def aside(self, note: dict, file: str) -> str:
        words = [self.words[g] for g in note["words"][1:]]
        notes: list = []
        return (
            f'<aside epub:type="footnote" id={_attr(note["id"])}><p>'
            f'<a href="#r{note["id"]}">{_xml_text(note["marker"])}</a> '
            f"{self.inline(words, file, notes)}</p></aside>"
        )

    # --- sections --------------------------------------------------------------------------

    def group(self, gi: int, lang: str) -> bytes:
        file = f"s{gi + 1}.xhtml"
        members = self.groups[gi]
        title = self.titles[gi]
        out: list[tuple[int, str]] = []
        end_notes: list = []

        def emit(page: int, html: str) -> None:
            if html:
                out.append((page, html))

        if gi == 0:
            for item in self.orphans:
                emit(item[0]["page"], self.zone(*item, file, end_notes))
        for k in members:
            sec = self.sections[k]
            text = sec["text"]
            blocks = sec.get("blocks") or [[0, "p"]]
            ends = [int(b[0]) for b in blocks[1:]] + [len(text)]
            sec_words = [w for w in self.words if w.s == k]
            starts = [w.start for w in sec_words]
            first_start, first_stop = int(blocks[0][0]), ends[0]
            title_block = (
                blocks[0][1] == "h"
                and text[first_start:first_stop].strip() == (sec.get("title") or "").strip()
            )
            renamed = k == members[0] and title != (sec.get("title") or f"Section {gi + 1}")
            if k == members[0] and (renamed or not title_block):
                emit(sec_words[0].page if sec_words else 0,
                     f"<h{2 if title_block else 1}>{_xml_text(title)}</h{2 if title_block else 1}>")  # fmt: skip
            for bi, (b, end) in enumerate(zip(blocks, ends, strict=True)):
                lo, hi = bisect_left(starts, int(b[0])), bisect_left(starts, end)
                bwords = sec_words[lo:hi]
                if bi == 0 and title_block and renamed and k == members[0]:
                    bwords = [w for w in bwords if self.before.get(w.g) or self.after.get(w.g)]
                    bwords_visible = False
                else:
                    bwords_visible = True
                notes: list = []
                seg: list[W] = []
                deferred: list = []
                key = f"{k}:{bi}"
                for w in bwords:
                    here = self.before.get(w.g) or []
                    if here:
                        force = any(m == "page" for _z, m in here)
                        if not seg or force or _TERMINAL.search(seg[-1].text):
                            if seg:
                                emit(seg[0].page, self.text_block(seg, b[1], key, file, notes))
                                seg = []
                            for item in here:
                                emit(item[0]["page"], self.zone(*item, file, notes))
                        else:
                            deferred += here
                    if bwords_visible and w.g not in self.suppressed:
                        seg.append(w)
                if seg:
                    emit(seg[0].page, self.text_block(seg, b[1], key, file, notes))
                for item in deferred:
                    emit(item[0]["page"], self.zone(*item, file, notes))
                for w in bwords:
                    for item in self.after.get(w.g) or []:
                        emit(item[0]["page"], self.zone(*item, file, notes))
                if self.rules.footnotes == "popup":
                    for note in notes:
                        emit(note["page"], self.aside(note, file))
                else:
                    end_notes += notes
        for note in end_notes:
            emit(note["page"], self.aside(note, file))
        self.items += out
        body = "\n".join(html for _p, html in out)
        return (
            XHTML_HEAD.format(lang=escape(lang), title=_xml_text(title))
            + body
            + "\n</body>\n</html>\n"
        ).encode()


def _wrap(style: int, html: str) -> str:
    for bit, (o, c) in (
        (SUB, ("<sub>", "</sub>")),
        (SUP, ("<sup>", "</sup>")),
        (SMALLCAPS, ('<span class="sc">', "</span>")),
        (ITALIC, ("<i>", "</i>")),
        (BOLD, ("<b>", "</b>")),
    ):
        if style & bit:
            html = o + html + c
    return html


# --- the edition --------------------------------------------------------------------------------


def ocr_of(doc: dict) -> dict | None:
    """``{"engine", "language"}`` when the document's text was recognised by OCR, else None."""
    ocr = doc.get("ocr")
    if not isinstance(ocr, dict):
        return None
    return {"engine": str(ocr.get("engine") or "OCR"), "language": str(ocr.get("language") or "")}


def ocr_note(ocr: dict) -> str:
    """The one line an OCR-generated edition carries wherever it describes itself."""
    lang = f", language {ocr['language']}" if ocr["language"] else ""
    return (
        f"Generated from text recognition (OCR: {ocr['engine']}{lang}) of a scanned PDF; "
        "expect recognition errors."
    )


def _check(doc: dict) -> None:
    if doc.get("status") != "ready":
        raise ConversionError("needs_ocr")
    if not doc.get("sections") or not doc.get("pages"):
        raise ConversionError("no_text")


def build_epub(
    doc: dict,
    *,
    title: str,
    author: str,
    language: str | None,
    source_name: str,
    pdf_sha256: str,
    isbn: str | None = None,
    cover_jpeg: bytes | None = None,
    structure: dict | None = None,
    rules: Rules | None = None,
    images: dict[str, bytes] | None = None,
) -> tuple[bytes, dict]:
    """The converted EPUB's bytes and its conversion report. Raises :class:`ConversionError`
    when the document has nothing to convert. ``images`` are the rendered :func:`regions`;
    a zone or page whose image is missing falls back to its text."""
    _check(doc)
    rules = rules or Rules()
    images = images or {}
    conv = _Converter(doc, structure, rules, set(images), preview=False)
    lang = language or "en"

    files: list[tuple[str, bytes]] = [(CSS_NAME, CSS)]
    manifest: list[tuple[str, str, str, str]] = [("css", CSS_NAME, "text/css", "")]
    spine = []
    for gi in range(len(conv.groups)):
        name = f"s{gi + 1}.xhtml"
        files.append((name, conv.group(gi, lang)))
        manifest.append((f"s{gi + 1}", name, "application/xhtml+xml", ""))
        spine.append(f"s{gi + 1}")
    used = sorted({m for m in re.findall(rb'src="images/([^"]+)"', b"".join(d for _n, d in files))})
    for raw in used:
        name = raw.decode()
        files.append((f"images/{name}", images[name]))
        media = "image/jpeg" if name.endswith(".jpg") else "image/png"
        manifest.append((f"img-{name.rsplit('.', 1)[0]}", f"images/{name}", media, ""))

    ocr = ocr_of(doc)
    files.append((NAV_NAME, _nav_doc(title, lang, conv.titles, conv.page_list, ocr)))
    manifest.append(("nav", NAV_NAME, "application/xhtml+xml", "nav"))
    # The nav document is not in the spine: it is a navigation aid, not content, and it must
    # not join the text timeline (sentences, narration, the reader's spine).

    if cover_jpeg:
        files.append(("cover.jpg", cover_jpeg))
        manifest.append(("cover-image", "cover.jpg", "image/jpeg", "cover-image"))

    files.insert(0, ("content.opf", _opf(
        title=title, author=author, language=lang, source_name=source_name,
        pdf_sha256=pdf_sha256, isbn=isbn, manifest=manifest, spine=spine,
        has_cover=bool(cover_jpeg), page_numbers=rules.page_numbers.keep, ocr=ocr,
    )))  # fmt: skip
    return _zip(files), _report(doc, conv, structure, rules)


def preview(doc: dict, structure: dict | None, rules: Rules, pages: list[int]) -> list[dict]:
    """The converted text of ``pages`` (1-based) under ``rules``, as XHTML fragments (images
    shown as their alt text), for the side-by-side review."""
    _check(doc)
    conv = _Converter(doc, structure, rules, None, preview=True)
    for gi in range(len(conv.groups)):
        conv.group(gi, "en")
    want = {p - 1 for p in pages}
    by_page: dict[int, list[str]] = {p: [] for p in sorted(want)}
    for page, html in conv.items:
        if page in want:
            by_page[page].append(html)
    st_pages = (structure or {}).get("pages") or []
    out = []
    for p, parts in by_page.items():
        if not 0 <= p < len(conv.labels):
            continue
        info = st_pages[p] if p < len(st_pages) else {}
        out.append({
            "page": p + 1,
            "label": conv.labels[p],
            "confidence": info.get("confidence"),
            "reasons": info.get("reasons") or [],
            "as_image": p in conv.page_img,
            "dropped": p in conv.drop,
            "html": "\n".join(parts),
        })  # fmt: skip
    return out


def _nav_doc(
    title: str,
    lang: str,
    titles: list[str],
    page_list: list[tuple[str, str, str]],
    ocr: dict | None = None,
) -> bytes:
    head = XHTML_HEAD.format(lang=escape(lang), title=_xml_text(f"{title} — Contents"))
    lines = [f'<nav epub:type="toc" id="toc"><h1>{_xml_text(title)}</h1><ol>']
    for i, t in enumerate(titles):
        lines.append(f'<li><a href="s{i + 1}.xhtml">{_xml_text(t)}</a></li>')
    lines.append("</ol></nav>")
    if page_list:
        lines.append('<nav epub:type="page-list" id="page-list" hidden="hidden"><ol>')
        lines += [
            f'<li><a href="{escape(href)}#{pid}">{_xml_text(label)}</a></li>'
            for href, pid, label in page_list
        ]
        lines.append("</ol></nav>")
    if ocr is not None:
        lines.append(f'<p class="lectern-ocr">{_xml_text(ocr_note(ocr))}</p>')
    return (head + "\n".join(lines) + "\n</body>\n</html>\n").encode()


def _opf(
    *,
    title: str,
    author: str,
    language: str,
    source_name: str,
    pdf_sha256: str,
    isbn: str | None,
    manifest: list[tuple[str, str, str, str]],
    spine: list[str],
    has_cover: bool,
    page_numbers: bool = True,
    ocr: dict | None = None,
) -> bytes:
    if ocr is None:
        description = (
            f"EPUB edition converted from the PDF file {_xml_text(source_name)} "
            "by Lectern; the original PDF is kept."
        )
    else:
        description = (
            f"EPUB edition converted by Lectern from the scanned PDF file {_xml_text(source_name)}. "
            f"{_xml_text(ocr_note(ocr))} The original PDF is kept."
        )
    metadata = [
        f'<dc:identifier id="uid">urn:sha256:{pdf_sha256}</dc:identifier>',
        f"<dc:title>{_xml_text(title)}</dc:title>",
        f"<dc:creator>{_xml_text(author)}</dc:creator>",
        f"<dc:language>{escape(language)}</dc:language>",
        f"<dc:description>{description}</dc:description>",
        f"<dc:source>{_xml_text(source_name)}</dc:source>",
        f'<meta property="dcterms:modified">{MODIFIED}</meta>',
    ]
    if page_numbers:
        # Where the page numbers come from: this PDF's pagination.
        metadata.insert(-1, f'<meta property="pageBreakSource">urn:sha256:{pdf_sha256}</meta>')
    if isbn:
        metadata.insert(1, f'<dc:identifier id="isbn">urn:isbn:{_xml_text(isbn)}</dc:identifier>')
    if has_cover:
        metadata.append('<meta name="cover" content="cover-image"/>')
    if ocr is not None:
        metadata.append('<meta name="lectern:source" content="ocr"/>')
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">',
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">',
        *metadata,
        "</metadata>",
        "<manifest>",
    ]
    for ident, href, media, props in manifest:
        p = f" properties={quoteattr(props)}" if props else ""
        lines.append(
            f"<item id={quoteattr(ident)} href={quoteattr(href)} media-type={quoteattr(media)}{p}/>"
        )
    lines += ["</manifest>", "<spine>"]
    for ident in spine:
        lines.append(f"<itemref idref={quoteattr(ident)}/>")
    lines += ["</spine>", "</package>"]
    return "\n".join(lines).encode()


def _zip(files: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:

        def put(name: str, data: bytes, method=zipfile.ZIP_DEFLATED) -> None:
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = method
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)

        put("mimetype", b"application/epub+zip", zipfile.ZIP_STORED)
        put(
            "META-INF/container.xml",
            b'<?xml version="1.0" encoding="utf-8"?>\n'
            b'<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            b'<rootfiles><rootfile full-path="OEBPS/content.opf" '
            b'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        for name, data in files:
            put(f"OEBPS/{name}", data)
    return buf.getvalue()


def _report(doc: dict, conv: _Converter, structure: dict | None, rules: Rules) -> dict:
    """What the conversion did and what a person should check before trusting it."""
    sections = doc.get("sections") or []
    stats = doc.get("stats") or {}
    labels = conv.labels
    empty = [i + 1 for i, p in enumerate(doc.get("pages") or []) if not p.get("text")]
    linked = {int(pid.split("-")[1]) for _f, pid, _l in conv.page_list}
    unlinked = (
        sorted(set(range(1, len(labels) + 1)) - linked - {p + 1 for p in conv.drop})
        if rules.page_numbers.keep
        else []
    )
    st_pages = (structure or {}).get("pages") or []
    flagged = [p["page"] for p in st_pages if p.get("flagged")]
    warnings: list[str] = []
    ocr = ocr_of(doc)
    if ocr is not None:
        warnings.append(ocr_note(ocr))
    if stats.get("truncated"):
        warnings.append("the PDF was truncated at the extractor's page cap")
    if not any(s.get("from_outline") for s in sections):
        warnings.append("chapters were inferred from headings (the PDF has no outline)")
    if unlinked:
        warnings.append(
            f"no anchor for page(s) {unlinked} (no text on them); they are missing from the page list"
        )
    as_text = [p for p in flagged if p - 1 not in conv.page_img and p - 1 not in conv.drop]
    if as_text:
        warnings.append(f"low-confidence page(s) {as_text} were reflowed as text; check them")
    if structure is None:
        warnings.append("figures, tables, footnotes, and styles were not analysed")
    return {
        "pdf_sha256": doc.get("pdf_sha256"),
        "status": doc.get("status"),
        "source": "ocr" if ocr is not None else "text_layer",
        "ocr": ocr,
        "pages": stats.get("pages", len(labels)),
        "sections": len(conv.groups),
        "page_list_entries": len(conv.page_list),
        "labels_source": (
            "page_labels" if any(p.get("label") for p in doc.get("pages") or []) else "physical"
        ),
        "warnings": warnings,
        "pages_without_text": empty,
        "unlinked_pages": unlinked,
        "flagged_pages": flagged,
        "page_images": sorted(p + 1 for p in conv.page_img),
        "dropped_pages": sorted(p + 1 for p in conv.drop),
        "converted": dict(conv.stats),
        "confidence": [
            {"page": p["page"], "confidence": p["confidence"], "reasons": p["reasons"]}
            for p in st_pages
        ],
        "rules": rules.model_dump(),
    }
