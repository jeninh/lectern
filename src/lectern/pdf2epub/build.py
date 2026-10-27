"""Build a reflowable EPUB 3 from a PDF's canonical text document.

The input is the document :func:`lectern.pdftext.document.build_document` stored next to the
PDF: its sections are the chapters (PDF outline, else detected headings), its blocks are
paragraphs and headings, its word map gives every character's page, and its page labels are
the PDF's ``/PageLabels``. Everything here is a pure function of that document and the work's
metadata, and the zip is deterministic (fixed timestamps and order), so the same PDF and the
same inputs convert to the same bytes: a regeneration that changes nothing keeps the file's
SHA-256, and a changed PDF is a new edition.

Page numbers are metadata, not text (issue #28): printed page numbers and running
headers/footers are already stripped from the text flow by :mod:`lectern.pdftext.layout`
(repetition at the same position across pages), so this module only re-attaches the
pagination as ``epub:type="pagebreak"`` markers — one inline empty ``span`` at every page's
first word — plus a ``page-list`` nav, ``dc:source``, and ``pageBreakSource``.

A scanned PDF converts once OCR has given it a text document (issue #62: the document then
carries ``"ocr": {"engine", "language"}``). Its edition says so everywhere a person or a
reader looks: the package description and a ``lectern:source`` meta, a note in the
navigation document, and the report (``source: "ocr"`` and a warning to expect recognition
errors). Nothing else differs: the same chapters, pagination, and read-along.

Deliberately out of scope here (the routes' docs and the PR carry the deferral list):
images and figures, tables, footnotes as noteref asides, and inline styling. The converted
edition carries the reflowable text and the pagination.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.sax.saxutils import escape, quoteattr

MODIFIED = "1980-01-01T00:00:00Z"  # deterministic, like the zip timestamps
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
CONVERTED_NAME = "book-converted-from-pdf.epub"
REPORT_NAME = "pdf2epub-report.json"
NAV_NAME = "nav.xhtml"

# Characters XML 1.0 forbids outright (PDF-sourced text and /PageLabels can carry them).
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _xml_text(text: str) -> str:
    """PDF-sourced text made XML-safe: control characters dropped, the rest escaped."""
    return escape(_XML_BAD.sub("", text))


XHTML_HEAD = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    "<!DOCTYPE html>\n"
    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"'
    ' xml:lang="{lang}" lang="{lang}">\n<head>\n<title>{title}</title>\n</head>\n<body>\n'
)


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


def _page_starts(section: dict) -> list[tuple[int, int]]:
    """``(char offset, page)`` for every page's first word in a section, in reading order: the
    positions the pagebreak markers and the page list anchor to."""
    out: list[tuple[int, int]] = []
    last: int | None = None
    for word in section.get("words") or []:
        start, page = int(word[0]), int(word[2])
        if last is None or page != last:
            out.append((start, page))
            last = page
    return out


def _markers(sections: list[dict]) -> list[list[tuple[int, int]]]:
    """Per section, its pagebreak markers as ``(char offset, page)``, sorted by offset. Each
    page is claimed by the first section containing any word of it (reading order), so every
    page has at most one marker and marker ids are unique."""
    claimed: set[int] = set()
    out = []
    for section in sections:
        starts = _page_starts(section)
        fallback = int(section.get("page_start") or 0)
        mine: list[tuple[int, int]] = []
        for offset, page in starts or [(0, fallback)]:
            if page in claimed:
                continue
            claimed.add(page)
            mine.append((offset, page))
        mine.sort()
        out.append(mine)
    return out


def _section_xhtml(
    section: dict, index: int, lang: str, labels: list[str], markers: list[tuple[int, int]]
) -> bytes:
    """One section's XHTML: its title as ``h1``, blocks as ``h2``/``p``, with the section's
    pagebreak markers inline in the text flow (before the piece of text they open)."""
    cuts = [m[0] for m in markers]
    marks = dict(markers)

    def block(btext: str, offset: int, tag: str) -> str:
        parts: list[str] = []
        for start, end in _spans(len(btext), offset, cuts):
            piece = _xml_text(btext[start - offset : end - offset])
            page = marks.get(start)
            if page is not None:
                # An empty inline marker: the pagination stays metadata, never text.
                label = _XML_BAD.sub("", labels[page])
                piece = (
                    f'<span epub:type="pagebreak" id="page-{page + 1}" title={quoteattr(label)}/>'
                ) + piece
            parts.append(piece)
        return f"<{tag}>{''.join(parts)}</{tag}>"

    body = []
    text = section["text"]
    blocks = section.get("blocks") or [[0, "p"]]
    ends = [int(b[0]) for b in blocks[1:]] + [len(text)]
    first_start, first_stop = int(blocks[0][0]), ends[0]
    title = section.get("title") or f"Section {index + 1}"
    # The section's own first heading block is its title: it must not print twice.
    title_block = blocks[0][1] == "h" and text[first_start:first_stop].strip() == title.strip()
    if not title_block:
        body.append(f"<h1>{_xml_text(title)}</h1>")
    for b, end in zip(blocks, ends, strict=True):
        start, kind = int(b[0]), b[1]
        stop = end - 1 if end < len(text) else end  # the joining space is no block's
        if stop > start:
            body.append(block(text[start:stop], start, "h2" if kind == "h" else "p"))
    return (
        XHTML_HEAD.format(lang=escape(lang), title=_xml_text(title))
        + "\n".join(body)
        + "\n</body>\n</html>\n"
    ).encode()


def _spans(length: int, offset: int, cuts: list[int]) -> list[tuple[int, int]]:
    """``(absolute start, absolute end)`` pieces of ``[offset, offset+length)``, split at the
    cut points that fall strictly inside it."""
    bounds = [c for c in cuts if offset < c < offset + length]
    spans = []
    prev = offset
    for c in bounds:
        spans.append((prev, c))
        prev = c
    spans.append((prev, offset + length))
    return spans


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
) -> tuple[bytes, dict]:
    """The converted EPUB's bytes and its conversion report. Raises :class:`ConversionError`
    when the document has nothing to convert (a scan with no recognised text yet)."""
    if doc.get("status") != "ready":
        raise ConversionError("needs_ocr")
    ocr = ocr_of(doc)
    sections = doc.get("sections") or []
    if not sections or not doc.get("pages"):
        raise ConversionError("no_text")
    labels = page_labels(doc)

    per_section = _markers(sections)
    page_list = [
        (f"s{i + 1}.xhtml", f"page-{page + 1}", labels[page])
        for i, marks in enumerate(per_section)
        for _offset, page in marks
    ]

    files: list[tuple[str, bytes]] = []
    manifest: list[tuple[str, str, str, str]] = []
    spine = []
    for index, section in enumerate(sections):
        name = f"s{index + 1}.xhtml"
        files.append(
            (name, _section_xhtml(section, index, language or "en", labels, per_section[index]))
        )
        manifest.append((f"s{index + 1}", name, "application/xhtml+xml", ""))
        spine.append(f"s{index + 1}")

    files.append((NAV_NAME, _nav_doc(title, language or "en", sections, page_list, ocr)))
    manifest.append(("nav", NAV_NAME, "application/xhtml+xml", "nav"))
    # The nav document is not in the spine: it is a navigation aid, not content, and it must
    # not join the text timeline (sentences, narration, the reader's spine).

    if cover_jpeg:
        files.append(("cover.jpg", cover_jpeg))
        manifest.append(("cover-image", "cover.jpg", "image/jpeg", "cover-image"))

    files.insert(0, ("content.opf", _opf(
        title=title, author=author, language=language or "en", source_name=source_name,
        pdf_sha256=pdf_sha256, isbn=isbn, manifest=manifest, spine=spine,
        has_cover=bool(cover_jpeg), ocr=ocr,
    )))  # fmt: skip
    return _zip(files), _report(doc, labels, page_list)


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


def _nav_doc(
    title: str,
    lang: str,
    sections: list[dict],
    page_list: list[tuple[str, str, str]],
    ocr: dict | None = None,
) -> bytes:
    head = XHTML_HEAD.format(lang=escape(lang), title=_xml_text(f"{title} — Contents"))
    lines = [f'<nav epub:type="toc" id="toc"><h1>{_xml_text(title)}</h1><ol>']
    for i, section in enumerate(sections):
        lines.append(
            f'<li><a href="s{i + 1}.xhtml">'
            f"{_xml_text(section.get('title') or f'Section {i + 1}')}</a></li>"
        )
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
        # Where the page numbers come from: this PDF's pagination.
        f'<meta property="pageBreakSource">urn:sha256:{pdf_sha256}</meta>',
        f'<meta property="dcterms:modified">{MODIFIED}</meta>',
    ]
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


def _report(doc: dict, labels: list[str], page_list: list[tuple[str, str, str]]) -> dict:
    """What the conversion did and what a person should check before trusting it."""
    sections = doc.get("sections") or []
    stats = doc.get("stats") or {}
    pages_with_words = {int(w[2]) for s in sections for w in s.get("words") or []}
    empty = [i + 1 for i, p in enumerate(doc.get("pages") or []) if not p.get("text")]
    unlinked = sorted(set(range(1, len(labels) + 1)) - {p + 1 for p in pages_with_words})
    warnings: list[str] = []
    ocr = ocr_of(doc)
    if ocr is not None:
        warnings.append(ocr_note(ocr))
    if doc.get("stats", {}).get("truncated"):
        warnings.append("the PDF was truncated at the extractor's page cap")
    if not any(s.get("from_outline") for s in sections):
        warnings.append("chapters were inferred from headings (the PDF has no outline)")
    if unlinked:
        warnings.append(
            f"no anchor for page(s) {unlinked} (no text on them); they are missing from the page list"
        )
    return {
        "pdf_sha256": doc.get("pdf_sha256"),
        "status": doc.get("status"),
        "source": "ocr" if ocr is not None else "text_layer",
        "ocr": ocr,
        "pages": stats.get("pages", len(labels)),
        "sections": len(sections),
        "page_list_entries": len(page_list),
        "labels_source": (
            "page_labels" if any(p.get("label") for p in doc.get("pages") or []) else "physical"
        ),
        "warnings": warnings,
        "pages_without_text": empty,
        "unlinked_pages": unlinked,
    }
