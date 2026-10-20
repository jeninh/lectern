"""EPUB metadata extraction from the OPF package document, via lxml."""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
MAX_XML_BYTES = 4 * 1024 * 1024
MAX_COVER_BYTES = 10 * 1024 * 1024  # = lectern.ingest.cover.COVER_MAX_BYTES


class EpubError(ValueError):
    pass


@dataclass
class EpubMetadata:
    title: str
    authors: list[str] = field(default_factory=list)
    language: str | None = None
    isbn: str | None = None
    identifiers: list[str] = field(default_factory=list)
    description: str | None = None
    publisher: str | None = None
    published: str | None = None
    cover_href: str | None = None  # path inside the zip
    cover_media_type: str | None = None
    series: str | None = None
    series_index: float | None = None
    narrators: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


def _read_xml(zf: zipfile.ZipFile, name: str) -> etree._Element:
    info = zf.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise EpubError(f"{name} is too large")
    return etree.fromstring(zf.read(name), _PARSER)


def normalize_isbn(raw: str) -> str | None:
    digits = re.sub(r"[^0-9Xx]", "", raw).upper()
    if len(digits) == 13 and digits.isdigit() and _isbn13_ok(digits):
        return digits
    if len(digits) == 10 and _isbn10_ok(digits):
        return _isbn10_to_13(digits)
    return None


def _isbn10_ok(s: str) -> bool:
    total = 0
    for i, ch in enumerate(s):
        val = 10 if ch == "X" and i == 9 else (int(ch) if ch.isdigit() else -1)
        if val < 0:
            return False
        total += (10 - i) * val
    return total % 11 == 0


def _isbn13_ok(s: str) -> bool:
    total = sum(int(ch) * (1 if i % 2 == 0 else 3) for i, ch in enumerate(s))
    return total % 10 == 0


def _isbn10_to_13(s: str) -> str:
    core = "978" + s[:9]
    check = (10 - sum(int(ch) * (1 if i % 2 == 0 else 3) for i, ch in enumerate(core)) % 10) % 10
    return core + str(check)


def _text(el) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _open_zip(path: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise EpubError("not a zip file") from exc


def _read_opf(zf: zipfile.ZipFile) -> tuple[etree._Element, str]:
    """The package document and its path inside the zip."""
    try:
        container = _read_xml(zf, "META-INF/container.xml")
    except KeyError as exc:
        raise EpubError("missing META-INF/container.xml") from exc
    rootfile = container.find(".//c:rootfile", NS)
    if rootfile is None or not rootfile.get("full-path"):
        raise EpubError("container.xml has no rootfile")
    opf_path = rootfile.get("full-path")
    try:
        return _read_xml(zf, opf_path), opf_path
    except KeyError as exc:
        raise EpubError(f"missing OPF {opf_path}") from exc


def read_spine(path: Path, *, strict: bool = True) -> list[str]:
    """Zip member names of the spine items in reading order.

    The list index is the ``spine_index`` used by sentence maps, ``sentences`` rows, and
    KOReader's ``DocFragment[n]`` (which is this index plus one). An itemref missing from the
    manifest raises ``EpubError``; with ``strict=False`` it keeps its index with the name
    ``""`` instead (no zip member has that name), so the items after it keep theirs.
    """
    with _open_zip(path) as zf:
        opf, opf_path = _read_opf(zf)
        opf_dir = posixpath.dirname(opf_path)
        manifest = opf.find("opf:manifest", NS)
        spine = opf.find("opf:spine", NS)
        if manifest is None or spine is None:
            raise EpubError("OPF has no manifest or spine")
        hrefs = {it.get("id"): it.get("href") for it in manifest.findall("opf:item", NS)}
        names = []
        for ref in spine.findall("opf:itemref", NS):
            href = hrefs.get(ref.get("idref"))
            if not href:
                if strict:
                    raise EpubError(f"spine itemref {ref.get('idref')!r} is not in the manifest")
                names.append("")
                continue
            names.append(posixpath.normpath(posixpath.join(opf_dir, href)))
        return names


def read_epub_metadata(path: Path) -> EpubMetadata:
    with _open_zip(path) as zf:
        opf, opf_path = _read_opf(zf)
        meta = opf.find("opf:metadata", NS)
        if meta is None:
            raise EpubError("OPF has no metadata")
        opf_dir = posixpath.dirname(opf_path)

        title = _text(meta.find("dc:title", NS)) or path.stem
        refined = {
            (m.get("refines") or "")[1:]: _text(m)
            for m in meta.findall("opf:meta", NS)
            if m.get("property") == "role"
        }
        authors = [
            _text(c)
            for c in meta.findall("dc:creator", NS)
            if _text(c)
            and (
                c.get(f"{{{NS['opf']}}}role") or (refined.get(c.get("id")) if c.get("id") else None)
            )
            in (None, "aut")
        ]
        language = _text(meta.find("dc:language", NS)) or None
        identifiers = [_text(i) for i in meta.findall("dc:identifier", NS) if _text(i)]
        isbn = None
        for ident in identifiers:
            candidate = normalize_isbn(ident.split(":")[-1] if "isbn" in ident.lower() else ident)
            if candidate and (("isbn" in ident.lower()) or re.fullmatch(r"[0-9Xx-]{10,17}", ident)):
                isbn = candidate
                break
        description = _text(meta.find("dc:description", NS)) or None
        publisher = _text(meta.find("dc:publisher", NS)) or None
        published = _text(meta.find("dc:date", NS)) or None

        cover_href, cover_type = _find_cover(opf, meta, opf_dir, zf)
        series, series_index = _series(meta)
        narrators = _narrators(meta)
        tags = [_text(t)[:128] for t in meta.findall("dc:subject", NS) if _text(t)][:50]

    return EpubMetadata(
        title=title,
        authors=authors,
        language=language[:16] if language else None,
        isbn=isbn,
        identifiers=identifiers,
        description=description,
        publisher=publisher,
        published=published[:32] if published else None,
        cover_href=cover_href,
        cover_media_type=cover_type,
        series=series,
        series_index=series_index,
        narrators=narrators,
        tags=tags,
    )


def _number(v: str | None) -> float | None:
    try:
        n = float((v or "").strip())
    except ValueError:
        return None
    return n if 0 <= n <= 100000 else None


def _series(meta) -> tuple[str | None, float | None]:
    """Calibre's ``calibre:series`` metas, else EPUB 3 ``belongs-to-collection``."""
    calibre = {m.get("name"): m.get("content") for m in meta.findall("opf:meta", NS)}
    if calibre.get("calibre:series"):
        return calibre["calibre:series"].strip()[:256], _number(calibre.get("calibre:series_index"))
    metas = meta.findall("opf:meta", NS)
    for m in metas:
        if m.get("property") != "belongs-to-collection" or not _text(m):
            continue
        ref = f"#{m.get('id')}" if m.get("id") else None
        kind = position = None
        for r in metas:
            if ref and r.get("refines") == ref:
                if r.get("property") == "collection-type":
                    kind = _text(r)
                elif r.get("property") == "group-position":
                    position = _number(_text(r))
        if kind in (None, "series"):
            return _text(m)[:256], position
    return None, None


def _narrators(meta) -> list[str]:
    """``dc:contributor`` (or ``dc:creator``) whose role is MARC ``nrt``, in EPUB 2 or 3 form."""
    roles: dict[str, str] = {}
    for m in meta.findall("opf:meta", NS):
        if m.get("property") == "role" and (m.get("refines") or "").startswith("#"):
            roles[m.get("refines")[1:]] = _text(m)
    out: list[str] = []
    for el in meta.findall("dc:contributor", NS) + meta.findall("dc:creator", NS):
        role = el.get(f"{{{NS['opf']}}}role") or roles.get(el.get("id") or "")
        if role == "nrt" and _text(el) and _text(el) not in out:
            out.append(_text(el)[:128])
    return out[:20]


def _find_cover(opf, meta, opf_dir: str, zf: zipfile.ZipFile) -> tuple[str | None, str | None]:
    manifest = opf.find("opf:manifest", NS)
    if manifest is None:
        return None, None
    items = {it.get("id"): it for it in manifest.findall("opf:item", NS)}
    candidate = None
    # EPUB 3: properties="cover-image"
    for it in items.values():
        if "cover-image" in (it.get("properties") or "").split():
            candidate = it
            break
    # EPUB 2: <meta name="cover" content="item-id"/>
    if candidate is None:
        for m in meta.findall("opf:meta", NS):
            if m.get("name") == "cover" and m.get("content") in items:
                candidate = items[m.get("content")]
                break
    if candidate is None:
        for it in items.values():
            if (it.get("media-type") or "").startswith("image/") and "cover" in (
                it.get("id") or ""
            ).lower():
                candidate = it
                break
    if candidate is None or not candidate.get("href"):
        return None, None
    href = posixpath.normpath(posixpath.join(opf_dir, candidate.get("href")))
    try:
        info = zf.getinfo(href)
    except KeyError:
        return None, None
    if info.file_size > MAX_COVER_BYTES:
        return None, None
    return href, candidate.get("media-type")


def extract_cover(path: Path, href: str, dest: Path) -> bool:
    """Write the cover image as JPEG to dest. Returns False if it cannot be decoded, or if
    the entry or the image it decodes to is too large (checked before decompressing)."""
    from lectern.ingest.cover import EMBEDDED_FORMATS, CoverRejected, save_cover_jpeg

    with zipfile.ZipFile(path) as zf:
        try:
            info = zf.getinfo(href)
        except KeyError:
            return False
        if info.file_size > MAX_COVER_BYTES:
            return False
        try:  # the header's size may lie: read at most one byte more than the cap
            with zf.open(info) as fh:
                data = fh.read(MAX_COVER_BYTES + 1)
        except Exception:  # noqa: BLE001 - a corrupt entry (bad CRC, bad deflate) is no cover
            return False
    if len(data) > MAX_COVER_BYTES:
        return False
    try:
        save_cover_jpeg(data, dest, formats=EMBEDDED_FORMATS, quality=85)
        return True
    except (CoverRejected, OSError):  # a corrupt image is not fatal to ingest
        return False
