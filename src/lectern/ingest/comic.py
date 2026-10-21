"""CBZ comics: a zip of page images, optionally with a ComicInfo.xml (the ComicRack schema).

Every archive is checked before any page is read (:func:`open_comic`):

- at most :data:`MAX_ENTRIES` entries and :data:`MAX_PAGES` page images;
- no entry larger than :data:`MAX_ENTRY_BYTES` uncompressed, no more than
  :data:`MAX_TOTAL_BYTES` in total, and no entry over 1 MiB that claims to expand more than
  :data:`MAX_RATIO` times (the shape of a zip bomb; real page images barely compress);
- no encrypted entries; overlapping entries are refused by :mod:`zipfile` itself.

Nothing is ever extracted to disk: pages are read into memory one at a time, at most one byte
more than the size the archive declares (so a lying header is caught), and an image is
served only if its first bytes are JPEG, PNG, GIF, or WebP. Covers go through the bounded
decoder in :mod:`lectern.ingest.cover`.
"""

from __future__ import annotations

import posixpath
import re
import threading
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from lectern.ingest.formats import FormatError, Probe

MAX_ENTRIES = 10_000
MAX_PAGES = 5_000
MAX_ENTRY_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_RATIO = 100
MAX_COMICINFO_BYTES = 1024 * 1024
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
_NATURAL = re.compile(r"(\d+)")


@dataclass
class Comic:
    """A checked archive: page entry names in reading order, plus ComicInfo when present."""

    pages: list[str]
    info: dict[str, str] = field(default_factory=dict)
    cover_index: int = 0
    rtl: bool = False


def _natural(s: str) -> list:
    return [int(p) if p.isdigit() else p.lower() for p in _NATURAL.split(s)]


def _is_page(name: str) -> bool:
    parts = name.split("/")
    if name.endswith("/") or name.startswith("/") or ".." in parts:
        return False
    if any(p.startswith(".") or p == "__MACOSX" for p in parts):
        return False
    return posixpath.splitext(name)[1].lower() in IMAGE_SUFFIXES


def _read(zf: zipfile.ZipFile, info: zipfile.ZipInfo, cap: int) -> bytes:
    if info.file_size > cap:
        raise FormatError("archive_too_large", f"{info.filename} is too large")
    try:
        with zf.open(info) as fh:
            data = fh.read(info.file_size + 1)
    except (zipfile.BadZipFile, OSError, EOFError, RuntimeError) as exc:
        raise FormatError("not_comic", f"{info.filename}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - a corrupt deflate stream (zlib.error)
        raise FormatError("not_comic", f"{info.filename}: {type(exc).__name__}") from exc
    if len(data) > info.file_size:
        raise FormatError("archive_too_large", f"{info.filename} is larger than it says")
    return data


def _check(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise FormatError("archive_too_large", f"{len(infos)} entries")
    total = 0
    for i in infos:
        if i.flag_bits & 0x1:
            raise FormatError("comic_encrypted", i.filename)
        if i.file_size > MAX_ENTRY_BYTES:
            raise FormatError("archive_too_large", f"{i.filename} is too large")
        if i.file_size > 1024 * 1024 and i.file_size > MAX_RATIO * max(i.compress_size, 1):
            raise FormatError("archive_too_large", f"{i.filename} expands too much")
        total += i.file_size
        if total > MAX_TOTAL_BYTES:
            raise FormatError("archive_too_large", "too much data")
    return infos


def _comicinfo(zf: zipfile.ZipFile, infos: list[zipfile.ZipInfo]) -> etree._Element | None:
    ci = next((i for i in infos if i.filename.lower() == "comicinfo.xml"), None)
    if ci is None:
        return None
    try:
        return etree.fromstring(_read(zf, ci, MAX_COMICINFO_BYTES), _PARSER)
    except (etree.XMLSyntaxError, FormatError):
        return None  # a broken ComicInfo means no metadata, not a broken comic


def open_comic(path: Path) -> Comic:
    """Check the archive and list its pages; raises :class:`FormatError`."""
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise FormatError("not_comic", str(exc)) from exc
    with zf:
        infos = _check(zf)
        pages = sorted((i.filename for i in infos if _is_page(i.filename)), key=_natural)
        if not pages:
            raise FormatError("not_comic", "no page images")
        if len(pages) > MAX_PAGES:
            raise FormatError("too_many_pages", f"{len(pages)} pages")
        root = _comicinfo(zf, infos)
    info: dict[str, str] = {}
    cover = 0
    rtl = False
    if root is not None:
        for el in root:
            if isinstance(el.tag, str) and len(el) == 0 and (el.text or "").strip():
                info[etree.QName(el).localname] = " ".join(el.text.split())[:4096]
        rtl = info.get("Manga", "").lower() == "yesandrighttoleft"
        for p in root.iter("{*}Page", "Page"):
            if (p.get("Type") or "").lower() == "frontcover":
                try:
                    idx = int(p.get("Image") or "")
                except ValueError:
                    break
                if 0 <= idx < len(pages):
                    cover = idx
                break
    return Comic(pages=pages, info=info, cover_index=cover, rtl=rtl)


_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def image_type(data: bytes) -> str | None:
    for magic, mt in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mt
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def read_page(path: Path, name: str) -> tuple[bytes, str]:
    """The bytes and image type of one page entry (already listed by :func:`open_comic`)."""
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise FormatError("not_comic", str(exc)) from exc
    with zf:
        try:
            info = zf.getinfo(name)
        except KeyError as exc:
            raise FormatError("not_comic", "no such page") from exc
        data = _read(zf, info, MAX_ENTRY_BYTES)
    mt = image_type(data)
    if mt is None:
        raise FormatError("not_an_image", name)
    return data, mt


def _number(v: str | None) -> float | None:
    try:
        n = float((v or "").strip())
    except ValueError:
        return None
    return n if 0 <= n <= 100000 else None


def _names(v: str | None) -> list[str]:
    return [n.strip()[:256] for n in (v or "").split(",") if n.strip()][:20]


def probe_comic(path: Path) -> Probe:
    """Metadata from ComicInfo.xml (when present) and the front cover page."""
    from lectern.ingest.cover import COVER_MAX_BYTES
    from lectern.ingest.epub import EpubMetadata, normalize_isbn

    comic = open_comic(path)
    i = comic.info
    series = i.get("Series", "")[:256] or None
    number = i.get("Number")
    title = i.get("Title") or (f"{series} #{number}" if series and number else series)
    y, m, d = i.get("Year", ""), i.get("Month", ""), i.get("Day", "")
    published = None
    if re.fullmatch(r"\d{4}", y):
        published = y
        if m.isdigit() and 1 <= int(m) <= 12:
            published += f"-{int(m):02d}"
            if d.isdigit() and 1 <= int(d) <= 31:
                published += f"-{int(d):02d}"
    tags = []
    for t in _names(i.get("Genre")) + _names(i.get("Tags")):
        if t not in tags:
            tags.append(t[:128])
    cover = None
    try:
        data, _mt = read_page(path, comic.pages[comic.cover_index])
        cover = data if len(data) <= COVER_MAX_BYTES else None
    except FormatError:
        cover = None
    return Probe(
        EpubMetadata(
            title=(title or path.stem)[:512],
            authors=_names(i.get("Writer")),
            language=(i.get("LanguageISO") or None) and i["LanguageISO"][:16],
            isbn=normalize_isbn(i.get("GTIN") or "") if i.get("GTIN") else None,
            description=i.get("Summary") or None,
            publisher=i.get("Publisher", "")[:256] or None,
            published=published,
            series=series,
            series_index=_number(number) if series else None,
            tags=tags[:50],
        ),
        cover=cover,
        pages=len(comic.pages),
    )


class ComicCache:
    """Page lists of recently opened comics, keyed by file and content digest."""

    _lock = threading.Lock()
    _items: OrderedDict[tuple[str, str], Comic] = OrderedDict()
    MAX = 64

    @classmethod
    def get(cls, path: Path, sha256: str) -> Comic:
        key = (str(path), sha256)
        with cls._lock:
            if key in cls._items:
                cls._items.move_to_end(key)
                return cls._items[key]
        comic = open_comic(path)
        with cls._lock:
            cls._items[key] = comic
            while len(cls._items) > cls.MAX:
                cls._items.popitem(last=False)
        return comic
