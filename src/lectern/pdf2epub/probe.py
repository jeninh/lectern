"""What the PDF's pages look like beyond their words, read in the sandboxed PDF child.

The canonical text (:mod:`lectern.pdftext`) keeps only words and boxes; the EPUB conversion
also needs the type and the pictures: which pieces of a word are italic, bold, small caps, or
raised (a footnote marker), where the images and vector drawings are, and the page rendered
to pixels for figures, tables, maths, and pages kept as images. PDFs are untrusted, so all of
it runs in ``python -I -m lectern.pdf2epub.probe`` children with exactly the limits PDF ingest
uses (:mod:`lectern.ingest.pdf`), in batches of :data:`BATCH_PAGES` pages, under the shared
parser slots.

``scan`` writes, per page::

    {"index", "w", "h", "x0", "y0", "rotation", "chars", "map_errors",
     "runs":   [[text, x0, y0, x1, y1, size, style, baseline], …],
     "images": [[x0, y0, x1, y1], …],
     "paths":  [[x0, y0, x1, y1], …]}

A run is a stretch of one word set in one style, size, and baseline (so ``word¹`` is two
runs). ``style`` bits: :data:`ITALIC`, :data:`BOLD`, :data:`SMALLCAPS`, :data:`MATHFONT`.
Coordinates are PDF user space, as in the text document.

``render`` takes a list of ``[page, x0, y0, x1, y1, name, kind]`` regions (a whole page when
the box is empty) and writes each as ``name`` into the output directory: JPEG for photos and
whole pages, PNG for drawings, tables, and maths.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from lectern import limits
from lectern.ingest import pdf as pdf_ingest
from lectern.ingest.formats import PARSER_SLOTS, FormatError, ProbeBusy
from lectern.pdftext.extract import _SPACES

BATCH_PAGES = 48
MAX_PAGE_CHARS = 30_000  # as the text extractor
MAX_OBJECTS = 20_000  # page objects looked at per page
MAX_BOXES = 3_000  # image and path boxes kept per page
RENDER_BATCH = 40  # regions per render child
# Pixels: a region's long side, and the scale cap (≈ 200 dpi).
REGION_LONG_SIDE_PX = 1600
PAGE_LONG_SIDE_PX = 1400
MAX_SCALE = 200 / 72

ITALIC = 1
BOLD = 2
SMALLCAPS = 4
MATHFONT = 8
_BOLD_NAME = re.compile(rb"bold|black|heavy|semibold|demi", re.I)
_ITALIC_NAME = re.compile(rb"italic|oblique|slant|-it\b|\bit$", re.I)
_MATH_NAME = re.compile(rb"symbol|cmsy|cmmi|cmex|msam|msbm|math|stix|euler|mtextra", re.I)
_SC_NAME = re.compile(rb"smallcaps|\bsc\b|-sc$|caps", re.I)
_FLAG_ITALIC = 1 << 6
_FLAG_SMALLCAP = 1 << 17
_FLAG_FORCEBOLD = 1 << 18


def scan(path: Path) -> list[dict]:
    """Every page's runs and object boxes. Raises :class:`FormatError` or :class:`ProbeBusy`
    like :func:`lectern.pdftext.extract.extract_words`."""
    path = path.resolve()
    first = _run(["scan", str(path), "{out}", "0", str(BATCH_PAGES)])
    total = int(first.get("pages") or 0)
    pages = list(first.get("batch") or [])
    start = BATCH_PAGES
    while start < total:
        out = _run(["scan", str(path), "{out}", str(start), str(min(total, start + BATCH_PAGES))])
        pages.extend(out.get("batch") or [])
        start += BATCH_PAGES
    return pages


def render(path: Path, regions: list[list], out_dir: Path) -> list[str]:
    """Render ``regions`` into ``out_dir``; returns the names written (a region that fails
    is left out, and the caller falls back to its text)."""
    path = path.resolve()
    done: list[str] = []
    for i in range(0, len(regions), RENDER_BATCH):
        with tempfile.TemporaryDirectory(prefix="lectern-pdf2epub-jobs-") as tmp:
            jobs = Path(tmp) / "jobs.json"
            jobs.write_text(json.dumps(regions[i : i + RENDER_BATCH]))
            out = _run(["render", str(path), "{out}", str(jobs), str(out_dir.resolve())])
        done.extend(out.get("written") or [])
    return done


def _run(argv: list[str]) -> dict:
    if not PARSER_SLOTS.acquire(timeout=pdf_ingest.BUSY_WAIT_S):
        raise ProbeBusy()
    try:
        with tempfile.TemporaryDirectory(prefix="lectern-pdf2epub-") as tmp:
            out_path = Path(tmp) / "out.json"
            args = [a.replace("{out}", str(out_path)) for a in argv]
            try:
                res = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    [sys.executable, "-I", "-m", "lectern.pdf2epub.probe", *args],
                    capture_output=True,
                    timeout=pdf_ingest.TIMEOUT_S,
                    check=False,
                    env={
                        "PATH": os.environ.get("PATH", ""),
                        "LANG": "C.UTF-8",
                        **limits.GOVERNOR.limit(limits.CONVERT).child_env(),
                    },
                    cwd=tmp,
                )
            except subprocess.TimeoutExpired:
                raise FormatError("pdf_too_complex", f"{argv[0]}: timed out") from None
            try:
                status = json.loads(res.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
            except (ValueError, IndexError):
                raise FormatError(
                    "pdf_too_complex", f"{argv[0]}: child exited {res.returncode}"
                ) from None
            if not status.get("ok"):
                raise FormatError(status.get("error") or "not_pdf", status.get("detail") or "")
            try:
                return json.loads(out_path.read_text("utf-8"))
            except (OSError, ValueError):
                raise FormatError("pdf_too_complex", "output limit") from None
    finally:
        PARSER_SLOTS.release()


# --- the child --------------------------------------------------------------------------------


def _r(v: float) -> float:
    return round(float(v), 2)


def _font_style(raw, tp, i: int, cache: dict, buf) -> int:
    """The style bits of character ``i``'s font, looked up once per font."""
    obj = raw.FPDFText_GetTextObject(tp, i)
    font = raw.FPDFTextObj_GetFont(obj) if obj else None
    key = ctypes.cast(font, ctypes.c_void_p).value if font else None
    if key is not None and key in cache:
        return cache[key]
    flags = ctypes.c_int(0)
    raw.FPDFText_GetFontInfo(tp, i, buf, len(buf), ctypes.byref(flags))
    name = buf.value or b""
    style = 0
    angle = ctypes.c_int(0)
    if font and raw.FPDFFont_GetItalicAngle(font, ctypes.byref(angle)) and angle.value != 0:
        style |= ITALIC
    if flags.value & _FLAG_ITALIC or _ITALIC_NAME.search(name):
        style |= ITALIC
    weight = raw.FPDFText_GetFontWeight(tp, i)
    if weight >= 600 or (
        weight <= 0 and (flags.value & _FLAG_FORCEBOLD or _BOLD_NAME.search(name))
    ):
        style |= BOLD
    if flags.value & _FLAG_SMALLCAP or _SC_NAME.search(name):
        style |= SMALLCAPS
    if _MATH_NAME.search(name):
        style |= MATHFONT
    if key is not None:
        cache[key] = style
    return style


def _runs_of(textpage, raw) -> tuple[list[list], int, int]:
    tp = textpage.raw
    n = min(raw.FPDFText_CountChars(tp), MAX_PAGE_CHARS)
    rect = raw.FS_RECTF()
    ox, oy = ctypes.c_double(0), ctypes.c_double(0)
    buf = ctypes.create_string_buffer(96)
    cache: dict = {}
    runs: list[list] = []
    cur: list = []  # [chars, x0, y0, x1, y1, size, style, baseline]
    chars = map_errors = 0

    def flush() -> None:
        nonlocal cur
        if cur and cur[0]:
            runs.append(["".join(cur[0]), *(_r(v) for v in cur[1:6]), cur[6], _r(cur[7])])
        cur = []

    for i in range(n):
        u = raw.FPDFText_GetUnicode(tp, i)
        generated = raw.FPDFText_IsGenerated(tp, i) == 1
        ch = chr(u) if 0 < u < 0x110000 and not 0xD800 <= u < 0xE000 else "\ufffd"
        if u == 0x02 or ch in "\r\n" or ch in _SPACES or generated or u < 0x20:
            flush()
            continue
        chars += 1
        if raw.FPDFText_HasUnicodeMapError(tp, i) == 1:
            map_errors += 1
        if not raw.FPDFText_GetLooseCharBox(tp, i, ctypes.byref(rect)):
            continue
        x0, x1 = min(rect.left, rect.right), max(rect.left, rect.right)
        y0, y1 = min(rect.bottom, rect.top), max(rect.bottom, rect.top)
        size = raw.FPDFText_GetFontSize(tp, i)
        style = _font_style(raw, tp, i, cache, buf)
        base = (
            oy.value
            if raw.FPDFText_GetCharOrigin(tp, i, ctypes.byref(ox), ctypes.byref(oy))
            else y0
        )
        if cur and (
            cur[6] != style
            or abs(cur[5] - size) > 0.05 * max(size, cur[5], 1.0)
            or abs(cur[7] - base) > 0.15 * max(size, cur[5], 1.0)
        ):
            flush()
        if not cur:
            cur = [[], x0, y0, x1, y1, size, style, base]
        else:
            cur[1], cur[2] = min(cur[1], x0), min(cur[2], y0)
            cur[3], cur[4] = max(cur[3], x1), max(cur[4], y1)
        cur[0].append(ch)
    flush()
    return runs, chars, map_errors


def _transform(m, box) -> list[float]:
    a, b, c, d, e, f = m
    xs, ys = [], []
    for x, y in ((box[0], box[1]), (box[0], box[3]), (box[2], box[1]), (box[2], box[3])):
        xs.append(a * x + c * y + e)
        ys.append(b * x + d * y + f)
    return [min(xs), min(ys), max(xs), max(ys)]


def _compose(outer, inner):
    a1, b1, c1, d1, e1, f1 = inner
    a2, b2, c2, d2, e2, f2 = outer
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2, c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)  # fmt: skip


def _objects(page, raw) -> tuple[list[list], list[list]]:
    """Image and path boxes in page space (objects inside form XObjects included)."""
    images: list[list] = []
    paths: list[list] = []
    mats: list[tuple] = [(1, 0, 0, 1, 0, 0)]
    for k, obj in enumerate(page.get_objects(max_depth=6)):
        if k >= MAX_OBJECTS or len(images) + len(paths) >= MAX_BOXES:
            break
        level = getattr(obj, "level", 0)
        del mats[level + 1 :]
        outer = mats[level] if level < len(mats) else mats[-1]
        try:
            box = list(obj.get_bounds())
        except Exception:  # noqa: BLE001, S112 - an object without bounds is skipped
            continue
        if obj.type == raw.FPDF_PAGEOBJ_FORM:
            m = obj.get_matrix()
            mats.append(_compose(outer, (m.a, m.b, m.c, m.d, m.e, m.f)))
            continue
        if level:
            box = _transform(outer, box)
        box = [_r(v) for v in box]
        if obj.type == raw.FPDF_PAGEOBJ_IMAGE:
            images.append(box)
        elif obj.type in (raw.FPDF_PAGEOBJ_PATH, raw.FPDF_PAGEOBJ_SHADING):
            paths.append(box)
    return images, paths


def _scan_child(doc, out_path: Path, first: int, last: int) -> dict:
    import pypdfium2.raw as raw

    total = len(doc)
    batch = []
    for index in range(max(0, first), min(total, last)):
        page = doc[index]
        try:
            left, bottom, right, top = page.get_cropbox()
            w, h = right - left, top - bottom
            entry = {"index": index, "w": _r(w), "h": _r(h), "x0": _r(left), "y0": _r(bottom),
                     "rotation": int(page.get_rotation() or 0), "chars": 0, "map_errors": 0,
                     "runs": [], "images": [], "paths": []}  # fmt: skip
            if 0 < w <= pdf_ingest.MAX_PAGE_SIDE_PT and 0 < h <= pdf_ingest.MAX_PAGE_SIDE_PT:
                textpage = page.get_textpage()
                try:
                    entry["runs"], entry["chars"], entry["map_errors"] = _runs_of(textpage, raw)
                finally:
                    textpage.close()
                with contextlib.suppress(Exception):  # a broken object tree is no figures
                    entry["images"], entry["paths"] = _objects(page, raw)
            batch.append(entry)
        finally:
            page.close()
    return {"ok": True, "pages": total, "batch": batch}


def _pixel_box(box, page_box, rotation: int, scale: float, size: tuple[int, int]):
    """A user-space box as a PIL crop box on the rendered page (``/Rotate`` applied)."""
    px0, py0, pw, ph = page_box
    x0, y0, x1, y1 = (box[0] - px0, box[1] - py0, box[2] - px0, box[3] - py0)
    if rotation == 90:
        rect = (y0, x0, y1, x1)
    elif rotation == 180:
        rect = (pw - x1, y0, pw - x0, y1)
    elif rotation == 270:
        rect = (ph - y1, pw - x1, ph - y0, pw - x0)
    else:
        rect = (x0, ph - y1, x1, ph - y0)
    w, h = size
    left, top, right, bottom = (round(v * scale) for v in rect)
    return max(0, left), max(0, top), min(w, right), min(h, bottom)


def _render_child(doc, out_path: Path, jobs_path: Path, out_dir: Path) -> dict:
    regions = json.loads(jobs_path.read_text())
    by_page: dict[int, list] = {}
    for r in regions:
        by_page.setdefault(int(r[0]), []).append(r)
    written: list[str] = []
    for index, items in sorted(by_page.items()):
        if not 0 <= index < len(doc):
            continue
        page = doc[index]
        try:
            left, bottom, right, top = page.get_cropbox()
            w, h = right - left, top - bottom
            if not (0 < w <= pdf_ingest.MAX_PAGE_SIDE_PT and 0 < h <= pdf_ingest.MAX_PAGE_SIDE_PT):
                continue
            whole = any(not any(r[1:5]) for r in items)
            long_side = PAGE_LONG_SIDE_PX if whole else 2 * REGION_LONG_SIDE_PX
            scale = min(MAX_SCALE, long_side / max(w, h))
            image = page.render(scale=scale, may_draw_forms=False).to_pil().convert("RGB")
            rotation = int(page.get_rotation() or 0)
            for _i, x0, y0, x1, y1, name, kind in items:
                if not re.fullmatch(r"[a-z0-9-]{1,40}\.(png|jpg)", name):
                    continue
                if any((x0, y0, x1, y1)):
                    crop = _pixel_box((x0, y0, x1, y1), (left, bottom, w, h), rotation, scale,
                                      image.size)  # fmt: skip
                    if crop[2] - crop[0] < 4 or crop[3] - crop[1] < 4:
                        continue
                    piece = image.crop(crop)
                else:
                    piece = image
                if max(piece.size) > REGION_LONG_SIDE_PX:
                    piece.thumbnail((REGION_LONG_SIDE_PX, REGION_LONG_SIDE_PX))
                dest = out_dir / name
                if name.endswith(".jpg"):
                    piece.save(dest, "JPEG", quality=82, optimize=True)
                else:
                    piece.convert("L" if kind in ("table", "math") else "RGB").save(
                        dest, "PNG", optimize=True
                    )
                written.append(name)
        finally:
            page.close()
    return {"ok": True, "written": written}


def _child(argv: list[str]) -> dict:
    import pypdfium2 as pdfium

    cmd, path, out_path = argv[0], Path(argv[1]), Path(argv[2])
    try:
        doc = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        code = "pdf_encrypted" if "password" in str(exc).lower() else "not_pdf"
        return {"ok": False, "error": code, "detail": str(exc)[:200]}
    try:
        if len(doc) > pdf_ingest.MAX_PAGES:
            return {"ok": False, "error": "too_many_pages", "detail": f"{len(doc)} pages"}
        if cmd == "scan":
            result = _scan_child(doc, out_path, int(argv[3]), int(argv[4]))
        elif cmd == "render":
            result = _render_child(doc, out_path, Path(argv[3]), Path(argv[4]))
        else:
            return {"ok": False, "error": "not_pdf", "detail": "unknown command"}
    finally:
        doc.close()
    out_path.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), "utf-8")
    return {"ok": True}


def main(argv: list[str]) -> int:
    pdf_ingest._limit()
    try:
        status = _child(argv)
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
