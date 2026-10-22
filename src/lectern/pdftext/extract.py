"""Words and their boxes from a PDF's text layer, read in the sandboxed PDF child.

PDFs are untrusted, so this never opens one in the server's process. :func:`extract_words`
runs ``python -I -m lectern.pdftext.extract`` once per batch of :data:`BATCH_PAGES` pages, each
child with exactly the limits PDF ingest uses (:mod:`lectern.ingest.pdf`: address space, CPU
seconds, output file size, no core dumps, ``nice``, the wall-clock timeout, the page cap, and
the shared parser slots). A batch keeps every child small whatever the length of the book, and
a page that would blow a limit fails its batch, which is reported, not the server.

The child reads the text through PDFium's text API (``FPDFText_*``, via pypdfium2, which ingest
already uses): one entry per character with its Unicode value, its *loose* box (the font's
ascent to descent, so highlights have even heights), its font size, whether PDFium generated it
(a space or line break it inferred), and whether it is a hyphen PDFium found at a line end.
Characters are grouped into words at spaces and line breaks. No JavaScript, forms, or
annotations are touched, and password-protected PDFs are refused, as at ingest.

Output (one JSON file per batch, written under ``RLIMIT_FSIZE``)::

    {"ok": true, "pages": N, "toc": [[level, title, page_index], …] (first batch only),
     "batch": [{"index", "w", "h", "x0", "y0", "rotation", "label",
                "chars", "map_errors", "words": [[text, x0, y0, x1, y1, size, flags], …]}]}

Coordinates are PDF user space (points, origin bottom left, before ``/Rotate``), which is what
pdf.js converts with ``viewport.convertToViewportRectangle``. ``flags``: 1 = the word ends
with a line-end hyphen PDFium flagged, 2 = a line break follows the word, 4 = most of its
first character is set in a bold font (weight 600 or more, or a bold font name).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lectern.ingest import pdf as pdf_ingest
from lectern.ingest.formats import FormatError, ProbeBusy

BATCH_PAGES = 48
# Characters read from one page; a page with more (a dense table, a broken generator) keeps
# its first ones and is marked truncated.
MAX_PAGE_CHARS = 30_000
# Words kept for the whole document (≈ 2,500 dense pages), so memory stays bounded.
MAX_WORDS = 1_500_000
TOC_MAX = 2_000
FLAG_HYPHEN = 1
FLAG_BREAK = 2
FLAG_BOLD = 4
_SPACES = frozenset(
    " \t\r\n\x0b\x0c\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)


@dataclass
class RawPage:
    index: int
    w: float
    h: float
    x0: float
    y0: float
    rotation: int
    label: str | None
    chars: int
    map_errors: int
    words: list[list]  # [text, x0, y0, x1, y1, size, flags]


@dataclass
class RawDoc:
    pages: list[RawPage]
    toc: list[tuple[int, str, int]]  # (level, title, page_index)
    truncated: bool = False


def extract_words(path: Path) -> RawDoc:
    """Every page's words, in batches of sandboxed children. Raises :class:`FormatError`
    (``not_pdf``, ``pdf_encrypted``, ``too_many_pages``, ``pdf_too_complex``) or
    :class:`ProbeBusy` when no parser slot frees up."""
    path = path.resolve()  # the child runs in its own scratch directory
    first = _run_batch(path, 0, BATCH_PAGES)
    total = int(first.get("pages") or 0)
    pages = [_page(p) for p in first.get("batch") or []]
    toc = [(int(lv), str(t), int(pi)) for lv, t, pi in (first.get("toc") or [])[:TOC_MAX]]
    words = sum(len(p.words) for p in pages)
    truncated = False
    start = BATCH_PAGES
    while start < total:
        if words >= MAX_WORDS:
            truncated = True
            break
        out = _run_batch(path, start, min(total, start + BATCH_PAGES))
        for p in out.get("batch") or []:
            page = _page(p)
            words += len(page.words)
            pages.append(page)
        start += BATCH_PAGES
    return RawDoc(pages, toc, truncated)


def _page(p: dict) -> RawPage:
    return RawPage(
        index=int(p["index"]),
        w=float(p["w"]),
        h=float(p["h"]),
        x0=float(p.get("x0") or 0.0),
        y0=float(p.get("y0") or 0.0),
        rotation=int(p.get("rotation") or 0),
        label=p.get("label"),
        chars=int(p.get("chars") or 0),
        map_errors=int(p.get("map_errors") or 0),
        words=p.get("words") or [],
    )


def _run_batch(path: Path, first: int, last: int) -> dict:
    if not pdf_ingest._SLOTS.acquire(timeout=pdf_ingest.BUSY_WAIT_S):
        raise ProbeBusy()
    try:
        with tempfile.TemporaryDirectory(prefix="lectern-pdftext-") as tmp:
            out_path = Path(tmp) / "words.json"
            try:
                res = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    [
                        sys.executable,
                        "-I",
                        "-m",
                        "lectern.pdftext.extract",
                        str(path),
                        str(out_path),
                        str(first),
                        str(last),
                    ],  # fmt: skip
                    capture_output=True,
                    timeout=pdf_ingest.TIMEOUT_S,
                    check=False,
                    env={"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"},
                    cwd=tmp,
                )
            except subprocess.TimeoutExpired:
                raise FormatError(
                    "pdf_too_complex", f"pages {first + 1}–{last}: timed out"
                ) from None
            try:
                status = json.loads(res.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
            except (ValueError, IndexError):
                raise FormatError(
                    "pdf_too_complex", f"pages {first + 1}–{last}: child exited {res.returncode}"
                ) from None
            if not status.get("ok"):
                raise FormatError(status.get("error") or "not_pdf", status.get("detail") or "")
            try:
                return json.loads(out_path.read_text("utf-8"))
            except (OSError, ValueError):
                raise FormatError("pdf_too_complex", "output limit") from None
    finally:
        pdf_ingest._SLOTS.release()


# --- the child --------------------------------------------------------------------------------


def _r(v: float) -> float:
    return round(float(v), 2)


def _words_of(textpage, raw) -> tuple[list[list], int, int]:
    import ctypes

    tp = textpage.raw
    n = min(raw.FPDFText_CountChars(tp), MAX_PAGE_CHARS)
    rect = raw.FS_RECTF()
    name_buf = ctypes.create_string_buffer(96)
    words: list[list] = []
    cur: list[str] = []
    box = [0.0, 0.0, 0.0, 0.0]
    sizes: list[float] = []
    bold = 0
    map_errors = 0
    chars = 0

    def flush(flags: int) -> None:
        nonlocal cur, sizes, bold
        if cur:
            size = sorted(sizes)[len(sizes) // 2] if sizes else 0.0
            if bold:
                flags |= FLAG_BOLD
            words.append(["".join(cur), _r(box[0]), _r(box[1]), _r(box[2]), _r(box[3]), _r(size),
                          flags])  # fmt: skip
        elif flags and words:
            words[-1][6] |= flags
        cur, sizes, bold = [], [], 0

    for i in range(n):
        u = raw.FPDFText_GetUnicode(tp, i)
        generated = raw.FPDFText_IsGenerated(tp, i) == 1
        ch = chr(u) if 0 < u < 0x110000 and not 0xD800 <= u < 0xE000 else "\ufffd"
        if u == 0x02 or (raw.FPDFText_IsHyphen(tp, i) == 1 and not generated):
            # PDFium's marker for a hyphen it found at the end of a line
            if cur:
                cur.append("-")
            flush(FLAG_HYPHEN | FLAG_BREAK)
            continue
        if ch in "\r\n":
            flush(FLAG_BREAK)
            continue
        if ch in _SPACES or generated or u < 0x20:
            flush(0)
            continue
        chars += 1
        if raw.FPDFText_HasUnicodeMapError(tp, i) == 1:
            map_errors += 1
        if not raw.FPDFText_GetLooseCharBox(tp, i, ctypes.byref(rect)):
            continue
        x0, x1 = min(rect.left, rect.right), max(rect.left, rect.right)
        y0, y1 = min(rect.bottom, rect.top), max(rect.bottom, rect.top)
        if not cur:
            box = [x0, y0, x1, y1]
        else:
            box = [min(box[0], x0), min(box[1], y0), max(box[2], x1), max(box[3], y1)]
        cur.append(ch)
        sizes.append(raw.FPDFText_GetFontSize(tp, i))
        if len(cur) == 1:
            bold = _is_bold(raw, tp, i, name_buf)  # per word: font lookups are not free
    flush(0)
    return words, chars, map_errors


_BOLD_NAME = re.compile(rb"bold|black|heavy|semibold|demi", re.I)
_FORCE_BOLD = 1 << 18


def _is_bold(raw, tp, i: int, buf) -> int:
    import ctypes

    weight = raw.FPDFText_GetFontWeight(tp, i)
    if weight >= 600:
        return 1
    if weight > 0:
        return 0
    flags = ctypes.c_int(0)  # no weight (standard 14 fonts): the name or ForceBold decides
    raw.FPDFText_GetFontInfo(tp, i, buf, len(buf), ctypes.byref(flags))
    return int(bool(flags.value & _FORCE_BOLD or _BOLD_NAME.search(buf.value or b"")))


def _toc(doc) -> list[list]:
    out: list[list] = []
    try:
        for bm in doc.get_toc(max_depth=4):
            if len(out) >= TOC_MAX:
                break
            dest = bm.get_dest()
            index = dest.get_index() if dest is not None else None
            if index is None:
                continue
            title = " ".join((bm.get_title() or "").split())[:200]
            if title:
                out.append([int(bm.level), title, int(index)])
    except Exception:  # noqa: BLE001 - a damaged outline is no outline
        return []
    return out


def _child(path: Path, out_path: Path, first: int, last: int) -> dict:
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    try:
        doc = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        code = "pdf_encrypted" if "password" in str(exc).lower() else "not_pdf"
        return {"ok": False, "error": code, "detail": str(exc)[:200]}
    try:
        total = len(doc)
        if total < 1:
            return {"ok": False, "error": "not_pdf", "detail": "no pages"}
        if total > pdf_ingest.MAX_PAGES:
            return {"ok": False, "error": "too_many_pages", "detail": f"{total} pages"}
        batch = []
        for index in range(max(0, first), min(total, last)):
            page = doc[index]
            try:
                left, bottom, right, top = page.get_cropbox()
                w, h = right - left, top - bottom
                entry = {
                    "index": index,
                    "w": _r(w),
                    "h": _r(h),
                    "x0": _r(left),
                    "y0": _r(bottom),
                    "rotation": int(page.get_rotation() or 0),
                    "label": None,
                    "chars": 0,
                    "map_errors": 0,
                    "words": [],
                }
                with contextlib.suppress(Exception):  # labels are optional
                    label = doc.get_page_label(index)
                    entry["label"] = label[:32] if label else None
                if 0 < w <= pdf_ingest.MAX_PAGE_SIDE_PT and 0 < h <= pdf_ingest.MAX_PAGE_SIDE_PT:
                    textpage = page.get_textpage()
                    try:
                        entry["words"], entry["chars"], entry["map_errors"] = _words_of(
                            textpage, raw
                        )
                    finally:
                        textpage.close()
                batch.append(entry)
            finally:
                page.close()
        result = {"ok": True, "pages": total, "batch": batch}
        if first == 0:
            result["toc"] = _toc(doc)
    finally:
        doc.close()
    out_path.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), "utf-8")
    return {"ok": True}


def main(argv: list[str]) -> int:
    pdf_ingest._limit()
    try:
        status = _child(Path(argv[0]), Path(argv[1]), int(argv[2]), int(argv[3]))
    except MemoryError:
        status = {"ok": False, "error": "pdf_too_complex", "detail": "memory limit"}
    except OSError:  # RLIMIT_FSIZE: the output limit
        status = {"ok": False, "error": "pdf_too_complex", "detail": "output limit"}
    except Exception as exc:  # noqa: BLE001 - anything else is an unreadable PDF
        status = {"ok": False, "error": "not_pdf", "detail": type(exc).__name__}
    sys.stdout.write(json.dumps(status) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
