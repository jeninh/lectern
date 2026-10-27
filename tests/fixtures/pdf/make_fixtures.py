"""Write the PDF fixtures for tests/test_pdf_text.py (deterministic; run from the repo root).

    uv run python tests/fixtures/pdf/make_fixtures.py

Every file is written by hand-built PDF syntax, so nothing depends on a PDF library's version:

two-column.pdf
    A5, three pages: a full-width title, then two columns of Courier text per page (Courier has
    fixed widths, so line breaks are exact). A running header on pages 2–3, a page number at
    the foot of every page, words hyphenated at line ends (and one compound broken at its own
    hyphen), "fi" typeset as the ligature glyph (code 0x80 → /fi, ToUnicode U+FB01), curly
    quotes, and an outline with two chapters (page 1 and page 3).
plain.pdf
    One column, three pages, no outline, a paragraph that continues across a page break and a
    repeated footer with the page number in it ("Field Notes · 2").
scanned.pdf
    Two pages that are only images (no text layer): what a scanner produces.
low-text.pdf
    Three image pages with only a page number in the text layer: also needs OCR.

The prose is original to this repository (public domain dedication: CC0).
"""

from __future__ import annotations

import json
import random
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent

TITLE = "The Keeper of Small Lights"
AUTHOR = "Ada Marlow"
CHAPTERS = [
    (
        "Chapter One",
        [
            "Every evening at dusk, Wren climbed the ninety-one steps of the lighthouse and "
            "lit the lamp that kept the fishing boats off the rocks. Her grandfather had done "
            "it for fifty years, and his father before him, and nobody in the village "
            "remembered a night when the light had failed.",
            "The well-known story was that the lamp had never once gone out. Wren knew "
            "better. On the night of the great storm the wick had drowned in its own oil, "
            "and her grandfather had held a candle in the window for six hours until his "
            "fingers blistered. The boats came home anyway. He never told the village.",
            "\u201cA light is a promise,\u201d he said to her once, \u201cnot a machine. "
            "You keep it with whatever you have in your hands.\u201d She thought about that "
            "often, polishing the brass, trimming the wick, counting the boats as they "
            "came in from the fishing grounds beyond the reef.",
            "In the spring a letter arrived from the harbour office. It explained, in "
            "careful official language, that an electric beacon would be installed at the "
            "end of the summer, and that the lighthouse would no longer be required.",
        ],
    ),
    (
        "Chapter Two",
        [
            "Wren read the letter twice and then folded it into a small square. She did "
            "not argue with it. Instead she began to write down everything the lighthouse "
            "had taught her: how the fog rolls in before a change in the weather, how "
            "the gulls go quiet an hour before a squall, how a lamp should be trimmed "
            "so that it burns without smoke.",
            "By August the notebook was full. The beacon arrived on a barge, a grey box "
            "with a blinking eye, and the engineers fixed it to the rocks in an afternoon. "
            "It was efficient and tireless and it did not need anyone at all.",
            "On the last night Wren lit the old lamp one final time. Down in the harbour "
            "the fishermen saw both lights burning side by side, the new one steady and "
            "the old one flickering, and one by one they took off their caps.",
        ],
    ),
]

FIELD_NOTES = [
    "These notes were kept during a single season on the northern shore, where the tide "
    "pools freeze at the edges in March and the first warblers return in the middle of "
    "April. They are not a scientific record. They are a list of things that were noticed, "
    "written down before they could be forgotten, in the order in which they happened.",
    "The first entry concerns a heron that stood in the same shallow pool every morning for "
    "eleven days. It caught nothing that anyone saw. On the twelfth day it was gone, and "
    "the pool had a thin skin of ice that cracked like glass when a pebble was thrown onto "
    "it. Nobody could say whether the heron had known the cold was coming or had simply "
    "grown tired of waiting for a fish that never arrived in that particular pool, which "
    "was the smallest of the seven along the path to the old pier.",
    "The second entry is about the light. In late April the sun rises over the water at an "
    "angle that turns the whole bay copper for about four minutes. If you are not looking "
    "at that moment you will miss it entirely, and most people do.",
]

# --- a tiny PDF writer --------------------------------------------------------------------------


class Pdf:
    def __init__(self) -> None:
        self.objects: list[bytes | None] = []

    def reserve(self) -> int:
        self.objects.append(None)
        return len(self.objects)

    def add(self, body: bytes | str, num: int | None = None) -> int:
        if isinstance(body, str):
            body = body.encode("latin-1")
        if num is None:
            self.objects.append(body)
            return len(self.objects)
        self.objects[num - 1] = body
        return num

    def stream(self, data: bytes, extra: str = "", compress: bool = True) -> int:
        if compress:
            data = zlib.compress(data, 9)
            extra = f"/Filter /FlateDecode {extra}"
        head = f"<< /Length {len(data)} {extra}>>\nstream\n".encode("latin-1")
        return self.add(head + data + b"\nendstream")

    def write(self, path: Path, root: int, info: int | None = None) -> None:
        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(self.objects, start=1):
            assert body is not None, f"object {i} reserved but never written"
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(self.objects) + 1}\n0000000000 65535 f \n".encode()
        for off in offsets:
            out += f"{off:010d} 00000 n \n".encode()
        trailer = f"/Size {len(self.objects) + 1} /Root {root} 0 R"
        if info:
            trailer += f" /Info {info} 0 R"
        # A fixed /ID keeps the bytes (and the fixture hashes) identical on every run.
        trailer += " /ID [<6c65637465726e706466746578740001> <6c65637465726e706466746578740001>]"
        out += f"trailer\n<< {trailer} >>\nstartxref\n{xref}\n%%EOF\n".encode()
        path.write_bytes(bytes(out))


WIN = {
    "\u2018": 0x91,
    "\u2019": 0x92,
    "\u201c": 0x93,
    "\u201d": 0x94,
    "\u2014": 0x97,
    "\u00b7": 0xB7,
}


def encode(text: str, ligatures: bool) -> bytes:
    """Text as Courier/WinAnsi codes; with ``ligatures`` every "fi" is the /fi glyph (0x80)."""
    out = bytearray()
    i = 0
    while i < len(text):
        if ligatures and text.startswith("fi", i):
            out.append(0x80)
            i += 2
            continue
        ch = text[i]
        out.append(WIN.get(ch, ord(ch)))
        i += 1
    esc = bytearray()
    for b in out:
        if b in (0x28, 0x29, 0x5C):
            esc.append(0x5C)
        esc.append(b)
    return bytes(esc)


def to_unicode_cmap() -> bytes:
    pairs = [(b, b) for b in range(0x20, 0x7F)] + [(0x80, 0xFB01)]
    pairs += [(code, ord(ch)) for ch, code in WIN.items()]
    lines = "\n".join(f"<{c:02X}> <{u:04X}>" for c, u in sorted(pairs))
    return (
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Lectern-UCS def /CMapType 2 def\n"
        "1 begincodespacerange <00> <FF> endcodespacerange\n"
        f"{len(pairs)} beginbfchar\n{lines}\nendbfchar\n"
        "endcmap CMapName currentdict /CMap defineresource pop end end\n"
    ).encode("latin-1")


def fonts(pdf: Pdf) -> str:
    cmap = pdf.stream(to_unicode_cmap())
    enc = "<< /Type /Encoding /BaseEncoding /WinAnsiEncoding /Differences [128 /fi] >>"
    f1 = pdf.add(
        f"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding {enc} /ToUnicode {cmap} 0 R >>"
    )
    f2 = pdf.add(
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier-Bold /Encoding /WinAnsiEncoding "
        f"/ToUnicode {cmap} 0 R >>"
    )
    return f"<< /Font << /F1 {f1} 0 R /F2 {f2} 0 R >> >>"


def wrap(text: str, width: int) -> list[str]:
    """Greedy wrap to ``width`` characters. A word that does not fit is broken with a hyphen
    (a compound at its own hyphen when that fits), as a typesetter's hyphenation would."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        while True:
            room = width - len(line) - (1 if line else 0)
            if len(word) <= room:
                line = f"{line} {word}" if line else word
                break
            cut = word.rfind("-", 0, room)
            if cut >= 2:
                head, word = word[: cut + 1], word[cut + 1 :]
            elif room >= 4 and len(word) >= 7 and word[: room - 1].isalpha():
                keep = min(room - 1, len(word) - 3)
                head, word = word[:keep] + "-", word[keep:]
            else:
                lines.append(line)
                line = ""
                continue
            lines.append(f"{line} {head}" if line else head)
            line = ""
    if line:
        lines.append(line)
    return lines


def text_op(font: str, size: float, x: float, y: float, s: str, lig: bool = True) -> bytes:
    return b"BT /%s %g Tf %g %g Td (" % (font.encode(), size, x, y) + encode(s, lig) + b") Tj ET\n"


def outline(pdf: Pdf, entries: list[tuple[str, int]], page_ids: list[int]) -> int:
    root = pdf.reserve()
    ids = [pdf.reserve() for _ in entries]
    for i, ((title, page), oid) in enumerate(zip(entries, ids, strict=True)):
        links = f"/Parent {root} 0 R /Dest [{page_ids[page]} 0 R /XYZ 0 595 0]"
        if i > 0:
            links += f" /Prev {ids[i - 1]} 0 R"
        if i + 1 < len(ids):
            links += f" /Next {ids[i + 1]} 0 R"
        pdf.add(f"<< /Title ({title}) {links} >>", oid)
    pdf.add(
        f"<< /Type /Outlines /First {ids[0]} 0 R /Last {ids[-1]} 0 R /Count {len(ids)} >>", root
    )
    return root


def finish(pdf: Pdf, path: Path, contents: list[bytes], resources: str, size, extra: str = "",
           outline_entries=None, xobjects: list[str] | None = None) -> None:  # fmt: skip
    pages_id = pdf.reserve()
    page_ids = []
    for i, c in enumerate(contents):
        cid = pdf.stream(c)
        res = resources
        if xobjects:
            res = res[:-2] + f" /XObject << /Im0 {xobjects[i]} >> >>"
        page_ids.append(
            pdf.add(
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {size[0]} {size[1]}] "
                f"/Resources {res} /Contents {cid} 0 R >>"
            )
        )
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    pdf.add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>", pages_id)
    cat = f"<< /Type /Catalog /Pages {pages_id} 0 R"
    if outline_entries:
        cat += f" /Outlines {outline(pdf, outline_entries, page_ids)} 0 R /PageMode /UseOutlines"
    root = pdf.add(cat + extra + " >>")
    info = pdf.add(
        f"<< /Title ({TITLE if 'two' in path.name else path.stem}) /Author ({AUTHOR}) >>"
    )
    pdf.write(path, root, info)


# --- two-column.pdf ------------------------------------------------------------------------


A5 = (420, 595)
COL_CHARS = 26  # 160 pt at 10 pt Courier (6 pt per character)
LEADING = 13
LEFT_X, RIGHT_X = 40, 220
BOTTOM = 190


def two_column(path: Path) -> dict:
    pdf = Pdf()
    res = fonts(pdf)
    pages: list[list[bytes]] = [[]]
    chapter_pages: list[int] = []
    expected_lines: list[list[str]] = []

    def page_top(i: int) -> float:
        return 470 if i == 0 else 520

    col, y = 0, page_top(0)

    def new_page() -> None:
        nonlocal col, y
        pages.append([])
        col, y = 0, page_top(len(pages) - 1)

    def emit(line: str, font="F1", size=10.0, advance=LEADING) -> None:
        nonlocal col, y
        if y < BOTTOM:
            if col == 0:
                col, y = 1, page_top(len(pages) - 1)
            else:
                new_page()
        pages[-1].append(text_op(font, size, LEFT_X if col == 0 else RIGHT_X, y, line))
        y -= advance

    # The title is the running header's text too, but larger: header removal must keep it.
    pages[0].append(text_op("F2", 16, 85, 540, TITLE.upper()))
    pages[0].append(text_op("F1", 10, 156, 516, f"by {AUTHOR}"))
    for ci, (heading, paragraphs) in enumerate(CHAPTERS):
        if ci > 0:
            new_page()
        chapter_pages.append(len(pages) - 1)
        emit(heading, font="F2", size=11, advance=LEADING * 2)
        for p in paragraphs:
            lines = wrap(p, COL_CHARS)
            expected_lines.append(lines)
            for line in lines:
                emit(line)
            y -= LEADING / 2
    contents = []
    for i, ops in enumerate(pages):
        body = b"".join(ops)
        if i > 0:
            body = text_op("F1", 7, 141, 565, "THE KEEPER OF SMALL LIGHTS") + body
        body += text_op("F1", 9, 207, 30, str(i + 1))
        contents.append(body)
    entries = [(h, p) for (h, _), p in zip(CHAPTERS, chapter_pages, strict=True)]
    finish(pdf, path, contents, res, A5, outline_entries=entries)
    hyphenated = sum(1 for ls in expected_lines for ln in ls if ln.endswith("-"))
    return {"pages": len(pages), "chapter_pages": chapter_pages, "hyphenated_lines": hyphenated}


# --- plain.pdf and labels.pdf -------------------------------------------------------------------


PAGE_LABELS = "/PageLabels << /Nums [0 << /S /r >> 2 << /S /D /St 5 >>] >>"
# pages 1-2 get lowercase roman numerals (front matter), page 3 onward counts from 5: an
# offset, as when a book's printed "page 1" follows a numbered preface.


def plain(path: Path, extra: str = "") -> dict:
    pdf = Pdf()
    res = fonts(pdf)
    size = (420, 595)
    pages: list[list[bytes]] = [[]]
    y = 540
    for p in FIELD_NOTES:
        for line in wrap(p, 55):
            if y < 400:  # short pages, so a paragraph runs over a page break
                pages.append([])
                y = 540
            pages[-1].append(text_op("F1", 10, 45, y, line))
            y -= LEADING
        y -= LEADING
    contents = [
        b"".join(ops) + text_op("F1", 8, 170, 36, f"Field Notes \u00b7 {i + 1}")
        for i, ops in enumerate(pages)
    ]
    finish(pdf, path, contents, res, size, extra=extra)
    return {"pages": len(pages)}


# --- scanned.pdf and low-text.pdf ---------------------------------------------------------------


def grey_image(pdf: Pdf, seed: int) -> str:
    """A 120×170 greyscale "scan": paper noise with dark bars where lines of print would be."""
    rng = random.Random(seed)
    w, h = 120, 170
    rows = bytearray()
    for yy in range(h):
        line_of_print = 20 <= yy < 150 and (yy // 4) % 3 == 0
        for xx in range(w):
            ink = line_of_print and 12 <= xx < 108 and rng.random() < 0.7
            rows.append(rng.randint(30, 70) if ink else rng.randint(225, 250))
    oid = pdf.stream(
        bytes(rows),
        f"/Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceGray "
        "/BitsPerComponent 8 ",
    )
    return f"{oid} 0 R"


def scanned(path: Path, pages: int, page_numbers: bool) -> dict:
    pdf = Pdf()
    res = fonts(pdf)
    xobjects = [grey_image(pdf, i) for i in range(pages)]
    contents = []
    for i in range(pages):
        c = b"q 420 0 0 595 0 0 cm /Im0 Do Q\n"
        if page_numbers:
            c += text_op("F1", 9, 207, 20, str(i + 1))
        contents.append(c)
    finish(pdf, path, contents, res, A5, xobjects=xobjects)
    return {"pages": pages}


# --- structured.pdf (issue #63: what a converted edition keeps beyond the text) --------------

S_DROP_PARA = (
    "The harbour was quiet that morning. Wren walked along the old stone pier and counted "
    "the boats: there were eleven, which was one fewer than the day before, and the missing "
    "one was the Heron."
)
S_ITALIC = ["old stone pier", "Heron"]
S_BOLD = ["eleven"]
S_AFTER_FIGURE = "The fog lifted by noon, and the gulls came back to the roofs of the sheds."
S_CAPTIONS = ["Figure 1. The harbour at dawn.", "Table 1. Boats in the harbour.",
              "Figure 2. A compass rose."]  # fmt: skip
S_NOTED = [
    ("Every boat that left was recorded in the harbour book.", "1",
     "The harbour book is kept in the office."),
    ("Its pages agree with the old chart.", "2", "The chart dates from 1888."),
]  # fmt: skip
S_TABLE = [["Boat", "Crew", "Catch"], ["Heron", "4", "120 kg"], ["Gull", "3", "85 kg"]]
S_POEM = [
    "The lamp is lit, the tide is low,",
    "the boats come in from sea;",
    "and every light the keeper keeps",
    "is kept for you and me.",
]
S_INTRO = "Before dusk the keeper had three tasks, and she did them in this order:"
S_LIST = ["Trim the wick.", "Polish the brass.", "Count the boats."]
S_QUOTE = "A light is a promise, not a machine. You keep it with whatever you have in your hands."
S_EQUATION = ("a + b = g", "\u03b1 + \u03b2 = \u03b3")  # Symbol codes, and what they show
S_LAST = "Wren kept the notebook on the shelf by the lamp."
S_PLATE = "Plate I"


def _ops(font: str, size: float, x: float, y: float, s: str, rise: float = 0.0) -> bytes:
    esc = s.translate({0x28: "\\(", 0x29: "\\)", 0x5C: "\\\\"})
    data = "".join(chr(WIN.get(ch, 0x95 if ch == "\u2022" else ord(ch))) for ch in esc)
    rise_op = b"%g Ts " % rise  # always set: the text rise outlives ET (graphics state)
    return b"BT /%s %g Tf %s%g %g Td (" % (font.encode(), size, rise_op, x, y) + (
        data.encode("latin-1") + b") Tj ET\n"
    )


def _styled(x: float, y: float, line: str, offset: int, para: str, size=10.0) -> bytes:
    """One wrapped line of ``para`` (starting at ``offset``) with the italic and bold phrases
    of the paragraph set in Courier-Oblique and Courier-Bold (Courier: 0.6 em per character)."""
    style = ["F1"] * len(line)
    for phrases, font in ((S_ITALIC, "F3"), (S_BOLD, "F2")):
        for ph in phrases:
            at = para.find(ph)
            for k in range(at, at + len(ph)):
                if offset <= k < offset + len(line):
                    style[k - offset] = font
    out = b""
    i = 0
    while i < len(line):
        j = i
        while j < len(line) and style[j] == style[i]:
            j += 1
        out += _ops(style[i], size, x + 0.6 * size * i, y, line[i:j])
        i = j
    return out


def _gradient(pdf: Pdf, w: int, h: int) -> str:
    rows = bytes((xx * 3 + yy * 2) % 256 for yy in range(h) for xx in range(w))
    oid = pdf.stream(rows, f"/Type /XObject /Subtype /Image /Width {w} /Height {h} "
                     "/ColorSpace /DeviceGray /BitsPerComponent 8 ")  # fmt: skip
    return f"{oid} 0 R"


def structured(path: Path) -> dict:
    pdf = Pdf()
    f = {
        "F1": "/Courier /Encoding /WinAnsiEncoding",
        "F2": "/Courier-Bold /Encoding /WinAnsiEncoding",
        "F3": "/Courier-Oblique /Encoding /WinAnsiEncoding",
        "F4": "/Symbol",
    }
    ids = {k: pdf.add(f"<< /Type /Font /Subtype /Type1 /BaseFont {v} >>") for k, v in f.items()}
    fonts_res = " ".join(f"/{k} {v} 0 R" for k, v in ids.items())
    photo, plate = _gradient(pdf, 120, 80), _gradient(pdf, 140, 200)
    res = f"<< /Font << {fonts_res} >> /XObject << /Im1 {photo} /Im2 {plate} >> >>"
    pages: list[bytes] = []

    # Page 1: a drop cap, italic and bold phrases, a photo with its caption below.
    c = _ops("F2", 16, 45, 540, "Chapter One")
    c += _ops("F1", 34, 45, 476, S_DROP_PARA[0])
    rest = S_DROP_PARA[1:]
    first3 = wrap(rest, 50)[:3]  # beside the drop cap, indented
    after = wrap(rest[len(" ".join(first3)) + 1 :], 55)
    y, offset = 500.0, 1
    for k, line in enumerate(first3 + after):
        x = 72 if k < 3 else 45
        c += _styled(x, y, line, offset, S_DROP_PARA)
        offset += len(line) + 1
        y -= LEADING
    c += b"q 200 0 0 100 110 300 cm /Im1 Do Q\n"
    c += _ops("F3", 9, 110, 286, S_CAPTIONS[0])
    y = 255.0
    for line in wrap(S_AFTER_FIGURE, 55):
        c += _ops("F1", 10, 45, y, line)
        y -= LEADING
    c += _ops("F1", 9, 207, 30, "1")
    pages.append(c)

    # Page 2: two footnote markers and their notes, a ruled table, a poem.
    c = _ops("F2", 16, 45, 540, "Chapter Two")
    y = 510.0
    for text, mark, _note in S_NOTED:  # one line each, the marker raised and smaller
        c += _ops("F1", 10, 45, y, text)
        c += _ops("F1", 6, 45 + 6 * len(text), y, mark, rise=4)
        y -= LEADING
    c += _ops("F3", 9, 60, 444, S_CAPTIONS[1])
    xs, ys = [60, 170, 260, 360], [430, 406, 382, 358]
    rules = b"0.5 w\n"
    for yy in ys:
        rules += b"60 %d m 360 %d l S\n" % (yy, yy)
    for xx in xs:
        rules += b"%d 358 m %d 430 l S\n" % (xx, xx)
    c += rules
    for r, row in enumerate(S_TABLE):
        for col, cell in enumerate(row):
            c += _ops("F2" if r == 0 else "F1", 10, xs[col] + 6, ys[r] - 16, cell)
    y = 320.0
    for line in S_POEM:
        c += _ops("F1", 10, 90, y, line)
        y -= LEADING
    c += b"0.5 w 45 104 m 150 104 l S\n"
    y = 92.0
    for _t, mark, note in S_NOTED:
        c += _ops("F1", 8, 45, y, f"{mark} {note}")
        y -= 10
    c += _ops("F1", 9, 207, 30, "2")
    pages.append(c)

    # Page 3: a list, a block quote, a displayed equation, a vector drawing with a caption.
    c = _ops("F2", 16, 45, 540, "Chapter Three")
    y = 510.0
    for line in wrap(S_INTRO, 55):
        c += _ops("F1", 10, 45, y, line)
        y -= LEADING
    y -= 10
    for item in S_LIST:
        c += _ops("F1", 10, 45, y, f"\u2022 {item}")
        y -= LEADING
    y -= 12
    for line in wrap(S_QUOTE, 40):
        c += _ops("F1", 10, 85, y, line)
        y -= LEADING
    y -= 14
    c += _ops("F4", 10, 170, y, S_EQUATION[0])
    c += _ops("F1", 10, 340, y, "(1)")
    # A compass: a circle of four curves, two strokes across it, the letter N inside.
    c += (b"1 w 210 260 m 237.6 260 260 237.6 260 210 c 260 182.4 237.6 160 210 160 c "
          b"182.4 160 160 182.4 160 210 c 160 237.6 182.4 260 210 260 c S\n"
          b"210 165 m 210 255 l S 165 210 m 255 210 l S\n")  # fmt: skip
    c += _ops("F2", 10, 214, 236, "N")
    c += _ops("F3", 9, 150, 146, S_CAPTIONS[2])
    c += _ops("F1", 10, 45, 118, S_LAST)
    c += _ops("F1", 9, 207, 30, "3")
    pages.append(c)

    # Page 4: a full-page plate with a two-word caption: no usable text, a page to keep whole.
    c = b"q 330 0 0 470 45 80 cm /Im2 Do Q\n" + _ops("F1", 10, 189, 60, S_PLATE)
    pages.append(c)

    entries = [("Chapter One", 0), ("Chapter Two", 1), ("Chapter Three", 2)]
    finish(pdf, path, pages, res, A5, outline_entries=entries)
    return {"pages": len(pages)}


def main() -> None:
    facts = {
        "structured.pdf": structured(HERE / "structured.pdf"),
        "two-column.pdf": two_column(HERE / "two-column.pdf"),
        "plain.pdf": plain(HERE / "plain.pdf"),
        "labels.pdf": plain(HERE / "labels.pdf", extra=PAGE_LABELS),
        "scanned.pdf": scanned(HERE / "scanned.pdf", 2, page_numbers=False),
        "low-text.pdf": scanned(HERE / "low-text.pdf", 3, page_numbers=True),
    }
    expected = {
        "title": TITLE,
        "chapters": [{"title": h, "paragraphs": [p for p in ps]} for h, ps in CHAPTERS],
        "field_notes": FIELD_NOTES,
        "structured": {
            "drop_paragraph": S_DROP_PARA,
            "italic": S_ITALIC,
            "bold": S_BOLD,
            "captions": S_CAPTIONS,
            "footnotes": [[t, m, n] for t, m, n in S_NOTED],
            "table": S_TABLE,
            "poem": S_POEM,
            "list": S_LIST,
            "quote": S_QUOTE,
            "equation": S_EQUATION[1],
            "plate_page": 4,
        },
        "facts": facts,
    }
    (HERE / "expected.json").write_text(json.dumps(expected, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(facts))


if __name__ == "__main__":
    main()
