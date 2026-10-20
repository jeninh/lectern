"""A spine item's DOM, indexed by the normalised-text offsets that sentence maps use.

:func:`lectern.positions.normalize_spine_text` defines the text timeline: the document-order
text of ``<body>`` (``<script>``/``<style>`` dropped, unresolved entity references dropped),
whitespace runs collapsed to one space, trimmed. :class:`SpineText` performs exactly the same
walk but remembers where every character came from, so a sentence's ``[char_start, char_end)``
can be turned back into DOM positions (for wrapping it in a ``<span>``) and the text can be
split into the blocks a reader displays.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from lxml import etree

from lectern.positions import normalize_spine_text

XHTML_NS = "http://www.w3.org/1999/xhtml"
SKIP_TAGS = {"script", "style"}

# Elements that may be split in two (shallow-cloned) without changing what a reader shows,
# because they are phrasing content whose rendering does not depend on being a single box.
SPLITTABLE = {
    "a", "abbr", "acronym", "b", "bdi", "bdo", "big", "cite", "code", "data", "del", "dfn",
    "em", "font", "i", "ins", "kbd", "mark", "q", "s", "samp", "small", "span", "strike",
    "strong", "sub", "sup", "time", "tt", "u", "var",
}  # fmt: skip
# Phrasing content that may sit inside a wrapper span when moved whole.
PHRASING = SPLITTABLE | {
    "br", "wbr", "img", "svg", "math", "audio", "video", "picture", "canvas", "embed",
    "object", "iframe", "input", "button", "label", "select", "textarea", "output",
    "meter", "progress", "ruby", "map", "area", "noscript",
}  # fmt: skip
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def local_name(tag) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def parse_xhtml(xhtml: bytes) -> etree._ElementTree:
    """Same parser settings as :func:`normalize_spine_text`, keeping the document wrapper."""
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=False)
    root = etree.fromstring(xhtml, parser)
    if root is None:
        raise ValueError("empty or unparseable XHTML")
    return root.getroottree()


def find_body(root: etree._Element) -> etree._Element:
    body = root.find(f".//{{{XHTML_NS}}}body")
    if body is None:
        body = root.find(".//body")
    return root if body is None else body


@dataclass(eq=False)
class Slot:
    """One text run in the DOM: ``el.text`` (``tail=False``) or ``el.tail`` (``tail=True``)."""

    el: etree._Element
    tail: bool
    start: int  # offset of its first character in the raw (unnormalised) concatenation
    length: int

    @property
    def end(self) -> int:
        return self.start + self.length

    def get(self) -> str:
        return (self.el.tail if self.tail else self.el.text) or ""

    def set(self, value: str) -> None:
        if self.tail:
            self.el.tail = value or None
        else:
            self.el.text = value or None

    def container(self) -> etree._Element:
        """The element whose child list this text run sits in."""
        return self.el.getparent() if self.tail else self.el


def block_of(el: etree._Element, stop: etree._Element) -> etree._Element:
    """Nearest ancestor-or-self that is not splittable inline content (``stop`` at most)."""
    node = el
    while node is not stop and local_name(node.tag) in SPLITTABLE:
        node = node.getparent()
    return node


@dataclass(frozen=True)
class Block:
    kind: str  # "p", "h1".."h6", "li", "quote", "pre"
    start: int  # normalised offsets, end exclusive
    end: int


class SpineText:
    """Parsed spine item plus the raw <-> normalised offset maps."""

    def __init__(self, xhtml: bytes) -> None:
        self.tree = parse_xhtml(xhtml)
        self.body = find_body(self.tree.getroot())
        self.slots: list[Slot] = []
        self._collect(self.body)
        raw = "".join(s.get() for s in self.slots)
        # norm_to_raw[i]: raw offset of normalised character i (a collapsed space maps to the
        # first whitespace character of its run).
        n2r: list[int] = []
        pending: int | None = None
        for i, ch in enumerate(raw):
            if ch.isspace():
                if n2r and pending is None:
                    pending = i
            else:
                if pending is not None:
                    n2r.append(pending)
                    pending = None
                n2r.append(i)
        self.raw = raw
        self.norm_to_raw = n2r
        self.text = " ".join(raw.split())
        if len(self.text) != len(n2r) or self.text != normalize_spine_text(xhtml):
            raise ValueError("spine text does not match the frozen normalisation")
        self._slot_starts = [s.start for s in self.slots]

    def _collect(self, node: etree._Element) -> None:
        # Mirrors lectern.positions._collect_text: comments, PIs, and entity references
        # contribute no text, but their tails do.
        if not isinstance(node.tag, str) or local_name(node.tag) in SKIP_TAGS:
            return
        pos = self.slots[-1].end if self.slots else 0
        if node.text:
            self.slots.append(Slot(node, False, pos, len(node.text)))
        for child in node:
            self._collect(child)
            if child.tail:
                pos = self.slots[-1].end if self.slots else 0
                self.slots.append(Slot(child, True, pos, len(child.tail)))

    # --- coordinates ---------------------------------------------------------------------

    def raw_range(self, char_start: int, char_end: int) -> tuple[int, int]:
        """Raw ``[start, end)`` covering normalised ``[char_start, char_end)``."""
        if not 0 <= char_start < char_end <= len(self.text):
            raise ValueError(f"span {char_start}-{char_end} outside 0-{len(self.text)}")
        return self.norm_to_raw[char_start], self.norm_to_raw[char_end - 1] + 1

    def norm_offset(self, raw: int) -> int:
        """Normalised offset of the first normalised character at or after raw offset."""
        return bisect_left(self.norm_to_raw, raw)

    def pieces(self, raw_start: int, raw_end: int) -> list[tuple[Slot, int, int]]:
        """``(slot, start, end)`` offsets within each slot text overlapping the raw range."""
        out = []
        i = max(0, bisect_right(self._slot_starts, raw_start) - 1)
        while i < len(self.slots) and self.slots[i].start < raw_end:
            s = self.slots[i]
            a, b = max(raw_start, s.start), min(raw_end, s.end)
            if a < b:
                out.append((s, a - s.start, b - s.start))
            i += 1
        return out

    # --- reader blocks ---------------------------------------------------------------------

    def blocks(self) -> list[Block]:
        """Consecutive text grouped by its nearest block element, in normalised offsets."""
        out: list[Block] = []
        current: etree._Element | None = None
        start = end = 0
        for s in self.slots:
            if not s.get().strip():
                continue
            blk = block_of(s.container(), self.body)
            a = self.norm_offset(s.start + len(s.get()) - len(s.get().lstrip()))
            b = self.norm_offset(s.start + len(s.get().rstrip()))
            if blk is current:
                end = b
                continue
            if current is not None and end > start:
                out.append(Block(_kind(current), start, end))
            current, start, end = blk, a, b
        if current is not None and end > start:
            out.append(Block(_kind(current), start, end))
        return out


def _kind(el: etree._Element) -> str:
    name = local_name(el.tag)
    if name in HEADINGS:
        return name
    if name == "li":
        return "li"
    if name == "blockquote" or (
        el.getparent() is not None and local_name(el.getparent().tag) == "blockquote"
    ):
        return "quote"
    if name == "pre":
        return "pre"
    return "p"
