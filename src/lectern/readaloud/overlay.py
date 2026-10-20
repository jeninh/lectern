"""Build a read-aloud EPUB: the original EPUB plus EPUB 3 Media Overlays.

What changes, and what does not:

- Every sentence of the work is wrapped in ``<span id="s{n}">`` inside its spine item. Only
  wrappers are added: the normalised text of every spine item is identical before and after
  (checked on every build), so the visible text and the sentence coordinates are unchanged.
  A sentence that crosses inline markup (``<em>``, ``<a>``, ...) splits that inline element
  into two shallow copies (the copy drops ``id``); a sentence that crosses a block boundary
  (two paragraphs) gets one wrapper per block, ``s{n}``, ``s{n}-2``, ... and its audio clip
  is shared between them in proportion to their length.
- One SMIL file per spine item with sentences; one ``<par>`` per matched sentence (part)
  pointing at the sentence's fragment and at a clip of an embedded audio track.
- The OPF gains the audio and SMIL manifest items, ``media-overlay`` on the spine items,
  ``media:duration`` per overlay and in total, ``media:active-class`` with a small
  stylesheet, and a fresh ``dcterms:modified``.
- EPUB 2 sources are upgraded to a valid EPUB 3 package on the way (``version="3.0"``,
  EPUB 3 metadata forms, a navigation document generated from the NCX, HTML5 doctype),
  because Media Overlays only exist in EPUB 3. The NCX stays for older readers.
- Everything else (images, fonts, stylesheets, unrelated documents) is copied byte for byte.

The source file is only read; the caller decides where the output goes (see
:mod:`lectern.readaloud.cache`, which writes to a temporary file and renames it).
"""

from __future__ import annotations

import html.entities
import posixpath
import re
import shutil
import zipfile
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import groupby
from pathlib import Path
from urllib.parse import quote, unquote, urldefrag

from lxml import etree

from lectern.positions import SentenceLike, normalize_spine_text
from lectern.readaloud.text import (
    PHRASING,
    SKIP_TAGS,
    XHTML_NS,
    Slot,
    SpineText,
    block_of,
    local_name,
    parse_xhtml,
)

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
OPS_NS = "http://www.idpf.org/2007/ops"
SMIL_NS = "http://www.w3.org/ns/SMIL"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
NS = {"opf": OPF_NS, "dc": DC_NS, "c": CONTAINER_NS, "ncx": NCX_NS, "smil": SMIL_NS}

XHTML_TYPE = "application/xhtml+xml"
SMIL_TYPE = "application/smil+xml"
# EPUB 3 core media types for audio; anything else would need a fallback chain.
AUDIO_EXTENSIONS = {"audio/mpeg": "mp3", "audio/mp4": "m4a"}
ACTIVE_CLASS = "-epub-media-overlay-active"
PLAYBACK_CLASS = "-epub-media-overlay-playing"
STYLESHEET = f""".{ACTIVE_CLASS} {{
  background-color: rgba(255, 213, 79, 0.55);
  border-radius: 0.15em;
}}
"""
# http-equiv values HTML5 allows; EPUB 2 files often carry Content-Style-Type and friends.
_HTTP_EQUIV_OK = {
    "content-type",
    "refresh",
    "default-style",
    "x-ua-compatible",
    "content-security-policy",
}
_FIXED_ZIP_TIME = (2000, 1, 1, 0, 0, 0)
_SPAN_ID = re.compile(r"^s\d+(-\d+)?$")
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
_MIN_CLIP_MS = 1


class OverlayError(ValueError):
    """The inputs cannot produce a read-aloud EPUB; the message is safe to show."""


@dataclass(frozen=True)
class OverlayTrack:
    """One audio file on the work timeline (``offset_s`` = sum of earlier durations)."""

    path: Path
    media_type: str
    offset_s: float
    duration_s: float


@dataclass
class OverlayReport:
    spans: int = 0  # sentence wrappers written (a sentence across blocks has several)
    pars: int = 0
    overlays: int = 0  # SMIL files = spine items with at least one par
    duration_s: float = 0.0
    upgraded_from: str | None = None  # "2.0" when the source was EPUB 2
    smil: dict[str, str] = field(default_factory=dict)  # zip name -> spine item zip name


# --- sentence wrapping ----------------------------------------------------------------------


@dataclass
class Part:
    id: str
    length: int  # normalised characters, for sharing a sentence's audio between parts


def _ancestors(el: etree._Element) -> list[etree._Element]:
    out = []
    while el is not None:
        out.append(el)
        el = el.getparent()
    return out


def _lca(a: etree._Element, b: etree._Element) -> etree._Element:
    seen = {id(x) for x in _ancestors(a)}
    for x in _ancestors(b):
        if id(x) in seen:
            return x
    raise OverlayError("nodes are not in the same document")


def _child_of(parent: etree._Element, node: etree._Element) -> etree._Element:
    while node.getparent() is not parent:
        node = node.getparent()
    return node


def _clone(el: etree._Element) -> etree._Element:
    attrib = {k: v for k, v in el.attrib.items() if local_name(k) != "id"}
    return el.makeelement(el.tag, attrib)


def _split(a: etree._Element, slot: Slot, off: int) -> etree._Element:
    """Split ``a`` at ``(slot, off)`` inside it: ``a`` keeps what precedes, the returned shallow
    copy (inserted right after ``a``) holds the rest. Original nodes stay where they are on the
    left, so text positions before the boundary remain valid."""
    container = slot.container()
    right = _clone(a)
    if container is a:
        text = slot.get()
        slot.set(text[:off])
        right.text = text[off:] or None
        node = slot.el.getnext() if slot.tail else (a[0] if len(a) else None)
    else:
        node = _split(_child_of(a, container), slot, off)
    movers = []
    while node is not None:
        movers.append(node)
        node = node.getnext()
    for m in movers:
        right.append(m)  # lxml moves an element together with its tail
    right.tail, a.tail = a.tail, None
    parent = a.getparent()
    parent.insert(parent.index(a) + 1, right)
    return right


def _at_start(a: etree._Element, slot: Slot, off: int) -> bool:
    """Only whitespace precedes the boundary inside ``a``."""
    if slot.tail or slot.get()[:off].strip():
        return False
    node = slot.el
    while node is not a:
        parent = node.getparent()
        if (parent.text or "").strip() or parent.index(node) != 0:
            return False
        node = parent
    return True


def _at_end(b: etree._Element, slot: Slot, off: int) -> bool:
    """Only whitespace follows the boundary inside ``b``."""
    if slot.get()[off:].strip():
        return False
    if slot.tail:
        if slot.el.getnext() is not None:
            return False
        node = slot.container()
    else:
        if len(slot.el):
            return False
        node = slot.el
    while node is not b:
        if (node.tail or "").strip() or node.getnext() is not None:
            return False
        node = node.getparent()
    return True


def _new_span(like: etree._Element, span_id: str) -> etree._Element:
    ns = like.tag.split("}", 1)[0][1:] if like.tag.startswith("{") else None
    return like.makeelement(f"{{{ns}}}span" if ns else "span", {"id": span_id})


def _wrap_same_slot(slot: Slot, a: int, b: int, span_id: str) -> None:
    text = slot.get()
    container = slot.container()
    span = _new_span(container, span_id)
    span.text, span.tail = text[a:b], text[b:] or None
    slot.set(text[:a])
    container.insert(container.index(slot.el) + 1 if slot.tail else 0, span)


def _moved_children_are_phrasing(
    lca: etree._Element, start: Slot, end: Slot, skip: set[int]
) -> bool:
    """Children of ``lca`` that a single wrapper would move whole may sit inside a span."""
    kids = list(lca)
    if start.container() is lca:
        first = kids.index(start.el) + 1 if start.tail else 0
    else:
        first = kids.index(_child_of(lca, start.container()))
    if end.container() is lca:
        last = kids.index(end.el) if end.tail else -1
    else:
        last = kids.index(_child_of(lca, end.container()))
    for child in kids[first : last + 1]:
        if id(child) in skip or not isinstance(child.tag, str):
            continue
        if local_name(child.tag) not in PHRASING:
            return False
    return True


def _wrap_range(start: Slot, so: int, end: Slot, eo: int, span_id: str) -> None:
    """Wrap DOM content from ``(start, so)`` to ``(end, eo)`` in one span. Inline ancestors
    that straddle a boundary are split first; the end is handled before the start so that the
    start position is still valid."""
    if start is end:
        _wrap_same_slot(start, so, eo, span_id)
        return
    cs, ce = start.container(), end.container()
    lca = _lca(cs, ce)
    a = None if cs is lca else _child_of(lca, cs)
    b = None if ce is lca else _child_of(lca, ce)

    end_after: etree._Element | None = None  # wrap through this child (exclusive of its tail)
    end_slot: Slot | None = None  # or through this tail slot of a child, up to eo
    if b is None:
        end_slot = end
    else:
        if not _at_end(b, end, eo):
            _split(b, end, eo)
        end_after = b

    first: etree._Element | None
    span = _new_span(lca, span_id)
    if a is None:
        text = start.get()
        start.set(text[:so])
        span.text = text[so:] or None
        first = start.el.getnext() if start.tail else (lca[0] if len(lca) else None)
        index = lca.index(start.el) + 1 if start.tail else 0
    else:
        if not _at_start(a, start, so):
            a_right = _split(a, start, so)
            if end_after is a:
                end_after = a_right
            elif end_slot is not None and end_slot.el is a:
                end_slot = Slot(a_right, True, end_slot.start, end_slot.length)
            a = a_right
        first, index = a, lca.index(a)

    last = end_after if end_after is not None else end_slot.el  # type: ignore[union-attr]
    movers = []
    node = first
    while node is not None:
        movers.append(node)
        if node is last:
            break
        node = node.getnext()
    if not movers or movers[-1] is not last:
        raise OverlayError("sentence boundaries are out of order in the document")
    lca.insert(index, span)
    if end_after is not None:
        saved, end_after.tail = end_after.tail, None
        for m in movers:
            span.append(m)
        span.tail = saved
    else:
        for m in movers:
            span.append(m)
        text = last.tail or ""
        last.tail = text[:eo] or None
        span.tail = text[eo:] or None


def _groups(st: SpineText, raw_start: int, raw_end: int) -> list[list[tuple[Slot, int, int]]]:
    """Pieces of the range grouped by nearest block, whitespace-only edges trimmed."""
    out = []
    pieces = st.pieces(raw_start, raw_end)
    for _, grp in groupby(pieces, key=lambda p: id(block_of(p[0].container(), st.body))):
        g = list(grp)
        while g and not g[0][0].get()[g[0][1] : g[0][2]].strip():
            g.pop(0)
        while g and not g[-1][0].get()[g[-1][1] : g[-1][2]].strip():
            g.pop()
        if not g:
            continue
        s, a, b = g[0]
        t = s.get()[a:b]
        g[0] = (s, a + len(t) - len(t.lstrip()), b)
        s, a, b = g[-1]
        t = s.get()[a:b]
        g[-1] = (s, a, b - (len(t) - len(t.rstrip())))
        out.append(g)
    return out


def _collect_text(node, parts: list[str]) -> None:
    if not isinstance(node.tag, str) or local_name(node.tag) in SKIP_TAGS:
        return
    if node.text:
        parts.append(node.text)
    for child in node:
        _collect_text(child, parts)
        if child.tail:
            parts.append(child.tail)


def _norm(text: str) -> str:
    return " ".join(text.split())


def wrap_sentences(
    st: SpineText, sentences: Sequence[SentenceLike], prefix: str
) -> dict[int, list[Part]]:
    """Wrap each sentence of this spine item; returns ``n -> parts`` in document order.

    Sentences are processed last to first: wrapping only touches the DOM at or after a
    sentence's start, so the text positions of earlier sentences stay valid.
    """
    out: dict[int, list[Part]] = {}
    expected: list[tuple[str, str]] = []
    for s in sorted(sentences, key=lambda s: s.char_start, reverse=True):
        raw_start, raw_end = st.raw_range(s.char_start, s.char_end)
        groups = _groups(st, raw_start, raw_end)
        # Plan the wrappers before touching the DOM: one per block group, or one per text
        # run when moving the group whole would put block content inside a span.
        ranges: list[tuple[Slot, int, Slot, int]] = []
        for g in groups:
            (s0, a0, _), (s1, _, b1) = g[0], g[-1]
            if s0 is s1:
                ranges.append((s0, a0, s1, b1))
                continue
            lca = _lca(s0.container(), s1.container())
            inline_ends = {
                id(_child_of(lca, c)) for c in (s0.container(), s1.container()) if c is not lca
            }
            if _moved_children_are_phrasing(lca, s0, s1, inline_ends):
                ranges.append((s0, a0, s1, b1))
            else:
                ranges.extend((p, a, p, b) for p, a, b in g if p.get()[a:b].strip())
        ids = [f"{prefix}{s.n}"] + [f"{prefix}{s.n}-{k}" for k in range(2, len(ranges) + 1)]
        parts = []
        for span_id, (s0, a0, s1, b1) in zip(ids, ranges, strict=True):
            raw = st.raw[s0.start + a0 : s1.start + b1]
            parts.append(Part(span_id, len(_norm(raw))))
            expected.append((span_id, _norm(raw)))
        for span_id, (s0, a0, s1, b1) in reversed(list(zip(ids, ranges, strict=True))):
            _wrap_range(s0, a0, s1, b1, span_id)
        out[s.n] = parts

    # Safety net: wrappers contain exactly their sentence text and the page text is unchanged.
    by_id = {el.get("id"): el for el in st.body.iter() if isinstance(el.tag, str) and el.get("id")}
    for span_id, want in expected:
        parts_text: list[str] = []
        _collect_text(by_id[span_id], parts_text)
        if _norm("".join(parts_text)) != want:
            raise OverlayError(f"wrapper {span_id} does not match its sentence text")
    parts_text = []
    _collect_text(st.body, parts_text)
    if _norm("".join(parts_text)) != st.text:
        raise OverlayError("wrapping changed the text of the document")
    return out


# --- packaging --------------------------------------------------------------------------------


def _clock(ms: int) -> str:
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}"


def _href(from_dir: str, to_name: str) -> str:
    return quote(posixpath.relpath(to_name, from_dir or "."), safe="/")


def _unique(base: str, used: set[str]) -> str:
    name, n = base, 2
    while name in used:
        name = f"{base}-{n}"
        n += 1
    used.add(name)
    return name


def _resolve_entities(root: etree._Element) -> None:
    """Replace entity references with their characters (an HTML5 doctype declares none)."""
    for ent in list(root.iter(etree.Entity)):
        char = html.entities.html5.get(f"{ent.name};", "")
        text = char + (ent.tail or "")
        prev, parent = ent.getprevious(), ent.getparent()
        if prev is not None:
            prev.tail = (prev.tail or "") + text
        else:
            parent.text = (parent.text or "") + text
        parent.remove(ent)


def _modernise_xhtml(tree: etree._ElementTree) -> None:
    """EPUB 2 -> 3: drop http-equiv values HTML5 rejects; entities become characters."""
    root = tree.getroot()
    for meta in list(root.iter(f"{{{XHTML_NS}}}meta", "meta")):
        equiv = meta.get("http-equiv")
        if equiv is not None and equiv.lower() not in _HTTP_EQUIV_OK:
            parent = meta.getparent()
            prev = meta.getprevious()
            if meta.tail:
                if prev is not None:
                    prev.tail = (prev.tail or "") + meta.tail
                else:
                    parent.text = (parent.text or "") + meta.tail
            parent.remove(meta)
    _resolve_entities(root)


def _serialize_xhtml(tree: etree._ElementTree, html5_doctype: bool) -> bytes:
    if html5_doctype:
        return etree.tostring(
            tree, encoding="utf-8", xml_declaration=True, doctype="<!DOCTYPE html>"
        )
    return etree.tostring(tree, encoding="utf-8", xml_declaration=True)


def _add_stylesheet(tree: etree._ElementTree, href: str) -> None:
    root = tree.getroot()
    head = root.find(f"{{{XHTML_NS}}}head")
    if head is None:
        head = root.find("head")
    if head is None:
        return
    ns = XHTML_NS if head.tag.startswith("{") else None
    link = head.makeelement(
        f"{{{ns}}}link" if ns else "link", {"href": href, "rel": "stylesheet", "type": "text/css"}
    )
    link.tail = "\n"
    head.append(link)


@dataclass
class _Package:
    opf_name: str
    opf_dir: str
    tree: etree._ElementTree
    version: str
    manifest: etree._Element
    metadata: etree._Element
    spine: etree._Element
    items: dict[str, etree._Element]  # manifest id -> <item>
    names: dict[str, str]  # manifest id -> zip member name
    spine_ids: list[str]


def _read_package(zf: zipfile.ZipFile) -> _Package:
    try:
        container = etree.fromstring(zf.read("META-INF/container.xml"), _PARSER)
        rootfile = container.find(".//c:rootfile", NS)
        opf_name = rootfile.get("full-path")
        tree = etree.fromstring(zf.read(opf_name), _PARSER).getroottree()
    except (KeyError, AttributeError, TypeError, etree.XMLSyntaxError) as exc:
        raise OverlayError("the EPUB has no readable package document") from exc
    pkg = tree.getroot()
    manifest, metadata, spine = (
        pkg.find(f"opf:{t}", NS) for t in ("manifest", "metadata", "spine")
    )
    if manifest is None or metadata is None or spine is None:
        raise OverlayError("the package document has no metadata, manifest, or spine")
    opf_dir = posixpath.dirname(opf_name)
    items, names = {}, {}
    for it in manifest.findall("opf:item", NS):
        if it.get("id") and it.get("href"):
            items[it.get("id")] = it
            names[it.get("id")] = posixpath.normpath(
                posixpath.join(opf_dir, unquote(urldefrag(it.get("href")).url))
            )
    spine_ids = [ref.get("idref") for ref in spine.findall("opf:itemref", NS)]
    if any(i not in items for i in spine_ids):
        raise OverlayError("a spine item is missing from the manifest")
    return _Package(
        opf_name, opf_dir, tree, pkg.get("version", "2.0"), manifest, metadata, spine,
        items, names, spine_ids,
    )  # fmt: skip


def _meta(pkg: _Package, prop: str, value: str, refines: str | None = None, **extra) -> None:
    attrs = {"property": prop, **extra}
    if refines:
        attrs["refines"] = f"#{refines}"
    el = etree.SubElement(pkg.metadata, f"{{{OPF_NS}}}meta", attrs)
    el.text = value
    el.tail = "\n  "


def _upgrade_metadata(pkg: _Package, used_ids: set[str]) -> None:
    """EPUB 2 metadata forms -> EPUB 3 (``opf:`` attributes become refinements)."""
    opf_attr = f"{{{OPF_NS}}}"
    dates = []
    for el in list(pkg.metadata):
        if not isinstance(el.tag, str):
            continue
        attrs = dict(el.attrib)
        for k in attrs:
            if k.startswith(opf_attr) or k == f"{{{XSI_NS}}}type":
                del el.attrib[k]
        name = local_name(el.tag)
        if el.tag == f"{{{DC_NS}}}date":
            dates.append((attrs.get(f"{opf_attr}event"), el))
        if name in ("creator", "contributor"):
            file_as, role = attrs.get(f"{opf_attr}file-as"), attrs.get(f"{opf_attr}role")
            if file_as or role:
                if not el.get("id"):
                    el.set("id", _unique(f"lectern-{name}", used_ids))
                if file_as:
                    _meta(pkg, "file-as", file_as, el.get("id"))
                if role:
                    _meta(pkg, "role", role, el.get("id"), scheme="marc:relators")
    # EPUB 3 allows one dc:date: keep the publication date.
    if len(dates) > 1:
        keep = next((el for ev, el in dates if ev == "publication"), dates[0][1])
        for _, el in dates:
            if el is not keep:
                pkg.metadata.remove(el)
    cover = next(
        (
            m.get("content")
            for m in pkg.metadata.findall("opf:meta", NS)
            if m.get("name") == "cover"
        ),
        None,
    )
    item = pkg.items.get(cover or "")
    if item is not None and (item.get("media-type") or "").startswith("image/"):
        props = (item.get("properties") or "").split()
        if "cover-image" not in props:
            item.set("properties", " ".join([*props, "cover-image"]))


def _nav_document(zf: zipfile.ZipFile, pkg: _Package, nav_name: str, title: str) -> bytes:
    """An EPUB 3 navigation document built from the NCX (or the spine if there is none)."""
    nav_dir = posixpath.dirname(nav_name)
    ncx_id = pkg.spine.get("toc")
    entries: list[tuple[str, str, list]] = []

    def points(parent, ncx_dir) -> list:
        out = []
        for np in parent.findall("ncx:navPoint", NS):
            label = (
                " ".join("".join(np.find("ncx:navLabel/ncx:text", NS).itertext()).split())
                if np.find("ncx:navLabel/ncx:text", NS) is not None
                else ""
            )
            src = np.find("ncx:content", NS)
            href = ""
            if src is not None and src.get("src"):
                url, frag = urldefrag(src.get("src"))
                target = posixpath.normpath(posixpath.join(ncx_dir, unquote(url)))
                href = _href(nav_dir, target) + (f"#{frag}" if frag else "")
            out.append((label or "Untitled", href, points(np, ncx_dir)))
        return out

    if ncx_id in pkg.names:
        try:
            ncx = etree.fromstring(zf.read(pkg.names[ncx_id]), _PARSER)
            nav_map = ncx.find("ncx:navMap", NS)
            if nav_map is not None:
                entries = points(nav_map, posixpath.dirname(pkg.names[ncx_id]))
        except (KeyError, etree.XMLSyntaxError):
            entries = []
    if not entries:
        entries = (
            [(title, _href(nav_dir, pkg.names[pkg.spine_ids[0]]), [])] if pkg.spine_ids else []
        )

    h = etree.Element(f"{{{XHTML_NS}}}html", nsmap={None: XHTML_NS, "epub": OPS_NS})
    head = etree.SubElement(h, f"{{{XHTML_NS}}}head")
    etree.SubElement(head, f"{{{XHTML_NS}}}title").text = title
    body = etree.SubElement(h, f"{{{XHTML_NS}}}body")
    nav = etree.SubElement(body, f"{{{XHTML_NS}}}nav", {f"{{{OPS_NS}}}type": "toc", "id": "toc"})
    etree.SubElement(nav, f"{{{XHTML_NS}}}h1").text = "Contents"

    def render(parent, items) -> None:
        ol = etree.SubElement(parent, f"{{{XHTML_NS}}}ol")
        for label, href, children in items:
            li = etree.SubElement(ol, f"{{{XHTML_NS}}}li")
            if href:
                etree.SubElement(li, f"{{{XHTML_NS}}}a", {"href": href}).text = label
            else:
                etree.SubElement(li, f"{{{XHTML_NS}}}span").text = label
            if children:
                render(li, children)

    render(nav, entries)
    return etree.tostring(
        h.getroottree(), encoding="utf-8", xml_declaration=True, doctype="<!DOCTYPE html>"
    )


def build_read_aloud_epub(
    source: Path,
    dest: Path,
    sentences: Sequence[SentenceLike],
    tracks: Sequence[OverlayTrack],
    *,
    modified: datetime,
) -> OverlayReport:
    """Write the read-aloud EPUB for ``source`` to ``dest`` (which must not be ``source``)."""
    if not tracks:
        raise OverlayError("the work has no audio")
    for t in tracks:
        if t.media_type not in AUDIO_EXTENSIONS:
            raise OverlayError(
                f"Audio in {t.media_type} can't be embedded in an EPUB; "
                "EPUB 3 readers need MP3 or AAC (M4A) tracks."
            )
    report = OverlayReport()
    with zipfile.ZipFile(source) as zin:
        pkg = _read_package(zin)
        if any(it.get("media-overlay") for it in pkg.items.values()):
            raise OverlayError("the EPUB already has media overlays")
        epub2 = not pkg.version.startswith("3")
        if epub2:
            report.upgraded_from = pkg.version

        used_ids = {
            el.get("id")
            for el in pkg.tree.getroot().iter()
            if isinstance(el.tag, str) and el.get("id")
        }
        used_ids |= set(pkg.items)
        existing_names = set(zin.namelist())

        def new_name(rel: str) -> str:
            name = posixpath.join(pkg.opf_dir, rel)
            if name in existing_names:
                raise OverlayError(f"the EPUB already contains {name}")
            existing_names.add(name)
            return name

        # Audio items, in play order.
        audio_names = []
        for i, t in enumerate(tracks):
            name = new_name(f"lectern/audio/track-{i:03d}.{AUDIO_EXTENSIONS[t.media_type]}")
            audio_names.append(name)
            item_id = _unique(f"lectern-audio-{i:03d}", used_ids)
            _add_item(pkg, item_id, name, t.media_type)
        css_name = new_name("lectern/read-aloud.css")
        _add_item(pkg, _unique("lectern-read-aloud-css", used_ids), css_name, "text/css")

        by_spine: dict[int, list[SentenceLike]] = {}
        for s in sentences:
            if s.spine_index >= len(pkg.spine_ids):
                raise OverlayError(f"sentence {s.n} points past the end of the spine")
            by_spine.setdefault(s.spine_index, []).append(s)

        replaced: dict[str, bytes] = {}
        added: list[tuple[str, bytes]] = []
        overlay_ms: list[tuple[str, int]] = []
        offsets = [t.offset_s for t in tracks]
        for spine_index in sorted(by_spine):
            item_id = pkg.spine_ids[spine_index]
            item = pkg.items[item_id]
            if item.get("media-type") != XHTML_TYPE:
                raise OverlayError(f"spine item {spine_index} is not an XHTML document")
            doc_name = pkg.names[item_id]
            try:
                xhtml = zin.read(doc_name)
            except KeyError as exc:
                raise OverlayError(f"{doc_name} is missing from the EPUB") from exc
            try:
                st = SpineText(xhtml)
            except ValueError as exc:
                raise OverlayError(f"spine item {spine_index} could not be read") from exc
            doc_ids = {
                el.get("id")
                for el in st.tree.getroot().iter()
                if isinstance(el.tag, str) and el.get("id")
            }
            prefix = "lectern-s" if any(_SPAN_ID.match(i) for i in doc_ids) else "s"
            try:
                parts = wrap_sentences(st, by_spine[spine_index], prefix)
            except ValueError as exc:
                raise OverlayError(f"spine item {spine_index}: {exc}") from exc
            report.spans += sum(len(p) for p in parts.values())

            smil_name = new_name(f"lectern/smil/{spine_index:04d}.smil")
            smil_dir = posixpath.dirname(smil_name)
            doc_href = _href(smil_dir, doc_name)
            pars, total_ms = _pars(
                by_spine[spine_index], parts, tracks, offsets, audio_names, smil_dir, doc_href
            )
            _add_stylesheet(st.tree, _href(posixpath.dirname(doc_name), css_name))
            if epub2:
                _modernise_xhtml(st.tree)
            replaced[doc_name] = _serialize_xhtml(st.tree, html5_doctype=epub2)
            if not pars:
                existing_names.discard(smil_name)
                continue
            smil = _smil(doc_href, pars)
            mo_id = _unique(f"lectern-mo-{spine_index:04d}", used_ids)
            _add_item(pkg, mo_id, smil_name, SMIL_TYPE)
            item.set("media-overlay", mo_id)
            added.append((smil_name, smil))
            overlay_ms.append((mo_id, total_ms))
            report.pars += len(pars)
            report.smil[smil_name] = doc_name

        if not overlay_ms:
            raise OverlayError("No sentence has audio to play yet.")
        report.overlays = len(overlay_ms)

        if epub2:
            # Every other XHTML document must also be valid in an EPUB 3 package.
            for item_id, it in pkg.items.items():
                name = pkg.names[item_id]
                if (
                    it.get("media-type") == XHTML_TYPE
                    and name not in replaced
                    and name in zin.namelist()
                ):
                    tree = parse_xhtml(zin.read(name))
                    _modernise_xhtml(tree)
                    replaced[name] = _serialize_xhtml(tree, html5_doctype=True)
            _upgrade_metadata(pkg, used_ids)
            pkg.tree.getroot().set("version", "3.0")
            if not any("nav" in (it.get("properties") or "").split() for it in pkg.items.values()):
                nav_name = new_name("lectern-nav.xhtml")
                title = (
                    " ".join("".join(pkg.metadata.find("dc:title", NS).itertext()).split())
                    if pkg.metadata.find("dc:title", NS) is not None
                    else "Contents"
                )
                added.append((nav_name, _nav_document(zin, pkg, nav_name, title)))
                _add_item(pkg, _unique("lectern-nav", used_ids), nav_name, XHTML_TYPE, "nav")

        for m in pkg.metadata.findall("opf:meta", NS):
            if m.get("property") in ("dcterms:modified", "media:duration", "media:active-class"):
                pkg.metadata.remove(m)
        _meta(pkg, "dcterms:modified", modified.strftime("%Y-%m-%dT%H:%M:%SZ"))
        total_ms = 0
        for mo_id, ms in overlay_ms:
            _meta(pkg, "media:duration", _clock(ms), mo_id)
            total_ms += ms
        _meta(pkg, "media:duration", _clock(total_ms))
        _meta(pkg, "media:active-class", ACTIVE_CLASS)
        report.duration_s = total_ms / 1000
        replaced[pkg.opf_name] = etree.tostring(pkg.tree, encoding="utf-8", xml_declaration=True)
        added.append((css_name, STYLESHEET.encode()))

        _write_zip(zin, dest, replaced, added, list(zip(audio_names, tracks, strict=True)))
    verify_read_aloud_epub(dest)
    return report


def _add_item(
    pkg: _Package, item_id: str, name: str, media_type: str, props: str | None = None
) -> None:
    attrs = {"id": item_id, "href": _href(pkg.opf_dir, name), "media-type": media_type}
    if props:
        attrs["properties"] = props
    el = etree.SubElement(pkg.manifest, f"{{{OPF_NS}}}item", attrs)
    el.tail = "\n  "
    pkg.items[item_id] = el
    pkg.names[item_id] = name


def _pars(
    sentences: Sequence[SentenceLike],
    parts: dict[int, list[Part]],
    tracks: Sequence[OverlayTrack],
    offsets: list[float],
    audio_names: list[str],
    smil_dir: str,
    doc_href: str,
) -> tuple[list[tuple[str, str, int, int]], int]:
    """``(fragment, audio href, clip begin ms, clip end ms)`` per part, and their total."""
    out = []
    total = 0
    for s in sorted(sentences, key=lambda s: s.n):
        if not s.matched or s.audio_start_s is None or s.audio_end_s is None:
            continue
        i = max(0, bisect_right(offsets, s.audio_start_s) - 1)
        track = tracks[i]
        # A clip cannot cross into the next file; the rare sentence that does is cut there.
        begin = max(0.0, s.audio_start_s - track.offset_s)
        end = min(s.audio_end_s - track.offset_s, track.duration_s)
        sentence_parts = parts.get(s.n, [])
        weight = sum(p.length for p in sentence_parts) or 1
        done = 0
        for p in sentence_parts:
            a = round((begin + (end - begin) * done / weight) * 1000)
            done += p.length
            b = round((begin + (end - begin) * done / weight) * 1000)
            if b - a < _MIN_CLIP_MS:
                continue
            out.append((p.id, _href(smil_dir, audio_names[i]), a, b))
            total += b - a
    return out, total


def _smil(doc_href: str, pars: list[tuple[str, str, int, int]]) -> bytes:
    root = etree.Element(
        f"{{{SMIL_NS}}}smil", {"version": "3.0"}, nsmap={None: SMIL_NS, "epub": OPS_NS}
    )
    body = etree.SubElement(root, f"{{{SMIL_NS}}}body", {f"{{{OPS_NS}}}textref": doc_href})
    for i, (frag, audio, a, b) in enumerate(pars):
        par = etree.SubElement(body, f"{{{SMIL_NS}}}par", {"id": f"p{i}"})
        etree.SubElement(par, f"{{{SMIL_NS}}}text", {"src": f"{doc_href}#{frag}"})
        etree.SubElement(
            par, f"{{{SMIL_NS}}}audio",
            {"src": audio, "clipBegin": f"{a / 1000:.3f}s", "clipEnd": f"{b / 1000:.3f}s"},
        )  # fmt: skip
    return etree.tostring(root, encoding="utf-8", xml_declaration=True, pretty_print=True)


def _write_zip(
    zin: zipfile.ZipFile,
    dest: Path,
    replaced: dict[str, bytes],
    added: list[tuple[str, bytes]],
    audio: list[tuple[str, OverlayTrack]],
) -> None:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
        mt = zipfile.ZipInfo("mimetype", date_time=_FIXED_ZIP_TIME)
        zout.writestr(mt, b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for info in zin.infolist():
            if info.filename == "mimetype" or info.is_dir():
                continue
            out = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            out.compress_type = zipfile.ZIP_DEFLATED
            out.external_attr = info.external_attr
            if info.filename in replaced:
                zout.writestr(out, replaced[info.filename])
                continue
            out.compress_type = info.compress_type
            out.file_size = info.file_size
            with (
                zin.open(info) as src,
                zout.open(out, "w", force_zip64=info.file_size > 2**31) as dst,
            ):
                shutil.copyfileobj(src, dst, 1 << 20)
        for name, data in added:
            zout.writestr(zipfile.ZipInfo(name, date_time=_FIXED_ZIP_TIME), data)
        for name, track in audio:
            info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_STORED  # already compressed media
            info.file_size = track.path.stat().st_size
            with (
                track.path.open("rb") as src,
                zout.open(info, "w", force_zip64=info.file_size > 2**31) as dst,
            ):
                shutil.copyfileobj(src, dst, 1 << 20)


# --- integrity check --------------------------------------------------------------------------


def verify_read_aloud_epub(path: Path) -> None:
    """Structural checks on a built file (epubcheck does the full validation in CI):
    a valid zip with ``mimetype`` first and stored, every manifest item present, every SMIL
    ``text`` fragment present in its document, every ``audio`` present, clips well-formed."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise OverlayError("output is not a zip file") from exc
    with zf:
        infos = zf.infolist()
        if (
            not infos
            or infos[0].filename != "mimetype"
            or infos[0].compress_type != zipfile.ZIP_STORED
        ):
            raise OverlayError("mimetype must be the first, uncompressed entry")
        if zf.read("mimetype") != b"application/epub+zip":
            raise OverlayError("wrong mimetype")
        bad = zf.testzip()
        if bad is not None:
            raise OverlayError(f"corrupt entry {bad}")
        pkg = _read_package(zf)
        names = set(zf.namelist())
        for item_id, name in pkg.names.items():
            if name not in names:
                raise OverlayError(f"manifest item {item_id} ({name}) is missing")
        ids_cache: dict[str, set[str]] = {}
        for item_id, it in pkg.items.items():
            if it.get("media-type") != SMIL_TYPE:
                continue
            smil_name = pkg.names[item_id]
            smil = etree.fromstring(zf.read(smil_name), _PARSER)
            smil_dir = posixpath.dirname(smil_name)
            for par in smil.iter(f"{{{SMIL_NS}}}par"):
                text = par.find("smil:text", NS)
                audio = par.find("smil:audio", NS)
                url, frag = urldefrag(text.get("src"))
                doc = posixpath.normpath(posixpath.join(smil_dir, unquote(url)))
                if doc not in ids_cache:
                    root = etree.fromstring(zf.read(doc), _PARSER)
                    ids_cache[doc] = {
                        e.get("id") for e in root.iter() if isinstance(e.tag, str) and e.get("id")
                    }
                if frag not in ids_cache[doc]:
                    raise OverlayError(f"{smil_name}: #{frag} is not in {doc}")
                if (
                    posixpath.normpath(posixpath.join(smil_dir, unquote(audio.get("src"))))
                    not in names
                ):
                    raise OverlayError(f"{smil_name}: audio {audio.get('src')} is missing")
                b, e = (float(audio.get(k).rstrip("s")) for k in ("clipBegin", "clipEnd"))
                if not 0 <= b < e:
                    raise OverlayError(f"{smil_name}: bad clip {b}-{e}")
        # Every XHTML document still parses strictly and keeps its text.
        for item_id, it in pkg.items.items():
            if it.get("media-type") == XHTML_TYPE:
                try:
                    etree.fromstring(zf.read(pkg.names[item_id]), _PARSER)
                except etree.XMLSyntaxError as exc:
                    raise OverlayError(f"{pkg.names[item_id]} is not well-formed: {exc}") from exc


def spine_texts(path: Path) -> list[str]:
    """Normalised text of every spine item (for comparing a source and its read-aloud copy)."""
    with zipfile.ZipFile(path) as zf:
        pkg = _read_package(zf)
        return [normalize_spine_text(zf.read(pkg.names[i])) for i in pkg.spine_ids]
