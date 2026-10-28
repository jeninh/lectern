"""Everything that opens an untrusted PDF for OCR, in resource-limited child processes.

Like PDF ingest (:mod:`lectern.ingest.pdf`) and the text-layer reader, the server never opens
a PDF in its own process. ``python -I -m lectern.ocr.child <mode> …`` sets hard limits on
itself (address space, CPU seconds, output size, no core dumps, ``nice``) before importing a
parser, and the parent kills it after a wall-clock timeout. Tesseract runs as the child's own
child, so it inherits the same limits. Three modes:

``scan``
    pages ``[first, last)``: each page's crop box, ``/Rotate``, label, and how many characters
    its text layer has (a scan has none), plus the outline on the first batch. Run under the
    ingest limits and parser slots.
``page``
    one page: rendered to a grayscale image at the job's resolution (at most
    :data:`MAX_PIXELS`), recognised by Tesseract, and its words mapped back from pixels to PDF
    user space with PDFium's own device-to-page transform (crop box offsets and ``/Rotate``
    included). One page per child keeps memory flat whatever the book's length, which is
    what a Raspberry Pi needs.
``assemble``
    the searchable copy: the original PDF with an invisible text layer added to every page
    (:mod:`lectern.ocr.textlayer`), written with pypdf. The original is only read.

Errors come back as ``{"ok": false, "error": code, "detail": …}`` on the last stdout line.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lectern.ingest import pdf as pdf_ingest
from lectern.ingest.formats import PARSER_SLOTS, FormatError, ProbeBusy

SCAN_BATCH = 500
# A page "has text" with this many characters in its text layer (as the text-layer reader
# counts body text); a PDF is a scan when fewer than half its pages do.
TEXT_PAGE_CHARS = 40
NEEDS_OCR_SHARE = 0.5
TOC_MAX = 2_000
MAX_PIXELS = 40_000_000
PAGE_TIMEOUT_S = 900
PAGE_CPU_S = 600
PAGE_OUTPUT_BYTES = 256 * 1024 * 1024
ASSEMBLE_CPU_S = 1800
ASSEMBLE_TIMEOUT_S = 2400


class OcrError(Exception):
    """A page or the assembly failed; ``code`` is a stable reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class Scan:
    pages: list[dict]  # {"index", "w", "h", "x0", "y0", "rotation", "label", "chars"}
    toc: list[list]  # [level, title, page_index]

    @property
    def pages_with_text(self) -> int:
        return sum(1 for p in self.pages if p["chars"] >= TEXT_PAGE_CHARS)

    @property
    def needs_ocr(self) -> bool:
        return bool(self.pages) and self.pages_with_text / len(self.pages) < NEEDS_OCR_SHARE


# --- the parent side ----------------------------------------------------------------------------


def _env(extra: dict | None = None) -> dict:
    from lectern import limits

    env = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8",
           limits.CHILD_NICE: str(limits.GOVERNOR.limit(limits.OCR).nice)}  # fmt: skip
    if prefix := os.environ.get("TESSDATA_PREFIX"):
        env["TESSDATA_PREFIX"] = prefix
    env.update(extra or {})
    return env


def _run(args: list[str], tmp: str, timeout: float, env: dict | None = None) -> dict:
    # Its own process group: on a timeout the child *and* the Tesseract it started are
    # killed, so a retry never runs next to an orphan (twice the memory on a Pi).
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-I", "-m", "lectern.ocr.child", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(env),
        cwd=tmp,
        start_new_session=True,
    )
    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise OcrError("timed_out", f"no answer in {int(timeout)} s") from None
    except BaseException:
        _kill_group(proc)
        raise
    # A normal exit: the child waited for its Tesseract, and its PID is reaped now, so its
    # group id may already belong to someone else; nothing to kill.
    try:
        status = json.loads(stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise OcrError("crashed", f"child exited {proc.returncode}") from None
    if not status.get("ok"):
        raise OcrError(status.get("error") or "crashed", status.get("detail") or "")
    return status


def _kill_group(proc: subprocess.Popen) -> None:
    import signal

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.communicate(timeout=5)


def scan(path: Path) -> Scan:
    """Every page's geometry and text count, in batches under the ingest limits. Raises
    :class:`FormatError` (``not_pdf``, ``pdf_encrypted``, ``too_many_pages``,
    ``pdf_too_complex``) or :class:`ProbeBusy`."""
    path = path.resolve()
    pages: list[dict] = []
    toc: list[list] = []
    first, total = 0, None
    while total is None or first < total:
        if not PARSER_SLOTS.acquire(timeout=pdf_ingest.BUSY_WAIT_S):
            raise ProbeBusy()
        try:
            with tempfile.TemporaryDirectory(prefix="lectern-ocr-scan-") as tmp:
                out = Path(tmp) / "scan.json"
                try:
                    _run(["scan", str(path), str(out), str(first), str(first + SCAN_BATCH)], tmp,
                         pdf_ingest.TIMEOUT_S)  # fmt: skip
                    data = json.loads(out.read_text("utf-8"))
                except OcrError as exc:
                    if exc.code in ("not_pdf", "pdf_encrypted", "too_many_pages"):
                        raise FormatError(exc.code, exc.detail) from None
                    raise FormatError("pdf_too_complex", exc.detail or exc.code) from None
                except (OSError, ValueError):
                    raise FormatError("pdf_too_complex", "output limit") from None
        finally:
            PARSER_SLOTS.release()
        total = int(data["pages"])
        pages.extend(data["batch"])
        if first == 0:
            toc = data.get("toc") or []
        first += SCAN_BATCH
    return Scan(pages, toc)


def ocr_page(
    path: Path,
    index: int,
    *,
    tesseract: str,
    language: str,
    dpi: int,
    memory_mb: int,
    threads: int = 1,
) -> dict:
    """Recognise one page; returns ``{"index", "words", "conf", "dpi"}`` (words in PDF user
    space). Raises :class:`OcrError`."""
    path = path.resolve()
    with tempfile.TemporaryDirectory(prefix="lectern-ocr-page-") as tmp:
        out = Path(tmp) / "page.json"
        _run(["page", str(path), str(index), str(out), tesseract, language, str(dpi),
              str(memory_mb)], tmp, PAGE_TIMEOUT_S,
             {"OMP_THREAD_LIMIT": str(max(1, threads))})  # fmt: skip
        try:
            return json.loads(out.read_text("utf-8"))
        except (OSError, ValueError):
            raise OcrError("crashed", "no page output") from None


def assemble(original: Path, pages: list[dict], dest: Path, *, memory_mb: int) -> None:
    """Write the searchable copy of ``original`` to ``dest`` with every page's words."""
    with tempfile.TemporaryDirectory(prefix="lectern-ocr-pdf-") as tmp:
        words = Path(tmp) / "words.json"
        words.write_text(
            json.dumps({str(p["index"]): p["words"] for p in pages}, ensure_ascii=False), "utf-8"
        )
        limit = original.stat().st_size * 2 + words.stat().st_size * 4 + 64 * 1024 * 1024
        _run(["assemble", str(original.resolve()), str(words), str(dest.resolve()),
              str(limit), str(memory_mb)], tmp, ASSEMBLE_TIMEOUT_S)  # fmt: skip


# --- the child --------------------------------------------------------------------------------


def _limit(memory_mb: int, cpu_s: int, output_bytes: int) -> None:
    import resource

    for which, value in (
        (resource.RLIMIT_AS, memory_mb * 1024 * 1024),
        (resource.RLIMIT_CPU, cpu_s),
        (resource.RLIMIT_FSIZE, output_bytes),
        (resource.RLIMIT_CORE, 0),
    ):
        try:
            resource.setrlimit(which, (value, value))
        except (ValueError, OSError):
            pass  # best effort where the platform refuses a limit
    from lectern.limits import lower_priority

    lower_priority(10)  # LECTERN_OCR_NICE, passed down by the parent


def _open(path: Path):
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        code = "pdf_encrypted" if "password" in str(exc).lower() else "not_pdf"
        raise OcrError(code, str(exc)[:200]) from None
    if len(doc) < 1:
        doc.close()
        raise OcrError("not_pdf", "no pages")
    if len(doc) > pdf_ingest.MAX_PAGES:
        n = len(doc)
        doc.close()
        raise OcrError("too_many_pages", f"{n} pages")
    return doc


def _r(v: float) -> float:
    return round(float(v), 2)


def _geometry(doc, page, index: int) -> dict:
    left, bottom, right, top = page.get_cropbox()
    entry = {
        "index": index,
        "w": _r(right - left),
        "h": _r(top - bottom),
        "x0": _r(left),
        "y0": _r(bottom),
        "rotation": int(page.get_rotation() or 0),
        "label": None,
    }
    with contextlib.suppress(Exception):  # labels are optional
        label = doc.get_page_label(index)
        entry["label"] = label[:32] if label else None
    return entry


def _toc(doc) -> list[list]:
    out: list[list] = []
    try:
        for bm in doc.get_toc(max_depth=4):
            if len(out) >= TOC_MAX:
                break
            dest = bm.get_dest()
            index = dest.get_index() if dest is not None else None
            title = " ".join((bm.get_title() or "").split())[:200]
            if index is not None and title:
                out.append([int(bm.level), title, int(index)])
    except Exception:  # noqa: BLE001 - a damaged outline is no outline
        return []
    return out


def _scan_child(path: Path, out: Path, first: int, last: int) -> dict:
    import pypdfium2.raw as raw

    doc = _open(path)
    try:
        batch = []
        for index in range(max(0, first), min(len(doc), last)):
            page = doc[index]
            try:
                entry = _geometry(doc, page, index)
                chars = 0
                if 0 < entry["w"] <= pdf_ingest.MAX_PAGE_SIDE_PT and 0 < entry["h"] <= (
                    pdf_ingest.MAX_PAGE_SIDE_PT
                ):
                    textpage = page.get_textpage()
                    try:
                        tp = textpage.raw
                        for i in range(min(raw.FPDFText_CountChars(tp), 4 * TEXT_PAGE_CHARS)):
                            u = raw.FPDFText_GetUnicode(tp, i)
                            if u > 0x20 and raw.FPDFText_IsGenerated(tp, i) != 1:
                                chars += 1
                    finally:
                        textpage.close()
                entry["chars"] = chars
                batch.append(entry)
            finally:
                page.close()
        result = {"pages": len(doc), "batch": batch}
        if first == 0:
            result["toc"] = _toc(doc)
    finally:
        doc.close()
    out.write_text(json.dumps(result, ensure_ascii=False), "utf-8")
    return {"ok": True}


def _page_child(path: Path, index: int, out: Path, tesseract: str, language: str,
                dpi: int) -> dict:  # fmt: skip
    import ctypes

    import pypdfium2.raw as raw

    from lectern.ocr import engine
    from lectern.ocr.words import from_tsv

    engine.parse_spec(language)  # an argument to Tesseract: checked again here
    doc = _open(path)
    try:
        if not 0 <= index < len(doc):
            raise OcrError("no_such_page", str(index))
        page = doc[index]
        try:
            w, h = page.get_size()  # as displayed: /Rotate applied
            if not (0 < w <= pdf_ingest.MAX_PAGE_SIDE_PT and 0 < h <= pdf_ingest.MAX_PAGE_SIDE_PT):
                result = {"index": index, "words": [], "conf": None, "dpi": 0}
                out.write_text(json.dumps(result), "utf-8")
                return {"ok": True}
            scale = min(dpi / 72.0, (MAX_PIXELS / (w * h)) ** 0.5)
            bitmap = page.render(scale=scale, may_draw_forms=False, grayscale=True)
            width, height = bitmap.width, bitmap.height
            image = Path("page.png").resolve()
            bitmap.to_pil().save(image, "PNG")
            bitmap.close()
            effective = round(scale * 72)
            try:
                res = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    [
                        tesseract,
                        str(image),
                        "page",
                        "-l",
                        language,
                        "--dpi",
                        str(effective),
                        "--psm",
                        "3",
                        "tsv",
                    ],  # fmt: skip
                    capture_output=True,
                    timeout=PAGE_TIMEOUT_S - 60,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise OcrError("timed_out", f"page {index + 1}") from None
            except FileNotFoundError:
                raise OcrError("engine_missing", "tesseract is not installed") from None
            image.unlink(missing_ok=True)
            tsv_path = Path("page.tsv")
            if res.returncode != 0 or not tsv_path.is_file():
                tail = res.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
                detail = " ".join(tail)[:300]
                if "Failed loading language" in detail or "Error opening data file" in detail:
                    raise OcrError("language_missing", language)
                raise OcrError("engine_failed", detail or f"exit {res.returncode}")
            tsv = tsv_path.read_text("utf-8", "replace")
            px, py = ctypes.c_double(), ctypes.c_double()

            def to_page(dx: float, dy: float) -> tuple[float, float]:
                # PDFium's own transform: crop box offset and /Rotate, as it rendered
                raw.FPDF_DeviceToPage(page.raw, 0, 0, width, height, 0, int(round(dx)),
                                      int(round(dy)), ctypes.byref(px), ctypes.byref(py))  # fmt: skip
                return px.value, py.value

            words = from_tsv(tsv, to_page, 1 / scale)
            confs = []
            for row in tsv.splitlines()[1:]:
                cols = row.split("\t")
                if len(cols) >= 12 and cols[0] == "5" and cols[11].strip():
                    with contextlib.suppress(ValueError):
                        confs.append(max(0.0, float(cols[10])))
            conf = round(sum(confs) / len(confs), 1) if confs else None
        finally:
            page.close()
    finally:
        doc.close()
    result = {"index": index, "words": words, "conf": conf, "dpi": effective}
    out.write_text(json.dumps(result, ensure_ascii=False), "utf-8")
    return {"ok": True}


def _assemble_child(original: Path, words_path: Path, dest: Path) -> dict:
    from pypdf import PdfWriter

    from lectern.ocr import textlayer

    by_page = json.loads(words_path.read_text("utf-8"))
    try:
        writer = PdfWriter(clone_from=str(original))
    except Exception as exc:  # noqa: BLE001 - pypdf raises many kinds for a broken file
        raise OcrError("not_pdf", type(exc).__name__) from None
    font = textlayer.add_font(writer)
    for index, page in enumerate(writer.pages):
        words = by_page.get(str(index))
        if words:
            rotation = int(page.get_inherited("/Rotate", 0) or 0)
            textlayer.overlay(writer, page, font, words, rotation)
    tmp = dest.with_name(f".{dest.name}.part")
    with tmp.open("wb") as fh:
        writer.write(fh)
    os.replace(tmp, dest)
    return {"ok": True}


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else ""
    try:
        if mode == "scan":
            pdf_ingest._limit()
            status = _scan_child(Path(argv[1]), Path(argv[2]), int(argv[3]), int(argv[4]))
        elif mode == "page":
            _limit(int(argv[7]), PAGE_CPU_S, PAGE_OUTPUT_BYTES)
            status = _page_child(Path(argv[1]), int(argv[2]), Path(argv[3]), argv[4], argv[5],
                                 int(argv[6]))  # fmt: skip
        elif mode == "assemble":
            _limit(int(argv[5]), ASSEMBLE_CPU_S, int(argv[4]))
            status = _assemble_child(Path(argv[1]), Path(argv[2]), Path(argv[3]))
        else:
            status = {"ok": False, "error": "usage", "detail": mode[:20]}
    except OcrError as exc:
        status = {"ok": False, "error": exc.code, "detail": exc.detail}
    except MemoryError:
        status = {"ok": False, "error": "out_of_memory", "detail": "memory limit"}
    except OSError as exc:  # RLIMIT_FSIZE, a full disk
        status = {"ok": False, "error": "output_limit", "detail": type(exc).__name__}
    except Exception as exc:  # noqa: BLE001 - anything else is an unreadable PDF
        status = {"ok": False, "error": "not_pdf", "detail": type(exc).__name__}
    sys.stdout.write(json.dumps(status) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
