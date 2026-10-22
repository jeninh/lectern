"""Book file formats: which ones Lectern takes, how each is recognised, and one entry point to
read any of them.

Lectern files three kinds of reading copy, all ``book_files`` rows of kind ``ebook``:

==========  ===================================  =========  ============================
Format      Media type                           Extension  Recognised by
==========  ===================================  =========  ============================
EPUB        ``application/epub+zip``             ``.epub``  zip local header ``PK\\x03\\x04``
PDF         ``application/pdf``                  ``.pdf``   ``%PDF-`` at offset 0
Comic       ``application/vnd.comicbook+zip``    ``.cbz``   zip local header ``PK\\x03\\x04``
==========  ===================================  =========  ============================

The declared media type picks the parser; the magic bytes must agree before any parser runs,
so a renamed file is refused in plain words instead of reaching the wrong parser.

Kindle books are taken but not stored: a DRM-free MOBI or AZW3 (PalmDB header with
``BOOKMOBI`` at offset 60) is converted to EPUB at ingest (:func:`to_epub`,
lectern.ingest.kindle) and filed as that EPUB; a DRM-protected one is refused
(``drm_protected``). KFX, Topaz, and other Kindle containers are not MOBI files.

Only EPUB has text. Everything built on text (sentences, alignment, read-along, the read-aloud
export, KOReader xpointer projection) uses EPUB files only; PDF and comics are page-based, and
KOReader syncs them as page numbers (``lectern.routes.api.pages``).

CBR (RAR) is not supported: reading RAR safely needs a maintained RAR decoder, and the Python
options either shell out to the non-free ``unrar`` or go through libarchive's RAR code. Convert
CBR to CBZ (unpack and zip the images) before adding it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

EPUB = "application/epub+zip"
PDF = "application/pdf"
CBZ = "application/vnd.comicbook+zip"

#: media type -> (stored extension, label shown to people, error code for a wrong file)
DOCUMENTS: dict[str, tuple[str, str, str]] = {
    EPUB: (".epub", "EPUB", "not_epub"),
    PDF: (".pdf", "PDF", "not_pdf"),
    CBZ: (".cbz", "CBZ", "not_comic"),
}
PAGED = frozenset({PDF, CBZ})
MOBI = "application/x-mobipocket-ebook"
AZW3 = "application/vnd.amazon.mobi8-ebook"
#: Kindle formats converted to EPUB at ingest: media type -> (extension, label).
KINDLE: dict[str, tuple[str, str]] = {MOBI: (".mobi", "MOBI"), AZW3: (".azw3", "AZW3")}
BY_SUFFIX = {ext: mt for mt, (ext, _label, _code) in DOCUMENTS.items()} | {
    ext: mt for mt, (ext, _label) in KINDLE.items()
}
#: One line for upload menus, error messages, and the MCP upload tool's description.
FORMATS_SUMMARY = (
    "EPUB, PDF, or CBZ comics (CBR is not supported: convert it to CBZ), DRM-free MOBI or AZW3 "
    "(converted to EPUB), and M4B/M4A, MP3, OGG/Opus, or FLAC audiobooks"
)

_ZIP_MAGIC = b"PK\x03\x04"


class FormatError(ValueError):
    """The file is not a readable book of its declared format. ``code`` is machine-readable
    (``not_pdf``, ``pdf_encrypted``, ``too_many_pages``, ``archive_too_large``, ...)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


class ProbeBusy(Exception):
    """Every parser slot stayed taken while we waited (a PDF in ``lectern.ingest.pdf``, a MOBI
    conversion in ``lectern.ingest.kindle``). The file is not at fault: callers keep it and ask
    to retry (HTTP 503 ``pdf_busy`` or ``convert_busy``) instead of refusing it."""

    def __init__(self, code: str = "pdf_busy") -> None:
        super().__init__(code)
        self.code = code


#: Codes of :class:`ProbeBusy`: retry later, the file is fine.
BUSY_CODES = frozenset({"pdf_busy", "convert_busy"})
# At most this many parser children at once in this server process (PDF reads and MOBI/AZW3
# conversions together: uploads, imports, inbox, the editor's "use the book's own cover"), each
# limited in memory and time. A caller that cannot get a slot in time gets ProbeBusy.
MAX_PARSERS = 2
PARSER_SLOTS = threading.BoundedSemaphore(MAX_PARSERS)


def is_document(media_type: str | None) -> bool:
    """A format Lectern stores (EPUB, PDF, CBZ)."""
    return media_type in DOCUMENTS


def is_kindle(media_type: str | None) -> bool:
    """MOBI or AZW3: converted to EPUB before anything else reads it."""
    return media_type in KINDLE


def is_book(media_type: str | None) -> bool:
    """A book file (as opposed to audio) that ingest accepts."""
    return is_document(media_type) or is_kindle(media_type)


def suffix_of(media_type: str) -> str:
    return DOCUMENTS[media_type][0]


def label_of(media_type: str) -> str:
    if media_type in KINDLE:
        return KINDLE[media_type][1]
    return DOCUMENTS.get(media_type, (None, "file", None))[1]


def wrong_format_code(media_type: str) -> str:
    return "not_mobi" if media_type in KINDLE else DOCUMENTS[media_type][2]


def media_type_for(path: Path) -> str | None:
    """The book format a file name claims (inbox files have no declared type)."""
    return BY_SUFFIX.get(path.suffix.lower())


def magic_ok(media_type: str, head: bytes) -> bool:
    """Do the first bytes of a file match its declared book format?"""
    if media_type == PDF:
        return head.startswith(b"%PDF-")
    if media_type in (EPUB, CBZ):
        return head.startswith(_ZIP_MAGIC)
    if media_type in KINDLE:
        return head[60:68] == b"BOOKMOBI"
    return False


def read_head(path: Path, n: int = 68) -> bytes:
    with path.open("rb") as fh:
        return fh.read(n)


@dataclass
class Probe:
    """What ingest needs from a book file: its metadata (the EPUB shape, shared by all
    formats), a JPEG-able first-page image for formats without an embedded cover, and the
    page count of page-based formats."""

    meta: object  # lectern.ingest.epub.EpubMetadata
    cover: bytes | None = None
    pages: int | None = None


def to_epub(path: Path, media_type: str, dest: Path) -> None:
    """Convert a MOBI or AZW3 to an EPUB at ``dest`` after checking its magic bytes; raises
    :class:`FormatError` (``not_mobi``, ``drm_protected``, ``mobi_too_complex``) or
    :class:`ProbeBusy` (``convert_busy``). The original is left where it is."""
    from lectern.ingest.kindle import to_epub as convert

    try:
        head = read_head(path)
    except OSError as exc:
        raise FormatError("not_mobi") from exc
    if not magic_ok(media_type, head):
        raise FormatError("not_mobi", "magic bytes do not match")
    convert(path, dest)


def probe(path: Path, media_type: str) -> Probe:
    """Check the magic bytes and read ``path`` as ``media_type``; raises :class:`FormatError`.

    PDF parsing and rendering run in a resource-limited child process (lectern.ingest.pdf);
    comic archives are checked against entry, size, and ratio caps before any image is read
    (lectern.ingest.comic).
    """
    from lectern.ingest.epub import EpubError, read_epub_metadata

    if media_type not in DOCUMENTS:
        raise FormatError("unsupported_media_type")
    try:
        head = read_head(path)
    except OSError as exc:
        raise FormatError(wrong_format_code(media_type)) from exc
    if not magic_ok(media_type, head):
        raise FormatError(wrong_format_code(media_type), "magic bytes do not match")
    if media_type == EPUB:
        try:
            return Probe(read_epub_metadata(path))
        except EpubError as exc:
            raise FormatError("not_epub", str(exc)) from exc
    if media_type == PDF:
        from lectern.ingest.pdf import probe_pdf

        return probe_pdf(path)
    from lectern.ingest.comic import probe_comic

    return probe_comic(path)
