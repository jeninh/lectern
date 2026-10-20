"""A spine item's normalised text with a source map back to its DOM.

:func:`lectern.positions.normalize_spine_text` defines the text timeline (frozen). This module
builds the *same* string while remembering, for every character, which DOM text node it came
from and the offset inside that node. That is what lets Lectern convert between

- KOReader xpointers (``/body/DocFragment[k]/body/div/p[3]/text().12``, a DOM path plus a
  character offset inside a text node, in crengine's conventions),
- text positions ``(spine_index, char)`` on the normalised text, and
- sentence positions, through :class:`lectern.positions.SentenceIndex`.

It also records block boundaries so the aligner can start a new sentence at every paragraph
or heading, which whitespace collapsing alone would hide.

crengine conventions reproduced here (see ``ldomXPointer::toStringV2`` and
``createXPointerV2`` in KOReader's crengine):

- an element segment is ``name`` or ``name[i]``, ``i`` being 1-based among sibling elements
  with the same name; the index is written only when there is more than one such sibling
  (newer KOReader builds always write it, and the parser accepts both forms);
- a text segment is ``text()`` or ``text()[i]``, 1-based among the parent's text children;
- whitespace-only text nodes directly inside a block element are not DOM nodes for crengine
  (they are dropped at load time or by autoboxing), so they do not count;
- crengine's own wrapper elements (``autoBoxing``, ``floatBox``, ``inlineBox``) are
  transparent in V2 paths and are skipped when they appear in an incoming pointer;
- the offset after the final ``.`` counts characters of the text node with whitespace runs
  collapsed to one space, which is what crengine stores.

Nothing here is byte-exact with crengine's DOM for every document (CSS can turn an inline into
a block, ``<pre>`` keeps its whitespace); the conversions are tolerant and clamp to the nearest
position rather than fail. Offsets within a text node are off by at most the number of
collapsed whitespace characters, i.e. a word boundary, which is below what a reader notices.
"""

from __future__ import annotations

import re
from array import array
from bisect import bisect_left
from dataclasses import dataclass, replace

from lxml import etree

from lectern.positions import normalize_spine_text

_XHTML_NS = "http://www.w3.org/1999/xhtml"
_SKIP_TAGS = {"script", "style"}
# Elements crengine lays out as blocks by default (lvstsheet defaults plus HTML5 sectioning).
BLOCK_TAGS = frozenset(
    """address article aside blockquote body caption center dd details dir div dl dt fieldset
    figcaption figure footer form frameset h1 h2 h3 h4 h5 h6 header hgroup hr html li main
    menu nav noscript ol p pre section summary table tbody td tfoot th thead title tr ul
    DocFragment""".split()
)
BOXING_TAGS = frozenset({"autoBoxing", "floatBox", "inlineBox", "tabularBox", "pseudoElem"})

_DOCFRAGMENT = re.compile(r"^/body/DocFragment(?:\[(\d+)\])?")
_STEP = re.compile(r"^([A-Za-z_][\w:-]*|text\(\))(?:\[(\d+)\])?$")


@dataclass(frozen=True)
class TextNodeRef:
    """One DOM text node that contributed characters to the stream."""

    key: str  # canonical path with every index written, e.g. "/body[1]/p[3]/text()[2]"
    path: str  # crengine display form (indices only where siblings exist), "/body/p[3]/text()[2]"
    block: int  # id of the nearest block ancestor; sentences never span two blocks
    start: int  # first stream char contributed by this node
    end: int  # one past the last


class SpineText:
    """The normalised text of one spine item plus its source map.

    ``text`` is exactly :func:`lectern.positions.normalize_spine_text` of the same bytes
    (tested); ``node_at(char)`` and ``char_at(path, offset)`` go between the two coordinate
    systems.
    """

    __slots__ = (
        "_elements",
        "_node_by_key",
        "_node_of_char",
        "_nodes",
        "_offset_in_node",
        "_root_key",
        "text",
    )

    def __init__(self, xhtml: bytes) -> None:
        builder = _Builder()
        builder.run(xhtml)
        self.text: str = "".join(builder.out)
        self._nodes: list[TextNodeRef] = builder.nodes
        self._node_by_key: dict[str, TextNodeRef] = {n.key: n for n in builder.nodes}
        self._node_of_char: array = builder.node_of_char
        self._offset_in_node: array = builder.offset_in_node
        self._elements: dict[str, tuple[int, int]] = builder.elements
        self._root_key: str = builder.root_key

    # --- block boundaries (sentence segmentation) ----------------------------------------

    @property
    def nodes(self) -> list[TextNodeRef]:
        return self._nodes

    def block_at(self, char: int) -> int:
        """Block id of the text node that char ``char`` came from."""
        return self._nodes[self._node_of_char[char]].block

    def block_starts(self) -> list[int]:
        """Stream offsets at which a new block begins (the first one is 0)."""
        starts: list[int] = []
        last = None
        for node in self._nodes:
            if node.block != last:
                starts.append(node.start)
                last = node.block
        return starts

    # --- stream char -> xpointer ---------------------------------------------------------

    def xpointer_tail(self, char: int) -> str:
        """``/body/p[3]/text().12`` for stream offset ``char`` (the part after DocFragment).

        Past the end of the text, the end of the last text node; an empty item points at
        its body.
        """
        if not self._nodes:
            return f"{self._root_display()}.0"
        if char >= len(self.text):
            node = self._nodes[-1]
            return f"{node.path}.{self._offset_in_node[node.end - 1] + 1}"
        char = max(0, char)
        node = self._nodes[self._node_of_char[char]]
        return f"{node.path}.{self._offset_in_node[char]}"

    def _root_display(self) -> str:
        return "/" + self._root_key.lstrip("/").split("[", 1)[0]

    # --- xpointer -> stream char ---------------------------------------------------------

    def char_at(self, tail: str) -> int:
        """Stream offset for the part of an xpointer after ``DocFragment[k]``.

        Accepts ``/body/div/p[3]/text().12``, ``/body/p[3].0``, ``/body/p[3]`` and the
        indexed variants; unknown or unresolvable paths clamp to the deepest element that
        did resolve, and finally to 0.
        """
        path, offset = _split_offset(tail)
        steps = [s for s in path.split("/") if s]
        if steps and steps[0].split("[", 1)[0] == "html":
            steps = steps[1:]
        element_key = ""
        text_index: int | None = None
        for step in steps:
            m = _STEP.match(step)
            if m is None:
                break
            name, idx = m.group(1), int(m.group(2) or 1)
            if name == "text()":
                text_index = idx
                break
            if name in BOXING_TAGS:
                continue
            candidate = f"{element_key}/{name}[{idx}]"
            if candidate not in self._elements:
                if not element_key and name != "body" and self._root_key in self._elements:
                    # pointer written without the body step; retry below it
                    candidate = f"{self._root_key}/{name}[{idx}]"
                    if candidate in self._elements:
                        element_key = candidate
                        continue
                break
            element_key = candidate
        if not element_key:
            element_key = self._root_key
        span = self._elements.get(element_key, (0, 0))
        # An element's span can begin with the separator space owned by the text before it.
        first = span[0] + 1 if span[0] < span[1] and self.text[span[0]] == " " else span[0]
        if text_index is None:
            return min(first, len(self.text))
        node = self._text_node(element_key, text_index)
        if node is None:
            return min(first, len(self.text))
        # first stream char of that node whose collapsed offset reaches the pointer's
        lo, hi = node.start, node.end
        i = bisect_left(self._offset_in_node, offset, lo, hi)
        return i if i < hi else hi

    def _text_node(self, element_key: str, index: int) -> TextNodeRef | None:
        prefix = f"{element_key}/text()["
        exact = self._node_by_key.get(f"{prefix}{index}]")
        if exact is not None:
            return exact
        # The pointer names a text child that has no stream characters (or crengine counted
        # differently): use the element's last text node instead.
        found = [n for n in self._nodes if n.key.startswith(prefix)]
        return found[-1] if found else None


def _split_offset(tail: str) -> tuple[str, int]:
    """``/body/p[3]/text().12`` -> (``/body/p[3]/text()``, 12). No offset -> 0."""
    last = tail.rsplit("/", 1)[-1]
    if "." in last:
        head, _, digits = tail.rpartition(".")
        if digits.isdigit():
            return head, int(digits)
    return tail, 0


class _Builder:
    """One pass over the DOM producing the normalised text and the per-char source map."""

    def __init__(self) -> None:
        self.out: list[str] = []
        self.nodes: list[TextNodeRef] = []
        self.node_of_char = array("i")
        self.offset_in_node = array("i")
        self.elements: dict[str, tuple[int, int]] = {}
        self.root_key = "/body[1]"
        self._pending_space = False
        self._block_counter = 0

    def run(self, xhtml: bytes) -> None:
        parser = etree.XMLParser(
            recover=True, resolve_entities=False, no_network=True, huge_tree=False
        )
        try:
            root = etree.fromstring(xhtml, parser)
        except (etree.LxmlError, ValueError):
            return  # unreadable item: no text, exactly like normalize_spine_text
        if root is None:
            return
        body = root.find(f".//{{{_XHTML_NS}}}body")
        if body is None:
            body = root.find(".//body")
        if body is None:
            body = root
        name = _local(body.tag)
        self.root_key = f"/{name}[1]"
        self._element(body, self.root_key, f"/{name}", self._new_block())

    def _new_block(self) -> int:
        self._block_counter += 1
        return self._block_counter

    def _element(self, el, key: str, display: str, block: int) -> None:
        start = len(self.out)
        is_block = _local(el.tag) in BLOCK_TAGS
        if is_block and key != self.root_key:
            block = self._new_block()
        children = list(el)
        # Text children in crengine's order: el.text, then each child's tail. Whitespace-only
        # ones inside a block element do not exist for crengine.
        raw_pieces: list[str | None] = [el.text] + [c.tail for c in children]
        kept: list[int] = []
        for i, piece in enumerate(raw_pieces):
            if not piece:
                continue
            if is_block and piece.isspace() and _ignorable_space(i, raw_pieces, children):
                continue
            kept.append(i)
        text_count = len(kept)
        text_index = {i: n + 1 for n, i in enumerate(kept)}
        name_counts: dict[str, int] = {}
        for c in children:
            if isinstance(c.tag, str):
                name_counts[_local(c.tag)] = name_counts.get(_local(c.tag), 0) + 1
        name_seen: dict[str, int] = {}

        def emit_text(i: int) -> None:
            piece = raw_pieces[i]
            if not piece:
                return
            if i not in text_index:
                # dropped by crengine, but still a whitespace run for the normalised text
                self._pending_space = True
                return
            idx = text_index[i]
            suffix = f"[{idx}]" if text_count > 1 else ""
            self._text(piece, f"{key}/text()[{idx}]", f"{display}/text(){suffix}", block)

        emit_text(0)
        for n, c in enumerate(children):
            if isinstance(c.tag, str):
                cname = _local(c.tag)
                if cname not in _SKIP_TAGS:
                    name_seen[cname] = name_seen.get(cname, 0) + 1
                    idx = name_seen[cname]
                    suffix = f"[{idx}]" if name_counts[cname] > 1 else ""
                    self._element(c, f"{key}/{cname}[{idx}]", f"{display}/{cname}{suffix}", block)
            emit_text(n + 1)
        self.elements[key] = (start, len(self.out))

    def _text(self, piece: str, key: str, display: str, block: int) -> None:
        start = len(self.out)
        coff = 0  # offset in the node's collapsed text
        node_space = False
        node_idx = len(self.nodes)
        for ch in piece:
            if ch.isspace():
                self._pending_space = True
                node_space = True
                continue
            if node_space and coff > 0:
                coff += 1
            node_space = False
            if self._pending_space and self.out:
                self.out.append(" ")
                if coff > 0:
                    self.node_of_char.append(node_idx)
                    self.offset_in_node.append(coff - 1)
                else:
                    # The separator between two text nodes belongs to the earlier one (as
                    # its trailing space), so this node's first char is its offset 0.
                    prev = self.nodes[-1]
                    self.nodes[-1] = replace(prev, end=prev.end + 1)
                    self.node_of_char.append(node_idx - 1)
                    self.offset_in_node.append(self.offset_in_node[-1] + 1)
                    start += 1
            self._pending_space = False
            self.out.append(ch)
            self.node_of_char.append(node_idx)
            self.offset_in_node.append(coff)
            coff += 1
        end = len(self.out)
        if end > start:
            self.nodes.append(TextNodeRef(key, display, block, start, end))


def _ignorable_space(i: int, raw_pieces: list[str | None], children: list) -> bool:
    """A whitespace-only text piece crengine drops: first or last child of a block, or next to
    a block-level sibling element."""
    if i == 0 or i == len(raw_pieces) - 1:
        return True
    before = children[i - 1]
    after = children[i]
    for sib in (before, after):
        if not isinstance(sib.tag, str) or _local(sib.tag) in BLOCK_TAGS:
            return True
    return False


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def check_matches_normalisation(xhtml: bytes) -> bool:
    """True when the source-mapped text equals the frozen normalisation (used by tests)."""
    return SpineText(xhtml).text == normalize_spine_text(xhtml)
