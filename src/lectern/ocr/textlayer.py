"""The invisible text layer of a searchable PDF, the way OCRmyPDF and Tesseract draw it.

Every recognised word is drawn in text render mode 3 (neither filled nor stroked: invisible)
over its place on the page image, in a "glyphless" font: one empty glyph, 500 units wide, for
every character, with a ``ToUnicode`` map that says each character code *is* its UTF-16 code
unit. Viewers then find, select, and copy the real text, and nothing changes visually. Each
word is stretched horizontally (``Tz``) to the width of its box, at a size equal to its line's
height, so a selection covers the word on the image. A space follows every word but the last
on a line (stretched into the gap), so copied text has its spaces.

The original page is untouched: its content streams are wrapped in ``q``/``Q`` and the text
follows in a stream of its own, in the page's own user space (before ``/Rotate``), which is
where the recognised boxes are.
"""

from __future__ import annotations

import io
from functools import lru_cache

FONT_NAME = "/LecternOCR"
GLYPH_WIDTH = 500  # in 1/1000 em, for every character (the font's /DW)
MIN_SIZE = 1.0
MAX_TZ = 10_000.0


@lru_cache(maxsize=1)
def glyphless_ttf() -> bytes:
    """A TrueType font with two empty glyphs (``.notdef`` and the one every code maps to)."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    fb = FontBuilder(1000, isTTF=True)
    order = [".notdef", "blank"]
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap({0x20: "blank"})
    empty = TTGlyphPen(None).glyph()
    pen = TTGlyphPen(None)
    pen.moveTo((0, 0))
    pen.lineTo((0, 1000))
    pen.lineTo((GLYPH_WIDTH, 1000))
    pen.lineTo((GLYPH_WIDTH, 0))
    pen.closePath()
    fb.setupGlyf({".notdef": empty, "blank": pen.glyph()})
    fb.setupHorizontalMetrics({name: (GLYPH_WIDTH, 0) for name in order})
    fb.setupHorizontalHeader(ascent=1000, descent=0)
    fb.setupNameTable({"familyName": "LecternOCR", "styleName": "Regular"})
    fb.setupOS2(sTypoAscender=1000, sTypoDescender=0, usWinAscent=1000, usWinDescent=0)
    fb.setupPost()
    fb.setupHead(unitsPerEm=1000, created=0, modified=0)
    out = io.BytesIO()
    fb.save(out)
    return out.getvalue()


def cid_to_gid_map() -> bytes:
    """Every CID (UTF-16 code unit) to glyph 1."""
    return b"\x00\x01" * 65536


def to_unicode_cmap() -> bytes:
    """Identity: character code ``XXYY`` is the UTF-16 code unit ``XXYY``. Written as 256
    ranges that each vary only in the last byte, as the CMap format requires."""
    ranges = [f"<{hi:02X}00> <{hi:02X}FF> <{hi:02X}00>" for hi in range(256)]
    blocks = []
    for i in range(0, len(ranges), 100):
        chunk = ranges[i : i + 100]
        blocks.append(f"{len(chunk)} beginbfrange\n" + "\n".join(chunk) + "\nendbfrange")
    return (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        + "\n".join(blocks)
        + "\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    ).encode("ascii")


def _hex(text: str) -> str:
    return text.encode("utf-16-be", "surrogatepass").hex().upper()


def _n(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# Per page /Rotate: the reading direction and the "up" direction, in user space, of text that
# is upright on screen, and which corner of a word's box its baseline starts at.
_FRAMES = {
    0: ((1, 0), (0, 1), lambda b: (b[0], b[1])),
    90: ((0, 1), (-1, 0), lambda b: (b[2], b[1])),
    180: ((-1, 0), (0, -1), lambda b: (b[2], b[3])),
    270: ((0, -1), (1, 0), lambda b: (b[0], b[3])),
}


def page_content(words: list[list], rotation: int = 0) -> bytes:
    """The text-layer content stream for one page's words
    (``[text, x0, y0, x1, y1, size, flags]``, PDF user space, in reading order). On a page
    with ``/Rotate``, words run the way they read on screen."""
    from lectern.ocr.words import FLAG_BREAK

    (dx, dy), (ux, uy), origin = _FRAMES.get(int(rotation) % 360, _FRAMES[0])
    sideways = dx == 0

    def along(box) -> float:  # where a word starts along the reading direction
        ox, oy = origin(box)
        return ox * dx + oy * dy

    out = ["BT", "3 Tr"]
    for i, w in enumerate(words):
        text, x0, y0, x1, y1, _size, flags = w
        if not text:
            continue
        box = (x0, y0, x1, y1)
        length, height = (y1 - y0, x1 - x0) if sideways else (x1 - x0, y1 - y0)
        size = max(MIN_SIZE, height)
        target = max(0.1, length)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt is not None and not (int(flags) & FLAG_BREAK):
            gap = along(nxt[1:5]) - along(box)
            if gap > length:
                text = text + " "
                target = gap
        natural = len(text.encode("utf-16-be", "surrogatepass")) // 2 * GLYPH_WIDTH / 1000 * size
        tz = min(MAX_TZ, max(1.0, 100.0 * target / natural))
        ox, oy = origin(box)
        out.append(
            f"{FONT_NAME} {_n(size)} Tf {_n(tz)} Tz {dx} {dy} {ux} {uy} {_n(ox)} {_n(oy)} Tm "
            f"<{_hex(text)}> Tj"
        )
    out.append("ET")
    return ("\n".join(out) + "\n").encode("ascii")


def add_font(writer) -> object:
    """Add the glyphless Type 0 font to ``writer`` (pypdf); returns its indirect reference."""
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
        NumberObject,
        TextStringObject,
    )

    def stream(data: bytes, **extra) -> object:
        s = DecodedStreamObject()
        s.set_data(data)
        for k, v in extra.items():
            s[NameObject(f"/{k}")] = v
        return writer._add_object(s.flate_encode())

    ttf = glyphless_ttf()
    font_file = stream(ttf, Length1=NumberObject(len(ttf)))
    descriptor = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/FontDescriptor"),
                NameObject("/FontName"): NameObject("/GlyphLessFont"),
                NameObject("/Flags"): NumberObject(5),
                NameObject("/FontBBox"): ArrayObject(
                    [
                        NumberObject(0),
                        NumberObject(0),
                        NumberObject(GLYPH_WIDTH),
                        NumberObject(1000),
                    ]
                ),
                NameObject("/ItalicAngle"): NumberObject(0),
                NameObject("/Ascent"): NumberObject(1000),
                NameObject("/Descent"): NumberObject(0),
                NameObject("/CapHeight"): NumberObject(1000),
                NameObject("/StemV"): NumberObject(80),
                NameObject("/FontFile2"): font_file,
            }
        )
    )
    cid_font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/CIDFontType2"),
                NameObject("/BaseFont"): NameObject("/GlyphLessFont"),
                NameObject("/CIDSystemInfo"): DictionaryObject(
                    {
                        NameObject("/Registry"): TextStringObject("Adobe"),
                        NameObject("/Ordering"): TextStringObject("Identity"),
                        NameObject("/Supplement"): NumberObject(0),
                    }
                ),
                NameObject("/FontDescriptor"): descriptor,
                NameObject("/DW"): NumberObject(GLYPH_WIDTH),
                NameObject("/CIDToGIDMap"): stream(cid_to_gid_map()),
            }
        )
    )
    return writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type0"),
                NameObject("/BaseFont"): NameObject("/GlyphLessFont"),
                NameObject("/Encoding"): NameObject("/Identity-H"),
                NameObject("/DescendantFonts"): ArrayObject([cid_font]),
                NameObject("/ToUnicode"): stream(to_unicode_cmap()),
            }
        )
    )


def overlay(writer, page, font_ref, words: list[list], rotation: int = 0) -> None:
    """Draw ``words`` invisibly over ``page`` (a page of ``writer``)."""
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject

    if not words:
        return

    def add(data: bytes):
        s = DecodedStreamObject()
        s.set_data(data)
        return writer._add_object(s.flate_encode())

    resources = page.get_inherited("/Resources", DictionaryObject())
    resources = DictionaryObject(resources.get_object() if resources else {})
    fonts = resources.get("/Font")
    fonts = DictionaryObject(fonts.get_object() if fonts is not None else {})
    fonts[NameObject(FONT_NAME)] = font_ref
    resources[NameObject("/Font")] = fonts
    page[NameObject("/Resources")] = resources
    contents = page.get("/Contents")
    existing = []
    if contents is not None:
        obj = contents.get_object()
        existing = list(obj) if isinstance(obj, ArrayObject) else [contents]
    page[NameObject("/Contents")] = ArrayObject(
        [add(b"q\n"), *existing, add(b"\nQ\n" + page_content(words, rotation))]
    )
