"""DRM-free MOBI and AZW3 (KF8) books, converted to EPUB at ingest; DRM-protected ones refused.

Lectern does not store Kindle formats. A MOBI or AZW3 is read once, turned into an EPUB 3,
and that EPUB is what is filed (``book.epub``), matched, read, aligned, and served. The
original is not kept (docs/kindle.md says why).

Two steps, so a DRM-protected file never reaches a parser:

1. :func:`read_header`, in the server: the PalmDB header (``BOOKMOBI`` at offset 60), the
   record list, and each MOBI header record (record 0, and the KF8 header after the
   ``BOUNDARY`` record of a joint MOBI7+KF8 file), read with bounded reads and plain
   ``struct`` unpacking. A non-zero encryption type in any of them is DRM: the file is
   refused (``drm_protected``) with no attempt to read past the header. EXTH metadata
   (authors, ASIN, ISBN, language, ...) comes from here too.
2. :func:`to_epub`: a child process (``python -I -m lectern.ingest.kindle``) with hard limits
   on itself (address space, CPU seconds, file size, no core dumps, ``nice``) and a wall-clock
   timeout in the parent, the way PDFs are read (lectern.ingest.pdf). It unpacks the book with
   KindleUnpack (the ``mobi`` package: pure Python, no native code) and builds the EPUB here:
   every XHTML document is rebuilt from an allowlist of elements and attributes (no scripts,
   forms, frames, event handlers, remote resources, or meta refreshes), internal links are
   checked against the ids that exist, and the package document is written from the EXTH
   metadata. The zip is deterministic (fixed timestamps and order), so the same MOBI converts
   to the same bytes and adding it twice is caught as a duplicate.

No DRM removal of any kind, and no advice on it: a protected file is refused, full stop.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import posixpath
import re
import struct
import subprocess
import sys
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lectern.ingest.formats import PARSER_SLOTS, FormatError, ProbeBusy

TIMEOUT_S = 90
CPU_LIMIT_S = 60
MEMORY_LIMIT_BYTES = 1536 * 1024 * 1024
# Largest single file the child may write (an unpacked image, the HTML, the EPUB).
FILE_LIMIT_BYTES = 768 * 1024 * 1024
BUSY_WAIT_S = 10.0
MAX_RECORDS = 65_535  # the PalmDB record count is 16 bits
MAX_HEADER_RECORD = 64 * 1024  # bytes of a MOBI header record read in the server
MAX_DOCUMENTS = 5_000
MAX_FIELD = 2048

DRM_MESSAGE = "This book is DRM-protected; Lectern can only add DRM-free books."
_BOUNDARY = b"BOUNDARY"
_CODECS = {65001: "utf-8", 1252: "cp1252"}


# --- the header (in the server) ---------------------------------------------------------------


@dataclass
class Header:
    """What the PalmDB and MOBI headers say, before anything is unpacked."""

    title: str
    encoding: str
    kf8: bool
    exth: dict[int, list[bytes]] = field(default_factory=dict)

    def text(self, code: int) -> list[str]:
        out = []
        for raw in self.exth.get(code, []):
            s = raw.decode(self.encoding, "replace").replace("\x00", "")
            s = " ".join(s.split())[:MAX_FIELD]
            if s:
                out.append(s)
        return out

    def first(self, code: int) -> str | None:
        vals = self.text(code)
        return vals[0] if vals else None


def _bad(detail: str) -> FormatError:
    return FormatError("not_mobi", detail)


def _exth(record: bytes, header_len: int) -> dict[int, list[bytes]]:
    out: dict[int, list[bytes]] = {}
    at = 16 + header_len
    if record[at : at + 4] != b"EXTH" or len(record) < at + 12:
        return out
    length, count = struct.unpack_from(">II", record, at + 4)
    end = min(len(record), at + length)
    pos = at + 12
    for _ in range(min(count, 1024)):
        if pos + 8 > end:
            break
        kind, size = struct.unpack_from(">II", record, pos)
        if size < 8 or pos + size > end:
            break
        out.setdefault(kind, []).append(record[pos + 8 : pos + size])
        pos += size
    return out


def read_header(path: Path) -> Header:
    """Check the PalmDB/MOBI headers and refuse DRM; raises :class:`FormatError`
    (``not_mobi``, ``drm_protected``). Reads a bounded number of bytes whatever the file says."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(78)
            if len(head) < 78 or head[60:68] != b"BOOKMOBI":
                raise _bad("no BOOKMOBI header")
            (n,) = struct.unpack_from(">H", head, 76)
            if not 1 <= n <= MAX_RECORDS:
                raise _bad("no records")
            table = fh.read(8 * n)
            if len(table) < 8 * n:
                raise _bad("truncated record list")
            offsets = [struct.unpack_from(">I", table, 8 * i)[0] for i in range(n)] + [size]
            if offsets[0] < 78 + 8 * n or any(
                b < a or b > size for a, b in zip(offsets, offsets[1:], strict=False)
            ):
                raise _bad("record offsets out of order")

            def record(i: int) -> bytes:
                fh.seek(offsets[i])
                return fh.read(min(offsets[i + 1] - offsets[i], MAX_HEADER_RECORD))

            headers = [0]
            for i in range(n - 1):  # the KF8 half of a joint file follows a BOUNDARY record
                if offsets[i + 1] - offsets[i] == len(_BOUNDARY) and record(i) == _BOUNDARY:
                    headers.append(i + 1)
                    break
            records = [record(i) for i in headers]
    except OSError as exc:
        raise _bad(str(exc)) from exc
    for rec in records:
        if len(rec) < 24 or rec[16:20] != b"MOBI":
            raise _bad("no MOBI header")
    for rec in records:
        # PalmDOC header offset 12: 0 = none, 1 = old Mobipocket, 2 = Mobipocket/Kindle DRM.
        if struct.unpack_from(">H", rec, 12)[0] != 0:
            raise FormatError("drm_protected", DRM_MESSAGE)
    main = records[0]
    header_len, codepage = struct.unpack_from(">II", main, 20)[0], 0
    if len(main) >= 32:
        codepage = struct.unpack_from(">I", main, 28)[0]
    encoding = _CODECS.get(codepage, "utf-8")
    version = struct.unpack_from(">I", main, 36)[0] if len(main) >= 40 else 0
    exth = _exth(main, header_len) if len(main) >= 132 else {}
    if len(records) > 1:  # KF8 metadata wins in a joint file, as on a Kindle
        kf8 = records[1]
        exth = {**exth, **_exth(kf8, struct.unpack_from(">I", kf8, 20)[0])}
    title = ""
    if len(main) >= 92:
        off, length = struct.unpack_from(">II", main, 84)
        title = main[off : off + min(length, MAX_FIELD)].decode(encoding, "replace")
    if not title.strip():
        title = head[:32].split(b"\x00")[0].decode("latin-1")
    return Header(
        title=" ".join(title.replace("\x00", "").split())[:512],
        encoding=encoding,
        kf8=version >= 8 or len(records) > 1,
        exth=exth,
    )


# --- conversion (parent side) -----------------------------------------------------------------


def to_epub(path: Path, dest: Path) -> None:
    """Convert ``path`` (MOBI or AZW3) to an EPUB at ``dest``. Raises :class:`FormatError`
    (``not_mobi``, ``drm_protected``, ``mobi_too_complex``), or :class:`ProbeBusy`
    (``convert_busy``) when no parser slot frees up within :data:`BUSY_WAIT_S`."""
    read_header(path)  # not_mobi / drm_protected before any parser runs
    if not PARSER_SLOTS.acquire(timeout=BUSY_WAIT_S):
        raise ProbeBusy("convert_busy")
    try:
        _convert(path, dest)
    finally:
        PARSER_SLOTS.release()


def _convert(path: Path, dest: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="lectern-kindle-") as tmp:
        out = Path(tmp) / "book.epub"
        try:
            res = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "lectern.ingest.kindle",
                    str(path.resolve()),
                    str(out),
                ],
                capture_output=True,
                timeout=TIMEOUT_S,
                check=False,
                env={"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"},
                cwd=tmp,
            )
        except subprocess.TimeoutExpired:
            raise FormatError("mobi_too_complex", "timed out") from None
        try:
            result = json.loads(res.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        except (ValueError, IndexError):
            # killed by a limit (memory, CPU, file size) or crashed
            raise FormatError("mobi_too_complex", f"child exited {res.returncode}") from None
        if not result.get("ok"):
            raise FormatError(result.get("error") or "not_mobi", result.get("detail") or "")
        if not out.is_file():
            raise FormatError("not_mobi", "no EPUB written")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_dest = dest.with_name(f".{dest.name}.part")
        try:
            with out.open("rb") as src, tmp_dest.open("wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
            os.replace(tmp_dest, dest)
        finally:
            tmp_dest.unlink(missing_ok=True)


# --- the child: unpack, then build the EPUB ---------------------------------------------------

XHTML_NS = "http://www.w3.org/1999/xhtml"
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
EPUB_NS = "http://www.idpf.org/2007/ops"
XML_NS = "http://www.w3.org/XML/1998/namespace"
OPF_NS = "http://www.idpf.org/2007/opf"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
MODIFIED = "2000-01-01T00:00:00Z"

#: Elements kept as they are (HTML5 flow and phrasing content an ebook uses).
KEEP = frozenset(
    "a abbr address article aside b bdi bdo blockquote br caption cite code col colgroup dd "
    "del details dfn div dl dt em figcaption figure footer h1 h2 h3 h4 h5 h6 header hgroup hr "
    "i img ins kbd li main mark nav ol p pre q rp rt ruby s samp section small span strong sub "
    "summary sup table tbody td tfoot th thead time tr u ul var wbr".split()
)
#: Old or Mobipocket markup, renamed to its HTML5 equivalent (with a style where needed).
RENAME = {
    "font": ("span", ""),
    "center": ("div", "text-align: center"),
    "big": ("span", "font-size: larger"),
    "tt": ("code", ""),
    "strike": ("s", ""),
    "blink": ("span", ""),
    "marquee": ("span", ""),
    "listing": ("pre", ""),
    "xmp": ("pre", ""),
    "plaintext": ("pre", ""),
    "nobr": ("span", "white-space: nowrap"),
    "dir": ("ul", ""),
    "menu": ("ul", ""),
}
#: Dropped with everything inside them.
DROP = frozenset(
    "script style noscript template object embed applet iframe frame frameset form input "
    "button select textarea option optgroup audio video source track canvas map area head "
    "meta link title base guide reference math".split()
)
VOID = frozenset("br hr img wbr col".split())
GLOBAL_ATTRS = frozenset("id class style title dir role".split())
ATTRS = {
    "a": {"href"},
    "img": {"src", "alt", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "col": {"span"},
    "colgroup": {"span"},
    "ol": {"start", "reversed", "type"},
    "li": {"value"},
    "blockquote": {"cite"},
    "q": {"cite"},
    "del": {"cite", "datetime"},
    "ins": {"cite", "datetime"},
    "time": {"datetime"},
    "nav": {"hidden"},
    "details": {"open"},
}
NUMERIC = frozenset({"width", "height", "colspan", "rowspan", "span", "start", "value"})
SVG_KEEP = frozenset(
    "svg g image rect circle ellipse line polyline polygon path text tspan title desc defs "
    "lineargradient radialgradient stop use".split()
)
SVG_ATTRS = frozenset(
    "width height viewbox preserveaspectratio version x y x1 y1 x2 y2 cx cy r rx ry d points "
    "fill stroke stroke-width transform opacity id class style offset stop-color font-size "
    "font-family text-anchor".split()
)
_BAD_STYLE = re.compile(r"url\s*\(|expression|javascript|behaviou?r|-moz-binding|@import", re.I)
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*$")
_LANG = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$")
_ASIN = re.compile(r"^[A-Z0-9]{10}$")
_DATE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")
_FONT_SIZES = {
    "1": "x-small",
    "2": "small",
    "3": "medium",
    "4": "large",
    "5": "x-large",
    "6": "xx-large",
    "7": "xx-large",
}
_COLOR = re.compile(r"^(#[0-9A-Fa-f]{3}|#[0-9A-Fa-f]{6}|[A-Za-z]{3,20})$")
_LENGTH = re.compile(r"^-?\d+(\.\d+)?(em|ex|pt|px|%)?$")
MEDIA_TYPES = {
    ".xhtml": "application/xhtml+xml",
    ".html": "application/xhtml+xml",
    ".htm": "application/xhtml+xml",
    ".css": "text/css",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/svg+xml", "image/webp"})


def _local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def _ns(tag) -> str:
    return tag[1:].split("}", 1)[0] if isinstance(tag, str) and tag.startswith("{") else ""


def _parser(kind: str):
    from lxml import etree, html

    if kind == "xml":
        return etree.XMLParser(
            resolve_entities=False, no_network=True, huge_tree=False, remove_comments=True
        )
    return html.HTMLParser(no_network=True, remove_comments=True, remove_pis=True)


def _parse_document(data: bytes, encoding: str | None = None, html_only: bool = False):
    """An XHTML (KF8) or Mobipocket HTML (MOBI7) document as an lxml tree."""
    from lxml import etree, html

    if not html_only:
        try:
            return etree.fromstring(data, _parser("xml"))
        except etree.XMLSyntaxError:
            pass
    parser = html.HTMLParser(
        encoding=encoding, no_network=True, remove_comments=True, remove_pis=True
    )
    return html.document_fromstring(data, parser=parser)


@dataclass
class Doc:
    """One XHTML document of the EPUB being built."""

    name: str  # path inside OEBPS/
    body: object  # an XHTML <body> element (lxml), sanitized
    title: str
    lang: str | None = None
    stylesheets: list[str] = field(default_factory=list)  # names inside OEBPS/
    styles: list[str] = field(default_factory=list)  # <style> text from the head
    ids: set[str] = field(default_factory=set)


class Book:
    """The pieces of the EPUB: documents, other resources, spine, and table of contents."""

    def __init__(self) -> None:
        self.docs: dict[str, Doc] = {}
        self.resources: dict[str, tuple[bytes, str]] = {}  # name -> (bytes, media type)
        self.spine: list[tuple[str, bool]] = []  # (doc name, linear)
        self.toc: list[tuple[str, str]] = []  # (label, href relative to OEBPS/)
        self.cover: str | None = None
        self.nav_doc: str | None = None

    def has(self, name: str) -> bool:
        return name in self.docs or name in self.resources


def _style_from(el, mobi: bool) -> list[str]:
    """Presentational attributes as CSS (MOBI7 markup leans on them)."""
    out = []
    tag = _local(el.tag)
    align = (el.get("align") or "").strip().lower()
    if align in {"left", "right", "center", "justify"} and tag != "img":
        out.append(f"text-align: {align}")
    if mobi and tag in {"p", "div", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}:
        height, width = (el.get("height") or "").strip(), (el.get("width") or "").strip()
        if _LENGTH.match(height):
            out.append(f"margin-top: {height if not height[-1].isdigit() else height + 'px'}")
        if _LENGTH.match(width):
            out.append(f"text-indent: {width if not width[-1].isdigit() else width + 'px'}")
    if tag == "font":
        size = (el.get("size") or "").strip()
        if size in _FONT_SIZES:
            out.append(f"font-size: {_FONT_SIZES[size]}")
        elif size.startswith(("+", "-")) and size[1:].isdigit():
            out.append("font-size: larger" if size[0] == "+" else "font-size: smaller")
        color = (el.get("color") or "").strip()
        if _COLOR.match(color):
            out.append(f"color: {color}")
    return out


class Sanitizer:
    """Rebuild a document's body from the allowlist above, in the XHTML namespace."""

    def __init__(self, mobi: bool) -> None:
        self.mobi = mobi

    def body(self, root):
        from lxml import etree

        src = next((e for e in root.iter() if _local(e.tag) == "body"), None)
        out = etree.Element(f"{{{XHTML_NS}}}body", nsmap={None: XHTML_NS, "epub": EPUB_NS})
        if src is None:
            return out
        self._attrs(src, out, "body")
        self._children(src, out)
        _fix_nesting(out)
        return out

    def _children(self, src, dst) -> None:
        from lxml import etree

        if src.text:
            self._text(dst, src.text)
        for child in src:
            tag = _local(child.tag)
            if not isinstance(child.tag, str):  # comment or PI
                pass
            elif _ns(child.tag) == SVG_NS or tag == "svg":
                svg = self._svg(child)
                if svg is not None:
                    dst.append(svg)
            elif tag == "pagebreak" or tag == "mbp:pagebreak":
                el = etree.SubElement(dst, f"{{{XHTML_NS}}}div")
                el.set("class", "mbp-pagebreak")
            elif tag in DROP:
                pass
            elif tag in KEEP or tag in RENAME:
                name, style = RENAME.get(tag, (tag, ""))
                el = etree.SubElement(dst, f"{{{XHTML_NS}}}{name}")
                self._attrs(child, el, tag, extra=style)
                if name not in VOID:
                    self._children(child, el)
            else:  # unknown (mbp:nu, html, o:p, ...): keep what is inside
                self._children(child, dst)
            if child.tail:
                self._text(dst, child.tail)

    @staticmethod
    def _text(dst, text: str) -> None:
        text = "".join(c for c in text if c in "\t\n\r" or ord(c) >= 0x20)
        if not text:
            return
        if len(dst):
            dst[-1].tail = (dst[-1].tail or "") + text
        else:
            dst.text = (dst.text or "") + text

    def _attrs(self, src, dst, tag: str, extra: str = "") -> None:
        allowed = GLOBAL_ATTRS | ATTRS.get(tag, set())
        styles = [s for s in [extra, *_style_from(src, self.mobi)] if s]
        for key, value in src.attrib.items():
            name, ns = _local(key), _ns(key)
            value = " ".join(str(value).split())
            if ns == EPUB_NS and name == "type":
                dst.set(f"{{{EPUB_NS}}}type", value[:256])
            elif ns == XML_NS and name == "lang" or (not ns and name == "lang"):
                if _LANG.match(value):
                    dst.set("lang", value)
                    dst.set(f"{{{XML_NS}}}lang", value)
            elif name.startswith("aria-") and not ns:
                dst.set(name, value[:512])
            elif tag == "a" and name == "name" and not ns and src.get("id") is None:
                if value and " " not in value:  # old-style anchor
                    dst.set("id", value[:256])
            elif ns or name not in allowed or name.startswith("on"):
                continue
            elif name == "style":
                if not _BAD_STYLE.search(value):
                    styles.append(value.rstrip("; "))
            elif name in NUMERIC:
                parent = dst.getparent()
                if name == "value" and (parent is None or _local(parent.tag) != "ol"):
                    continue  # li value is only valid in an ordered list
                if value.isdigit() and len(value) <= 6:
                    dst.set(name, value)
            elif name == "id":
                if value and " " not in value:
                    dst.set("id", value[:256])
            elif name == "hidden":
                dst.set("hidden", "hidden")
            elif name in {"reversed", "open"}:
                dst.set(name, name)
            else:
                dst.set(name, value[:2048])
        if tag == "img" and dst.get("alt") is None:
            dst.set("alt", "")
        if styles:
            dst.set("style", "; ".join(styles)[:2048])

    def _svg(self, src):
        from lxml import etree

        tag = _local(src.tag)
        if tag not in SVG_KEEP:
            return None
        el = etree.Element(
            f"{{{SVG_NS}}}{_SVG_CASE.get(tag, tag)}", nsmap={None: SVG_NS, "xlink": XLINK_NS}
        )
        for key, value in src.attrib.items():
            name, ns = _local(key), _ns(key)
            if name in ("href", "xlink:href") and ns in (XLINK_NS, ""):
                el.set(f"{{{XLINK_NS}}}href", str(value))  # checked when links are resolved
            elif not ns and name in SVG_ATTRS and not _BAD_STYLE.search(str(value)):
                el.set(_SVG_CASE.get(name, name), str(value)[:4096])
        if src.text and tag in {"text", "tspan", "title", "desc"}:
            el.text = src.text
        for child in src:
            sub = self._svg(child)
            if sub is not None:
                el.append(sub)
            if child.tail and tag in {"text", "tspan"}:
                (el[-1] if len(el) else el).tail = child.tail
        return el


#: Elements whose content may only be phrasing content.
PHRASING_PARENTS = frozenset(
    "p h1 h2 h3 h4 h5 h6 pre span b i em strong u s small code sub sup abbr cite dfn kbd mark "
    "q samp var bdi bdo time dt".split()
)
PHRASING = frozenset(
    "a abbr b bdi bdo br cite code del dfn em i img ins kbd mark q rp rt ruby s samp small "
    "span strong sub sup time u var wbr".split()
)


def _fix_nesting(body) -> None:
    """Make old markup valid: what a paragraph, heading, or inline element holds becomes
    phrasing content (a nested block turns into a ``display: block`` span), links do not nest,
    and lists hold only list items. Mobipocket HTML (``<font><p>..</p></font>``) needs this."""
    from lxml import etree

    for el in list(body.iter(f"{{{XHTML_NS}}}*")):
        tag = _local(el.tag)
        if tag in PHRASING_PARENTS or tag == "a":
            for sub in el.iterdescendants(f"{{{XHTML_NS}}}*"):
                name = _local(sub.tag)
                if name in PHRASING and not (name == "a" and tag == "a"):
                    continue
                style = [] if name in PHRASING else ["display: block"]
                if sub.get("style"):
                    style.append(sub.get("style"))
                for key in list(sub.attrib):
                    if _local(key) not in GLOBAL_ATTRS | {"lang"} and _ns(key) != EPUB_NS:
                        del sub.attrib[key]
                sub.tag = f"{{{XHTML_NS}}}span"
                if style:
                    sub.set("style", "; ".join(style))
        elif tag in {"ul", "ol", "dl"}:
            item = "dd" if tag == "dl" else "li"
            allowed = {"dt", "dd"} if tag == "dl" else {"li"}
            if (el.text or "").strip():
                first = etree.Element(f"{{{XHTML_NS}}}{item}")
                first.text = el.text
                el.insert(0, first)
            el.text = None
            for child in list(el):
                if _local(child.tag) not in allowed:
                    wrapper = etree.Element(f"{{{XHTML_NS}}}{item}")
                    child.addprevious(wrapper)
                    wrapper.append(child)  # the child's tail moves inside the item with it
            for child in el:
                if (child.tail or "").strip():
                    Sanitizer._text(child, child.tail)
                child.tail = None


_SVG_CASE = {
    "viewbox": "viewBox",
    "preserveaspectratio": "preserveAspectRatio",
    "lineargradient": "linearGradient",
    "radialgradient": "radialGradient",
}


def _head_info(root, base: str, book: Book, kind: str) -> tuple[str, list[str], list[str]]:
    """Title, linked stylesheets that exist in the book, and <style> text, from a head."""
    title, sheets, styles = "", [], []
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "body":
            break
        if tag == "title" and not title:
            title = " ".join("".join(el.itertext()).split())[:512]
        elif tag == "link" and "stylesheet" in (el.get("rel") or "").lower():
            target = _resolve(base, el.get("href") or "")
            if target and target[0] in book.resources:
                sheets.append(target[0])
        elif tag == "style" and kind == "kf8":
            css = "".join(el.itertext())
            if not _BAD_STYLE.search(css):
                styles.append(css)
    return title, sheets, styles


def _resolve(base: str, href: str) -> tuple[str, str] | None:
    """(name inside OEBPS/, fragment) for a relative link, or None for anything else."""
    href = href.strip()
    if not href:
        return None
    parts = urlsplit(href)
    if parts.scheme or parts.netloc:
        return None
    path = unquote(parts.path)
    if path:
        name = posixpath.normpath(posixpath.join(posixpath.dirname(base), path))
        if name.startswith("../") or name == ".." or name.startswith("/"):
            return None
    else:
        name = base
    return name, unquote(parts.fragment)


def _relative(base: str, name: str, fragment: str = "") -> str:
    rel = posixpath.relpath(name, posixpath.dirname(base) or ".")
    return rel + (f"#{fragment}" if fragment else "")


def _fix_links(book: Book) -> None:
    """Point links at documents and ids that exist; drop images and links that do not."""
    for doc in book.docs.values():
        for el in list(doc.body.iter()):
            tag, ns = _local(el.tag), _ns(el.tag)
            if ns == XHTML_NS and tag == "a" and el.get("href") is not None:
                href = el.get("href")
                scheme = urlsplit(href).scheme.lower()
                if scheme in {"http", "https", "mailto"}:
                    continue
                target = _resolve(doc.name, href)
                if target and target[0] in book.docs:
                    name, frag = target
                    if frag and frag not in book.docs[name].ids:
                        frag = ""
                    el.set("href", _relative(doc.name, name, frag))
                else:
                    del el.attrib["href"]
            elif ns == XHTML_NS and tag == "img":
                target = _resolve(doc.name, el.get("src") or "")
                res = book.resources.get(target[0]) if target else None
                if res and res[1] in IMAGE_TYPES:
                    el.set("src", _relative(doc.name, target[0]))
                else:
                    _remove(el)
            elif ns == SVG_NS and tag == "image" or (ns == SVG_NS and tag == "use"):
                key = f"{{{XLINK_NS}}}href"
                target = _resolve(doc.name, el.get(key) or "")
                res = book.resources.get(target[0]) if target else None
                if tag == "image" and res and res[1] in IMAGE_TYPES:
                    el.set(key, _relative(doc.name, target[0]))
                elif tag == "use" and target and target[0] == doc.name and target[1]:
                    el.set(key, f"#{target[1]}")
                else:
                    _remove(el)


def _remove(el) -> None:
    parent = el.getparent()
    if parent is None:
        return
    if el.tail:
        prev = el.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + el.tail
        else:
            parent.text = (parent.text or "") + el.tail
    parent.remove(el)


def _dedupe_ids(doc: Doc) -> None:
    seen: set[str] = set()
    for el in doc.body.iter():
        i = el.get("id")
        if i is None:
            continue
        if i in seen:
            del el.attrib["id"]
        else:
            seen.add(i)
    doc.ids = seen


def _read_opf(path: Path):
    from lxml import etree

    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        return None
    try:
        return etree.fromstring(path.read_bytes(), _parser("xml"))
    except etree.XMLSyntaxError:
        return None


def _cover_href(opf) -> str | None:
    if opf is None:
        return None
    items = {}
    for it in opf.iter(f"{{{OPF_NS}}}item"):
        items[it.get("id")] = it
        if "cover-image" in (it.get("properties") or "").split():
            return it.get("href")
    for m in opf.iter(f"{{{OPF_NS}}}meta"):
        if m.get("name") == "cover" and m.get("content") in items:
            return items[m.get("content")].get("href")
    return None


def _add_resources(book: Book, folder: Path, prefix: str = "") -> None:
    """Images, stylesheets, and fonts KindleUnpack wrote, under safe names only."""
    for f in sorted(folder.rglob("*")):
        if not f.is_file() or f.is_symlink():
            continue
        rel = f.relative_to(folder).as_posix()
        media = MEDIA_TYPES.get(f.suffix.lower())
        if media is None or media == "application/xhtml+xml" or not _SAFE_NAME.match(rel):
            continue
        name = prefix + rel
        data = f.read_bytes()
        if media == "text/css":
            data = _clean_css(data)
        book.resources[name] = (data, media)


def _clean_css(data: bytes) -> bytes:
    """Stylesheets may load nothing from outside the book."""
    css = data.decode("utf-8", "replace")
    css = re.sub(r"@import[^;]*;", "", css, flags=re.I)
    css = re.sub(r"url\(\s*['\"]?\s*(?:https?:|data:|//)[^)]*\)", "none", css, flags=re.I)
    return css.encode("utf-8")


def _build_kf8(book: Book, oebps: Path, header: Header) -> None:
    opf = _read_opf(oebps / "content.opf")
    if opf is None:
        raise FormatError("not_mobi", "no KF8 package document")
    _add_resources(book, oebps)
    items = {}
    for it in opf.iter(f"{{{OPF_NS}}}item"):
        href = posixpath.normpath(unquote(it.get("href") or ""))
        items[it.get("id")] = (href, it.get("properties") or "")
    for ref in opf.iter(f"{{{OPF_NS}}}itemref"):
        href, _props = items.get(ref.get("idref"), ("", ""))
        if not _SAFE_NAME.match(href) or MEDIA_TYPES.get(Path(href).suffix.lower()) != (
            "application/xhtml+xml"
        ):
            continue
        if href not in book.docs and (oebps / href).is_file():
            _add_doc(book, href, (oebps / href).read_bytes(), "kf8", header)
            book.spine.append((href, ref.get("linear") != "no"))
    nav = next((h for h, p in items.values() if "nav" in p.split()), None)
    if nav and _SAFE_NAME.match(nav) and (oebps / nav).is_file():
        book.toc = _toc_from_nav((oebps / nav).read_bytes(), nav)
    cover = _cover_href(opf)
    if cover:
        name = posixpath.normpath(unquote(cover))
        if book.resources.get(name, (b"", ""))[1] in IMAGE_TYPES:
            book.cover = name


def _toc_from_nav(data: bytes, base: str) -> list[tuple[str, str]]:
    """(label, target) pairs from a nav document's ``epub:type="toc"`` list (flattened)."""
    try:
        root = _parse_document(data)
    except Exception:  # noqa: BLE001 - a broken nav just means a generated one
        return []
    out = []
    for nav in root.iter():
        if _local(nav.tag) != "nav" or nav.get(f"{{{EPUB_NS}}}type") != "toc":
            continue
        for a in nav.iter():
            if _local(a.tag) == "a" and a.get("href"):
                target = _resolve(base, a.get("href"))
                label = " ".join("".join(a.itertext()).split())[:512]
                if target and label:
                    out.append((label, target[0] + (f"#{target[1]}" if target[1] else "")))
        break
    return out[:MAX_DOCUMENTS]


def _add_doc(book: Book, name: str, data: bytes, kind: str, header: Header) -> Doc:
    if len(book.docs) >= MAX_DOCUMENTS:
        raise FormatError("mobi_too_complex", "too many documents")
    root = _parse_document(data, header.encoding)
    sanitizer = Sanitizer(mobi=kind == "mobi7")
    body = sanitizer.body(root)
    title, sheets, styles = _head_info(root, name, book, kind)
    lang = root.get("lang") or root.get(f"{{{XML_NS}}}lang")
    doc = Doc(
        name=name,
        body=body,
        title=title or header.title,
        lang=lang if lang and _LANG.match(lang) else None,
        stylesheets=sheets,
        styles=styles,
    )
    _dedupe_ids(doc)
    book.docs[name] = doc
    return doc


def _build_mobi7(book: Book, folder: Path, header: Header) -> None:
    """Split the one Mobipocket HTML file at its page breaks into chapters."""
    from lxml import etree

    html_path = folder / "book.html"
    if not html_path.is_file():
        raise FormatError("not_mobi", "no text")
    if (folder / "Images").is_dir():
        _add_resources(book, folder / "Images", prefix="Images/")
    root = _parse_document(html_path.read_bytes(), header.encoding, html_only=True)
    whole = Sanitizer(mobi=True).body(root)
    chapters: list = []
    current = etree.Element(f"{{{XHTML_NS}}}body", nsmap={None: XHTML_NS, "epub": EPUB_NS})
    if whole.text and whole.text.strip():
        current.text = whole.text
    for child in list(whole):
        if child.get("class") == "mbp-pagebreak":
            if len(current) or (current.text or "").strip():
                chapters.append(current)
            current = etree.Element(f"{{{XHTML_NS}}}body", nsmap={None: XHTML_NS, "epub": EPUB_NS})
            if child.tail and child.tail.strip():
                current.text = child.tail
            continue
        current.append(child)
    if len(current) or (current.text or "").strip():
        chapters.append(current)
    # A page break nested inside a block stays where it is, as an empty div.
    for body in chapters:
        for el in body.iter(f"{{{XHTML_NS}}}div"):
            if el.get("class") == "mbp-pagebreak":
                el.set("style", "page-break-after: always")
                del el.attrib["class"]
    if not chapters:
        raise FormatError("not_mobi", "no text")
    if len(chapters) > MAX_DOCUMENTS:
        raise FormatError("mobi_too_complex", "too many chapters")
    book.resources["style.css"] = (_MOBI_CSS.encode(), "text/css")
    owner: dict[str, str] = {}
    for i, body in enumerate(chapters, 1):
        name = f"part{i:04d}.xhtml"  # next to Images/, where the HTML's src values point
        doc = Doc(name=name, body=body, title=header.title, stylesheets=["style.css"])
        _dedupe_ids(doc)
        for ident in doc.ids:
            owner.setdefault(ident, name)
        book.docs[name] = doc
        book.spine.append((name, True))
    # MOBI7 links are "#filepos123" into the one file: point them at the chapter holding it.
    for doc in book.docs.values():
        for a in doc.body.iter(f"{{{XHTML_NS}}}a"):
            href = a.get("href") or ""
            if href.startswith("#") and href[1:] in owner:
                a.set("href", f"{posixpath.basename(owner[href[1:]])}#{href[1:]}")
            elif href.startswith("#"):
                del a.attrib["href"]
    ncx = folder / "toc.ncx"
    if ncx.is_file() and ncx.stat().st_size < 4 * 1024 * 1024:
        try:
            tree = etree.fromstring(ncx.read_bytes(), _parser("xml"))
        except etree.XMLSyntaxError:
            tree = None
        for point in tree.iter() if tree is not None else []:
            if _local(point.tag) != "navpoint":
                continue
            label = next((e for e in point.iter() if _local(e.tag) == "text"), None)
            content = next((e for e in point.iter() if _local(e.tag) == "content"), None)
            if label is None or content is None:
                continue
            frag = urlsplit(content.get("src") or "").fragment
            text = " ".join("".join(label.itertext()).split())[:512]
            if text and frag in owner:
                book.toc.append((text, f"{owner[frag]}#{frag}"))
    cover = _cover_href(_read_opf(folder / "content.opf"))
    if cover:
        name = posixpath.normpath(unquote(cover))
        if book.resources.get(name, (b"", ""))[1] in IMAGE_TYPES:
            book.cover = name


_MOBI_CSS = """\
p { margin: 0; text-indent: 1.5em }
blockquote { margin: 0 0 0 1.25em }
img { max-width: 100% }
"""


# --- writing the EPUB -------------------------------------------------------------------------


def _xhtml(doc: Doc, lang: str) -> bytes:
    from lxml import etree

    html = etree.Element(f"{{{XHTML_NS}}}html", nsmap={None: XHTML_NS, "epub": EPUB_NS})
    lang = doc.lang or lang
    html.set("lang", lang)
    html.set(f"{{{XML_NS}}}lang", lang)
    head = etree.SubElement(html, f"{{{XHTML_NS}}}head")
    title = etree.SubElement(head, f"{{{XHTML_NS}}}title")
    title.text = doc.title or "Untitled"
    for sheet in doc.stylesheets:
        link = etree.SubElement(head, f"{{{XHTML_NS}}}link")
        link.set("rel", "stylesheet")
        link.set("type", "text/css")
        link.set("href", _relative(doc.name, sheet))
    for css in doc.styles:
        style = etree.SubElement(head, f"{{{XHTML_NS}}}style")
        style.text = css
    html.append(doc.body)
    body = etree.tostring(html, encoding="utf-8", xml_declaration=False)
    return b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n' + body


def _nav(book: Book, title: str, lang: str) -> bytes:
    from lxml import etree

    entries = [(label, href) for label, href in book.toc if _target_ok(book, href)]
    if not entries:
        entries = [(title, book.spine[0][0])] if book.spine else []
    html = etree.Element(f"{{{XHTML_NS}}}html", nsmap={None: XHTML_NS, "epub": EPUB_NS})
    html.set("lang", lang)
    html.set(f"{{{XML_NS}}}lang", lang)
    head = etree.SubElement(html, f"{{{XHTML_NS}}}head")
    etree.SubElement(head, f"{{{XHTML_NS}}}title").text = "Contents"
    body = etree.SubElement(html, f"{{{XHTML_NS}}}body")
    nav = etree.SubElement(body, f"{{{XHTML_NS}}}nav")
    nav.set(f"{{{EPUB_NS}}}type", "toc")
    nav.set("id", "toc")
    etree.SubElement(nav, f"{{{XHTML_NS}}}h1").text = "Contents"
    ol = etree.SubElement(nav, f"{{{XHTML_NS}}}ol")
    for label, href in entries:
        a = etree.SubElement(etree.SubElement(ol, f"{{{XHTML_NS}}}li"), f"{{{XHTML_NS}}}a")
        name, _, frag = href.partition("#")
        a.set("href", _relative(NAV_NAME, name, frag))
        a.text = label
    # Non-linear documents (a KF8 cover page) must be reachable: list them as landmarks.
    hidden = [name for name, linear in book.spine if not linear]
    if hidden:
        marks = etree.SubElement(body, f"{{{XHTML_NS}}}nav")
        marks.set(f"{{{EPUB_NS}}}type", "landmarks")
        marks.set("hidden", "hidden")
        ol = etree.SubElement(marks, f"{{{XHTML_NS}}}ol")
        for name in hidden:
            cover = "cover" in name.lower()
            a = etree.SubElement(etree.SubElement(ol, f"{{{XHTML_NS}}}li"), f"{{{XHTML_NS}}}a")
            a.set(f"{{{EPUB_NS}}}type", "cover" if cover else "bodymatter")
            a.set("href", _relative(NAV_NAME, name))
            a.text = "Cover" if cover else (book.docs[name].title or name)
    out = etree.tostring(html, encoding="utf-8", xml_declaration=False)
    return b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n' + out


NAV_NAME = "lectern-nav.xhtml"


def _target_ok(book: Book, href: str) -> bool:
    name, _, frag = href.partition("#")
    doc = book.docs.get(name)
    return doc is not None and (not frag or frag in doc.ids)


def _metadata(header: Header, sha256: str) -> dict:
    from lectern.ingest.epub import normalize_isbn

    authors: list[str] = []
    for a in header.text(100):
        for part in re.split(r"\s*(?:;|&)\s*", a):
            if part and part not in authors:
                authors.append(part[:256])
    subjects: list[str] = []
    for s in header.text(105):
        subjects += [t.strip()[:128] for t in re.split(r"\s*;\s*", s) if t.strip()]
    isbn = next((normalize_isbn(v) for v in header.text(104) if normalize_isbn(v)), None)
    asin = next((v for v in header.text(113) + header.text(504) if _ASIN.match(v)), None)
    lang = next((v for v in header.text(524) if _LANG.match(v)), None)
    date = None
    m = _DATE.match(header.first(106) or "")
    if m:
        date = "-".join(p for p in m.groups() if p)
    description = header.first(103)
    if description and "<" in description:
        from lxml import html

        with contextlib.suppress(Exception):
            description = " ".join(html.fromstring(description).text_content().split())
    return {
        "uid": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, 'lectern:kindle:' + sha256)}",
        "title": header.first(503) or header.title or "Untitled",
        "authors": authors[:20],
        "language": lang or "und",
        "isbn": isbn,
        "asin": asin,
        "publisher": header.first(101),
        "description": description,
        "date": date,
        "subjects": subjects[:50],
    }


def _opf(book: Book, meta: dict, manifest: list[tuple[str, str, str, str]]) -> bytes:
    from xml.sax.saxutils import escape, quoteattr

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<package xmlns="{OPF_NS}" version="3.0" unique-identifier="uid">',
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">',
        f'<dc:identifier id="uid">{escape(meta["uid"])}</dc:identifier>',
    ]
    if meta["isbn"]:
        lines.append(f'<dc:identifier id="isbn">urn:isbn:{meta["isbn"]}</dc:identifier>')
    if meta["asin"]:
        lines.append(f'<dc:identifier id="asin">mobi-asin:{meta["asin"]}</dc:identifier>')
    lines.append(f"<dc:title>{escape(meta['title'])}</dc:title>")
    for i, a in enumerate(meta["authors"]):
        lines.append(f'<dc:creator id="creator{i}">{escape(a)}</dc:creator>')
        lines.append(
            f'<meta refines="#creator{i}" property="role" scheme="marc:relators">aut</meta>'
        )
    lines.append(f"<dc:language>{escape(meta['language'])}</dc:language>")
    for key in ("publisher", "description", "date"):
        if meta[key]:
            lines.append(f"<dc:{key}>{escape(meta[key])}</dc:{key}>")
    for s in meta["subjects"]:
        lines.append(f"<dc:subject>{escape(s)}</dc:subject>")
    lines.append(f'<meta property="dcterms:modified">{MODIFIED}</meta>')
    if book.cover:
        lines.append('<meta name="cover" content="cover-image"/>')
    lines += ["</metadata>", "<manifest>"]
    for ident, href, media, props in manifest:
        p = f" properties={quoteattr(props)}" if props else ""
        lines.append(
            f"<item id={quoteattr(ident)} href={quoteattr(href)} media-type={quoteattr(media)}{p}/>"
        )
    lines += ["</manifest>", "<spine>"]
    ids = {href: ident for ident, href, _m, _p in manifest}
    for name, linear in book.spine:
        nonlinear = "" if linear else ' linear="no"'
        lines.append(f"<itemref idref={quoteattr(ids[name])}{nonlinear}/>")
    lines += ["</spine>", "</package>"]
    return "\n".join(lines).encode("utf-8")


def write_epub(book: Book, header: Header, sha256: str, out: Path) -> None:
    """Names are already safe (:data:`_SAFE_NAME`), so they go into the OPF unescaped."""
    if not any(linear for _n, linear in book.spine):
        raise FormatError("not_mobi", "no text")
    meta = _metadata(header, sha256)
    lang = meta["language"]
    _fix_links(book)
    files: list[tuple[str, bytes]] = []
    manifest: list[tuple[str, str, str, str]] = []
    used_resources: set[str] = set()
    for i, (name, _linear) in enumerate(book.spine):
        doc = book.docs[name]
        svg = any(_ns(e.tag) == SVG_NS for e in doc.body.iter())
        props = "svg" if svg else ""
        manifest.append((f"x{i:04d}", name, "application/xhtml+xml", props))
        files.append((name, _xhtml(doc, lang)))
        used_resources.update(doc.stylesheets)
    files.append((NAV_NAME, _nav(book, meta["title"], lang)))
    manifest.append(("nav", NAV_NAME, "application/xhtml+xml", "nav"))
    referenced = _referenced(book) | used_resources
    if book.cover:
        referenced.add(book.cover)
    for j, name in enumerate(sorted(n for n in book.resources if n in referenced)):
        data, media = book.resources[name]
        ident = "cover-image" if name == book.cover else f"r{j:05d}"
        props = "cover-image" if name == book.cover else ""
        manifest.append((ident, name, media, props))
        files.append((name, data))
    container = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    with zipfile.ZipFile(out, "w") as zf:

        def put(name: str, data: bytes, method=zipfile.ZIP_DEFLATED) -> None:
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = method
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)

        put("mimetype", b"application/epub+zip", zipfile.ZIP_STORED)
        put("META-INF/container.xml", container.encode())
        put("OEBPS/content.opf", _opf(book, meta, manifest))
        for name, data in files:
            put(f"OEBPS/{name}", data)


def _referenced(book: Book) -> set[str]:
    """Resources the documents and stylesheets use (images, fonts, imported sheets)."""
    used: set[str] = set()
    for doc in book.docs.values():
        for el in doc.body.iter():
            for key in ("src", f"{{{XLINK_NS}}}href"):
                target = _resolve(doc.name, el.get(key) or "")
                if target and target[0] in book.resources:
                    used.add(target[0])
        for css in doc.styles:
            used |= _css_refs(doc.name, css, book)
    for name in list(used | {s for d in book.docs.values() for s in d.stylesheets}):
        data, media = book.resources.get(name, (b"", ""))
        if media == "text/css":
            used |= _css_refs(name, data.decode("utf-8", "replace"), book)
    return used


def _css_refs(base: str, css: str, book: Book) -> set[str]:
    out = set()
    for m in re.finditer(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", css):
        target = _resolve(base, m.group(1))
        if target and target[0] in book.resources:
            out.add(target[0])
    return out


def build(src: Path, out: Path) -> dict:
    """The child's work: header and DRM check again, unpack, build, write."""
    import hashlib

    header = read_header(src)
    h = hashlib.sha256()
    with src.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    unpacked = Path(tempfile.mkdtemp(prefix="unpacked-", dir=Path.cwd()))
    _unpack(src, unpacked)
    book = Book()
    oebps = unpacked / "mobi8" / "OEBPS"
    if (oebps / "content.opf").is_file():
        _build_kf8(book, oebps, header)
    elif (unpacked / "mobi7").is_dir():
        _build_mobi7(book, unpacked / "mobi7", header)
    else:
        raise FormatError("not_mobi", "nothing unpacked (print replica or dictionary?)")
    write_epub(book, header, h.hexdigest(), out)
    return {"ok": True, "documents": len(book.spine)}


def _unpack(src: Path, dest: Path) -> None:
    from loguru import logger

    logger.remove()  # KindleUnpack logs every record; nobody reads it here
    from mobi.kindleunpack import unpackBook

    with contextlib.redirect_stdout(io.StringIO()):
        unpackBook(str(src), str(dest), epubver="3")


def _limit(file_limit: int) -> None:
    import resource

    for which, value in (
        (resource.RLIMIT_AS, MEMORY_LIMIT_BYTES),
        (resource.RLIMIT_CPU, CPU_LIMIT_S),
        (resource.RLIMIT_FSIZE, file_limit),
        (resource.RLIMIT_CORE, 0),
    ):
        try:
            resource.setrlimit(which, (value, value))
        except (ValueError, OSError):
            pass  # best effort where the platform refuses a limit
    try:
        os.nice(10)
    except OSError:
        pass


def main(argv: list[str]) -> int:
    _limit(FILE_LIMIT_BYTES)
    real_stdout = sys.stdout
    try:
        result = build(Path(argv[0]), Path(argv[1]))
    except FormatError as exc:
        result = {"ok": False, "error": exc.code, "detail": str(exc)[:200]}
    except (MemoryError, RecursionError):
        result = {"ok": False, "error": "mobi_too_complex", "detail": "limit"}
    except Exception as exc:  # noqa: BLE001 - anything else is an unreadable book
        result = {"ok": False, "error": "not_mobi", "detail": type(exc).__name__}
    real_stdout.write(json.dumps(result) + "\n")
    real_stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
