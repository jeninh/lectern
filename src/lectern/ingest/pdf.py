"""PDF metadata and first-page covers, read in a resource-limited child process.

PDF parsers are large native code (PDFium) and PDFs are untrusted, so the server never opens
one in its own process. :func:`probe_pdf` runs ``python -I -m lectern.ingest.pdf`` with the
file; the child sets hard limits on itself before importing a parser (address space, CPU
seconds, output file size, no core dumps), and the parent kills it after a wall-clock timeout.
Whatever the PDF does, the worst case is a refused upload.

What is read
    page count (refused above :data:`MAX_PAGES`); the document Info dictionary (Title, Author,
    Subject, Keywords) through PDFium, and XMP (``dc:title``, ``dc:creator``, ``dc:language``,
    ``dc:description``) through pypdf, which fills what Info lacks (XMP creators win over a
    single Info ``Author`` string because they are already a list); and the first page,
    rendered to at most 1600 px on its long side as the cover.

No JavaScript
    pypdfium2's PDFium build has no V8 engine, and the child never initialises the form-fill
    environment (the only place PDFium dispatches document JavaScript). pypdf only parses.
    Password-protected PDFs (a user password is needed to open them) are refused.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from lectern.ingest.formats import PARSER_SLOTS, FormatError, Probe, ProbeBusy

MAX_PAGES = 10_000
MAX_PAGE_SIDE_PT = 14_400  # the PDF 1.7 limit (200 inches); larger pages are not rendered
COVER_LONG_SIDE_PX = 1600
TIMEOUT_S = 45
MEMORY_LIMIT_BYTES = 1536 * 1024 * 1024
CPU_LIMIT_S = 30
OUTPUT_LIMIT_BYTES = 16 * 1024 * 1024
MAX_FIELD = 2048
# Parser children share lectern.ingest.formats.PARSER_SLOTS with MOBI conversions. A caller
# that cannot get a slot within BUSY_WAIT_S gets ProbeBusy and retries later.
BUSY_WAIT_S = 10.0
_JUNK_TITLE = re.compile(r"^(microsoft (word|powerpoint) - )", re.I)
_FILE_SUFFIX = re.compile(r"\.(docx?|pptx?|odt|rtf|pdf|indd|qxd|tex|txt)$", re.I)
_ISBN = re.compile(r"(?:isbn[-: ]*)?((?:97[89][- ]?)?(?:\d[- ]?){9}[\dXx])", re.I)


def probe_pdf(path: Path) -> Probe:
    """Read ``path`` in the sandboxed child; raises :class:`FormatError`, or
    :class:`ProbeBusy` when no parser slot frees up within :data:`BUSY_WAIT_S`."""
    if not PARSER_SLOTS.acquire(timeout=BUSY_WAIT_S):
        raise ProbeBusy()
    try:
        return _probe_pdf(path)
    finally:
        PARSER_SLOTS.release()


def _probe_pdf(path: Path) -> Probe:
    from lectern.ingest.cover import COVER_MAX_BYTES
    from lectern.ingest.epub import EpubMetadata

    with tempfile.TemporaryDirectory(prefix="lectern-pdf-") as tmp:
        cover_out = Path(tmp) / "cover.jpg"
        try:
            res = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [sys.executable, "-I", "-m", "lectern.ingest.pdf", str(path), str(cover_out)],
                capture_output=True,
                timeout=TIMEOUT_S,
                check=False,
                env={"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"},
                cwd=tmp,
            )
        except subprocess.TimeoutExpired:
            raise FormatError("pdf_too_complex", "timed out") from None
        try:
            out = json.loads(res.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        except (ValueError, IndexError):
            # killed by a limit (memory, CPU) or crashed in native code
            raise FormatError("pdf_too_complex", f"child exited {res.returncode}") from None
        if not out.get("ok"):
            raise FormatError(out.get("error") or "not_pdf", out.get("detail") or "")
        cover = None
        try:
            if cover_out.is_file() and cover_out.stat().st_size <= COVER_MAX_BYTES:
                cover = cover_out.read_bytes()
        except OSError:
            cover = None
    info, xmp = out.get("info") or {}, out.get("xmp") or {}
    title = clean_title(info.get("Title")) or clean_title(xmp.get("title"))
    authors = [a for a in (xmp.get("creators") or []) if a][:20] or split_authors(
        info.get("Author") or ""
    )
    keywords = info.get("Keywords") or ""
    tags = [t.strip()[:128] for t in re.split(r"[;,]", keywords) if t.strip()][:50]
    description = (info.get("Subject") or xmp.get("description") or "").strip() or None
    return Probe(
        EpubMetadata(
            title=title or path.stem,
            authors=authors,
            language=(xmp.get("language") or None) and xmp["language"][:16],
            isbn=_find_isbn(" ".join([keywords, description or "", title or ""])),
            description=description,
            tags=tags,
        ),
        cover=cover,
        pages=int(out.get("pages") or 0) or None,
    )


def clean_title(raw: str | None) -> str | None:
    """Drop what authoring tools put in Title instead of a title (``Microsoft Word - x.docx``,
    ``untitled``)."""
    t = " ".join((raw or "").split())
    t = _FILE_SUFFIX.sub("", _JUNK_TITLE.sub("", t)).strip()
    if not t or t.lower() in {"untitled", "unknown", "title", "document", "none"}:
        return None
    return t[:512]


def split_authors(raw: str) -> list[str]:
    """``A; B``, ``A & B``, and ``A and B`` are several people; a comma is not (``Last, First``)."""
    parts = re.split(r"\s*(?:;|&|\band\b)\s*", " ".join(raw.split()))
    return [p[:256] for p in parts if p and p.lower() not in {"unknown", "author"}][:20]


def _find_isbn(text: str) -> str | None:
    from lectern.ingest.epub import normalize_isbn

    for m in _ISBN.finditer(text or ""):
        isbn = normalize_isbn(m.group(1))
        if isbn:
            return isbn
    return None


# --- the child --------------------------------------------------------------------------------


def _limit() -> None:
    """In the child. A PDF → EPUB job's parent passes its ``convert`` memory cap and nice
    level down (lectern.limits); an import's probe uses the constants here."""
    import resource

    from lectern.limits import child_memory_bytes, lower_priority

    for which, value in (
        (resource.RLIMIT_AS, child_memory_bytes(MEMORY_LIMIT_BYTES)),
        (resource.RLIMIT_CPU, CPU_LIMIT_S),
        (resource.RLIMIT_FSIZE, OUTPUT_LIMIT_BYTES),
        (resource.RLIMIT_CORE, 0),
    ):
        try:
            resource.setrlimit(which, (value, value))
        except (ValueError, OSError):
            pass  # best effort where the platform refuses a limit
    lower_priority(10)


def _s(v) -> str:
    return str(v or "").replace("\x00", "")[:MAX_FIELD].strip()


def _xmp(path: Path) -> dict:
    try:
        from pypdf import PdfReader

        x = PdfReader(str(path), strict=False).xmp_metadata
    except Exception:  # noqa: BLE001 - XMP is optional; any parse trouble means none
        return {}
    if x is None:
        return {}

    def first(v):
        if isinstance(v, dict):
            return _s(v.get("x-default") or next(iter(v.values()), ""))
        if isinstance(v, list):
            return _s(v[0]) if v else ""
        return _s(v)

    out = {}
    with contextlib.suppress(Exception):
        out["title"] = first(x.dc_title)
    with contextlib.suppress(Exception):
        out["description"] = first(x.dc_description)
    with contextlib.suppress(Exception):
        out["creators"] = [_s(c) for c in (x.dc_creator or [])][:20]
    with contextlib.suppress(Exception):
        out["language"] = first(x.dc_language)[:16]
    return out


def _child(path: Path, cover_out: Path) -> dict:
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(str(path))  # no password: protected files fail here
    except pdfium.PdfiumError as exc:
        code = "pdf_encrypted" if "password" in str(exc).lower() else "not_pdf"
        return {"ok": False, "error": code, "detail": str(exc)[:200]}
    try:
        pages = len(doc)
        if pages < 1:
            return {"ok": False, "error": "not_pdf", "detail": "no pages"}
        if pages > MAX_PAGES:
            return {"ok": False, "error": "too_many_pages", "detail": f"{pages} pages"}
        info = {
            k: _s(v)
            for k, v in doc.get_metadata_dict(skip_empty=True).items()
            if k in ("Title", "Author", "Subject", "Keywords")
        }
        page = doc[0]
        w, h = page.get_size()
        if 0 < w <= MAX_PAGE_SIDE_PT and 0 < h <= MAX_PAGE_SIDE_PT:
            scale = COVER_LONG_SIDE_PX / max(w, h)
            image = page.render(scale=scale).to_pil().convert("RGB")
            image.save(cover_out, "JPEG", quality=85)
    finally:
        doc.close()
    return {"ok": True, "pages": pages, "info": info, "xmp": _xmp(path)}


def main(argv: list[str]) -> int:
    _limit()
    try:
        result = _child(Path(argv[0]), Path(argv[1]))
    except MemoryError:
        result = {"ok": False, "error": "pdf_too_complex", "detail": "memory limit"}
    except Exception as exc:  # noqa: BLE001 - anything else is an unreadable PDF
        result = {"ok": False, "error": "not_pdf", "detail": type(exc).__name__}
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
