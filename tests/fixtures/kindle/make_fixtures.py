"""Rebuild the Kindle-format test fixtures in this folder (see PROVENANCE.md).

Needs calibre's ``ebook-convert`` on PATH; CI does not run this, it uses the checked-in files.

    python tests/fixtures/kindle/make_fixtures.py

The source is a tiny EPUB written here (original text, generated images), so the fixtures carry
no third-party content. calibre writes a random UUID as the ASIN (EXTH 113); :func:`set_asin`
replaces it with the made-up ASIN ``B0LECTERN1``.
"""

from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent

OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:5c0d1b9e-7a51-4c1e-9d3f-1ec7e2a0b001</dc:identifier>
    <dc:identifier id="isbn">urn:isbn:9780000000002</dc:identifier>
    <dc:title>The Lighthouse Keeper’s Almanac</dc:title>
    <dc:creator id="a1">Ada Quill</dc:creator>
    <dc:creator id="a2">Bram Ellis</dc:creator>
    <dc:language>en</dc:language>
    <dc:publisher>Lectern Test Fixtures</dc:publisher>
    <dc:date>2024-03-05</dc:date>
    <dc:description>A short almanac written for Lectern’s tests.</dc:description>
    <dc:subject>Lighthouses</dc:subject>
    <dc:subject>Weather</dc:subject>
    <meta property="dcterms:modified">2024-03-05T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
    <item id="lamp" href="lamp.png" media-type="image/png"/>
    <item id="cover" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>
  </manifest>
  <spine><itemref idref="ch1"/><itemref idref="ch2"/></spine>
</package>"""

NAV = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Contents</title></head>
<body><nav epub:type="toc"><ol>
<li><a href="ch1.xhtml">Spring Tides</a></li>
<li><a href="ch2.xhtml">The Winter Lamp</a></li>
</ol></nav></body></html>"""

CH1 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
<head><title>Spring Tides</title><link rel="stylesheet" href="style.css"/></head>
<body>
<h1 id="spring">Spring Tides</h1>
<p>The keeper wrote the tide table in pencil, because the sea corrected him every week.</p>
<p>Visitors to the café at the harbour asked about the <em>naïve</em> little lamp, and he
told them to read <a href="ch2.xhtml#lamp">the chapter on the winter lamp</a>.</p>
<p>Weather notes: fog <b>three</b> days, rain two, clear one — a fair week.</p>
<p>The almanac is kept at <a href="https://example.org/almanac">the harbour office</a>.</p>
</body></html>"""

CH2 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
<head><title>The Winter Lamp</title><link rel="stylesheet" href="style.css"/></head>
<body>
<h1 id="winter">The Winter Lamp</h1>
<p id="lamp">In winter the lamp burned from four in the afternoon until eight in the morning.</p>
<p class="centre"><img src="lamp.png" alt="A drawing of the lamp"/></p>
<p>Every ship that passed the point was counted, and every count was a little wrong.</p>
</body></html>"""

CSS = "p { text-indent: 1em; margin: 0 } p.centre { text-align: center } h1 { margin: 1em 0 }"


def _png() -> bytes:
    im = Image.new("RGB", (120, 160), (250, 240, 200))
    d = ImageDraw.Draw(im)
    d.rectangle((45, 40, 75, 140), fill=(180, 40, 40))
    d.ellipse((40, 10, 80, 50), fill=(250, 200, 40))
    buf = BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _cover() -> bytes:
    im = Image.new("RGB", (600, 900), (20, 50, 90))
    d = ImageDraw.Draw(im)
    d.rectangle((60, 60, 540, 840), outline=(240, 200, 80), width=12)
    d.rectangle((270, 300, 330, 700), fill=(230, 230, 230))
    d.ellipse((240, 200, 360, 320), fill=(250, 210, 60))
    buf = BytesIO()
    im.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def source_epub(path: Path) -> Path:
    container = (
        '<?xml version="1.0"?><container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container)
        for name, data in (
            ("content.opf", OPF),
            ("nav.xhtml", NAV),
            ("ch1.xhtml", CH1),
            ("ch2.xhtml", CH2),
            ("style.css", CSS),
            ("lamp.png", _png()),
            ("cover.jpg", _cover()),
        ):
            zf.writestr(f"OEBPS/{name}", data)
    return path


def _set_exth(record: bytes, values: dict[int, bytes]) -> bytes:
    """``record`` (a MOBI header record) with EXTH entries ``values`` replaced or added. The
    full name follows the EXTH block, so its offset moves by the change in length."""
    header_len = struct.unpack_from(">I", record, 20)[0]
    at = 16 + header_len
    assert record[at : at + 4] == b"EXTH"
    length, count = struct.unpack_from(">II", record, at + 4)
    entries, pos = [], at + 12
    for _ in range(count):
        kind, size = struct.unpack_from(">II", record, pos)
        if kind not in values:
            entries.append((kind, record[pos + 8 : pos + size]))
        pos += size
    entries += sorted(values.items())
    body = b"".join(struct.pack(">II", k, len(v) + 8) + v for k, v in entries)
    exth = b"EXTH" + struct.pack(">II", 12 + len(body), len(entries)) + body
    exth += b"\0" * (-len(exth) % 4)
    old_end = at + length + (-length % 4)
    delta = len(exth) - (old_end - at)
    out = bytearray(record[:at] + exth + record[old_end:])
    name_offset = struct.unpack_from(">I", out, 84)[0]
    if name_offset >= old_end:
        struct.pack_into(">I", out, 84, name_offset + delta)
    return bytes(out)


def set_asin(data: bytes, asin: bytes) -> bytes:
    """Rewrite every MOBI header's ASIN (EXTH 113) and rebuild the PalmDB record offsets."""
    n = struct.unpack_from(">H", data, 76)[0]
    offsets = [struct.unpack_from(">I", data, 78 + 8 * i)[0] for i in range(n)] + [len(data)]
    records = [data[offsets[i] : offsets[i + 1]] for i in range(n)]
    records = [_set_exth(r, {113: asin}) if r[16:20] == b"MOBI" else r for r in records]
    head = bytearray(data[: offsets[0]])
    pos = offsets[0]
    for i, r in enumerate(records):
        struct.pack_into(">I", head, 78 + 8 * i, pos)
        pos += len(r)
    return bytes(head) + b"".join(records)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        src = source_epub(Path(tmp) / "lighthouse.epub")
        for out, extra in (
            ("lighthouse.mobi", ["--mobi-file-type", "old"]),
            ("lighthouse-joint.mobi", ["--mobi-file-type", "both"]),
            ("lighthouse.azw3", []),
        ):
            dest = HERE / out
            subprocess.run(  # noqa: S603, S607 - a developer tool, fixed argv
                ["ebook-convert", str(src), str(dest), *extra],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            dest.write_bytes(set_asin(dest.read_bytes(), b"B0LECTERN1"))
            print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
