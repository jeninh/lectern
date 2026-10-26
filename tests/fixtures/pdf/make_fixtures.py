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


def main() -> None:
    facts = {
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
        "facts": facts,
    }
    (HERE / "expected.json").write_text(json.dumps(expected, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(facts))


if __name__ == "__main__":
    main()
