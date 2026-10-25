"""OCR as a durable job (``kind = "ocr"``, capability ``ocr``), docs/ocr.md.

Finding scans
    The library sweep (:func:`sweep`, in Housekeeping) reads every PDF it has not seen yet in
    the sandboxed scan child: page geometry, the outline, and how much text each page's text
    layer has. The result is cached by the file's SHA-256 under ``<data>/ocr/scan/``. A PDF
    where fewer than half the pages have text is a scan; a text PDF is never recognised again.

Queueing
    A scan gets one ``ocr`` job automatically (``LECTERN_OCR_AUTO``) when something can run
    it: Tesseract on this server, or a worker that advertises ``ocr``. Without either it
    says so (``unavailable``) and nothing is queued. Owners and members can start, retry, or
    redo it with another language (``POST /api/files/{id}/ocr``); the default language is
    the work's. One active job per work, like every kind.

Running
    The server's runner (:class:`OcrRunner`) or a worker recognises the pages one at a time,
    each in its own sandboxed child, and stores them in batches of :data:`BATCH_PAGES` as the
    job's artifacts (``pages-<first page>.json``), so a retried or resumed attempt starts at
    the first batch that is missing. The server then validates every page (its own page
    geometry, never a worker's; words clipped to the page), writes the words document and
    the searchable PDF (assembled in a sandboxed child, from the original and the words, so a
    worker never supplies a PDF), and publishes both under ``<work>/derived/`` in the same
    step that marks the job succeeded, holding the lease.

A page that fails (time or memory limit) is tried once more at half the resolution, then kept
empty and counted; anything else (no engine, a missing language, an unreadable PDF) fails the
job, which is retried with backoff and then marked failed with the reason. An expired lease is
reclaimed by the usual sweep; a lease the server's own runner held when the server stopped is
handed back at the next start. Nothing stays "running".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from lectern import queue
from lectern.config import Settings
from lectern.ingest import formats
from lectern.ingest.formats import FormatError, ProbeBusy
from lectern.models import (
    JOB_ACTIVE_STATUSES,
    BookFile,
    Job,
    JobStatus,
    Work,
    WorkerCredential,
    utcnow,
)
from lectern.ocr import child, engine
from lectern.ocr.words import validated_page

log = logging.getLogger("lectern.ocr")

KIND = "ocr"
PRIORITY = 30  # after alignment (0), transcodes (10), and narration (20)
MAX_ATTEMPTS = 3
LEASE_S = 1200  # longer than one page may take (child.PAGE_TIMEOUT_S)
BATCH_PAGES = 10
DOCUMENT_VERSION = 1
SCAN_VERSION = 1
# PDFs scanned per sweep at most (each is a few sandboxed children); the rest wait a minute.
SCANS_PER_SWEEP = 20
RESULT_MAX_BYTES = 1024 * 1024
ARTIFACT_RE = r"^pages-\d{5}\.json$"
DERIVED = "derived"


def prefix(sha256: str) -> str:
    return sha256[:16]


def pdf_path(library: Path, work: Work, sha256: str) -> Path:
    """The searchable copy: ``<work>/derived/ocr-<sha256 prefix>.pdf``."""
    return library / work.dir_path / DERIVED / f"ocr-{prefix(sha256)}.pdf"


def words_path(library: Path, work: Work, sha256: str) -> Path:
    """The recognised words: ``<work>/derived/ocr-<sha256 prefix>.json``."""
    return library / work.dir_path / DERIVED / f"ocr-{prefix(sha256)}.json"


def has_output(library: Path, work: Work, sha256: str) -> bool:
    return pdf_path(library, work, sha256).is_file() and words_path(library, work, sha256).is_file()


def batch_name(first: int) -> str:
    return f"pages-{first:05d}.json"


def batches(pages: int, size: int = BATCH_PAGES) -> list[tuple[int, int]]:
    return [(first, min(pages, first + size)) for first in range(0, pages, size)]


# --- the scan cache -----------------------------------------------------------------------------


def _scan_path(data_dir: Path, sha256: str) -> Path:
    return data_dir / "ocr" / "scan" / f"{sha256}.json"


def load_scan(data_dir: Path, sha256: str) -> dict | None:
    """``{"version", "pages": [...], "toc", "pages_with_text", "needs_ocr"}`` or, for a PDF
    the scan child refused, ``{"version", "error"}``; ``None`` = not scanned yet."""
    try:
        data = json.loads(_scan_path(data_dir, sha256).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if data.get("version") == SCAN_VERSION else None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def scan_file(settings: Settings, book: BookFile, work: Work) -> dict | None:
    """Scan one PDF now and cache the result (``None`` when the parser slots are busy)."""
    path = settings.library_dir / work.dir_path / book.rel_path
    try:
        found = child.scan(path)
        data = {
            "version": SCAN_VERSION,
            "pages": found.pages,
            "toc": found.toc,
            "pages_with_text": found.pages_with_text,
            "needs_ocr": found.needs_ocr,
        }
    except ProbeBusy:
        return None
    except FormatError as exc:
        data = {"version": SCAN_VERSION, "error": exc.code}
    _write_json(_scan_path(settings.data_dir, book.sha256), data)
    return data


# --- engines -----------------------------------------------------------------------------------


def local_engine(settings: Settings) -> tuple[str, tuple[str, ...]] | None:
    """``(version, languages)`` of Tesseract here, when the server's runner may use it."""
    if not settings.ocr_local_runner:
        return None
    version, langs = engine.probe(settings.tesseract_path)
    return (version, langs) if version else None


def worker_can_ocr(db: Session) -> bool:
    """Has any worker credential (online or not) advertised ``ocr``? Its jobs then wait for
    it rather than being refused."""
    for caps, allowed in db.execute(
        select(WorkerCredential.advertised, WorkerCredential.capabilities).where(
            WorkerCredential.revoked_at.is_(None)
        )
    ):
        if queue.OCR in (caps or []) and (allowed is None or queue.OCR in allowed):
            return True
    return False


def can_run(db: Session, settings: Settings) -> bool:
    return local_engine(settings) is not None or worker_can_ocr(db)


NO_ENGINE = (
    "No text recognition here: install Tesseract on the server (the Lectern image has it), or "
    "connect a worker that has it"
)


# --- jobs ----------------------------------------------------------------------------------------


def jobs_for(db: Session, book: BookFile) -> list[Job]:
    """Every OCR job of the work for this exact file (newest first)."""
    rows = db.scalars(
        select(Job).where(Job.work_id == book.work_id, Job.kind == KIND).order_by(Job.id.desc())
    ).all()
    return [
        j
        for j in rows
        if (j.payload or {}).get("file_id") == book.id
        and (j.payload or {}).get("pdf_sha256") == book.sha256
    ]


def active_job(db: Session, work_id: int) -> Job | None:
    return db.scalar(
        select(Job).where(
            Job.work_id == work_id, Job.kind == KIND, Job.status.in_(JOB_ACTIVE_STATUSES)
        )
    )


class Busy(Exception):
    """Another PDF of the same work is being recognised."""


def enqueue(
    db: Session, settings: Settings, book: BookFile, work: Work, scan: dict, *,
    language: str | None = None, user_id: int | None = None,
) -> Job:  # fmt: skip
    """Queue OCR for ``book`` (caller commits). The live job when one for this file already
    runs; :class:`Busy` when another file of the work is being recognised."""
    current = active_job(db, work.id)
    if current is not None:
        if (current.payload or {}).get("file_id") == book.id:
            return current
        raise Busy()
    lang = language or engine.language_for(work.language)
    engine.parse_spec(lang)
    job = Job(
        kind=KIND,
        work_id=work.id,
        status=JobStatus.QUEUED,
        priority=PRIORITY,
        max_attempts=MAX_ATTEMPTS,
        capability=queue.OCR,
        payload={
            "file_id": book.id,
            "pdf_sha256": book.sha256,
            "pages": len(scan["pages"]),
            "language": lang,
            "dpi": settings.ocr_dpi,
            "batch_pages": BATCH_PAGES,
        },
        created_by=user_id,
    )
    db.add(job)
    db.flush()
    return job


def cancel(db: Session, job: Job) -> bool:
    """Cancel a live job (caller commits)."""
    if job.status not in JOB_ACTIVE_STATUSES:
        return False
    job.status = JobStatus.CANCELLED
    job.lease_token_hash = None
    job.lease_expires_at = None
    job.heartbeat_at = None
    job.finished_at = utcnow()
    db.flush()
    return True


def delete_output(library: Path, work: Work, sha256: str) -> bool:
    found = False
    for p in (pdf_path(library, work, sha256), words_path(library, work, sha256)):
        for f in (p, p.with_name(p.name + ".sha256")):
            if f.is_file():
                f.unlink()
                found = True
    return found


# --- the library sweep ----------------------------------------------------------------------------


def sweep(session_factory: sessionmaker, settings: Settings) -> int:
    """Scan new PDFs, queue OCR for scans that never had it, and remove derived OCR files
    of PDFs that are no longer in their work. Returns the jobs queued."""
    from lectern.fsops import LIBRARY_LOCK

    with session_factory() as db:
        rows = db.execute(
            select(BookFile.id, BookFile.sha256)
            .join(Work, Work.id == BookFile.work_id)
            .where(BookFile.media_type == formats.PDF, Work.trashed_at.is_(None))
            .order_by(BookFile.id)
        ).all()
        work_ids = list(db.scalars(select(Work.id).where(Work.trashed_at.is_(None))))
    scanned = queued = 0
    for file_id, sha in rows:
        scan = load_scan(settings.data_dir, sha)
        if scan is None:
            if scanned >= SCANS_PER_SWEEP:
                continue
            with session_factory() as db:
                book = db.get(BookFile, file_id)
                work = db.get(Work, book.work_id) if book is not None else None
                if book is None or work is None:
                    continue
                scan = scan_file(settings, book, work)
            scanned += 1
        if not scan or not scan.get("needs_ocr") or not settings.ocr_auto:
            continue
        with LIBRARY_LOCK, session_factory() as db:
            book = db.get(BookFile, file_id)
            work = db.get(Work, book.work_id) if book is not None else None
            if book is None or work is None or work.trashed_at is not None:
                continue
            if has_output(settings.library_dir, work, sha) or jobs_for(db, book):
                continue  # done, or it ran before: a deleted or failed one is the user's call
            if not can_run(db, settings):
                continue
            try:
                enqueue(db, settings, book, work, scan)
                db.commit()
                queued += 1
            except Busy:
                db.rollback()
    for work_id in work_ids:
        with LIBRARY_LOCK, session_factory() as db:
            _drop_orphans(db, settings.library_dir, work_id)
    if queued:
        log.info("queued text recognition for %d scanned PDF(s)", queued)
    return queued


def _drop_orphans(db: Session, library: Path, work_id: int) -> None:
    work = db.get(Work, work_id)
    if work is None or work.trashed_at is not None:
        return
    derived = library / work.dir_path / DERIVED
    found = list(derived.glob("ocr-*")) if derived.is_dir() else []
    if not found:
        return
    keep = {
        prefix(sha)
        for sha in db.scalars(
            select(BookFile.sha256).where(
                BookFile.work_id == work_id, BookFile.media_type == formats.PDF
            )
        )
    }
    for f in found:
        tag = f.name[len("ocr-") :].split(".", 1)[0]
        if tag not in keep and f.is_file():
            f.unlink(missing_ok=True)


# --- publishing -----------------------------------------------------------------------------------


@dataclass
class Source:
    """What a job reads: its file and work as they are now, with the scan."""

    book: BookFile
    work: Work
    path: Path
    scan: dict


class Stale(Exception):
    """The job's PDF is gone or changed since it was queued."""


def source_of(db: Session, settings: Settings, job: Job) -> Source:
    payload = job.payload or {}
    book = db.get(BookFile, payload.get("file_id"))
    work = db.get(Work, job.work_id) if job.work_id is not None else None
    if (
        book is None
        or work is None
        or work.trashed_at is not None
        or book.work_id != work.id
        or book.sha256 != payload.get("pdf_sha256")
    ):
        raise Stale()
    library = settings.library_dir.resolve()
    path = (settings.library_dir / work.dir_path / book.rel_path).resolve()
    if library not in path.parents or not path.is_file():
        raise Stale()
    scan = load_scan(settings.data_dir, book.sha256) or scan_file(settings, book, work)
    if not scan or "pages" not in scan:
        raise Stale()
    return Source(book, work, path, scan)


def read_batches(src: Source, files: dict[int, Path]) -> list[dict]:
    """Every page, validated against the server's own page geometry. ``files`` maps each
    batch's first page to its JSON; every page must be in exactly one batch."""
    geometry = src.scan["pages"]
    pages: dict[int, dict] = {}
    for first, path in sorted(files.items()):
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"batch {batch_name(first)} is not JSON") from exc
        raw_pages = data.get("pages") if isinstance(data, dict) else None
        if not isinstance(raw_pages, list):
            raise ValueError(f"batch {batch_name(first)} has no pages")
        for raw in raw_pages:
            index = raw.get("index") if isinstance(raw, dict) else None
            if not isinstance(index, int) or not 0 <= index < len(geometry):
                raise ValueError(f"batch {batch_name(first)} names a page the PDF does not have")
            if index in pages:
                raise ValueError(f"page {index + 1} is in two batches")
            page = validated_page(raw, geometry[index])
            if raw.get("failed"):
                page["failed"] = True
            pages[index] = page
    missing = [i + 1 for i in range(len(geometry)) if i not in pages]
    if missing:
        raise ValueError(f"pages missing: {missing[:10]}")
    return [pages[i] for i in range(len(geometry))]


def build_document(src: Source, pages: list[dict], *, engine_label: str, language: str,
                   dpi: int) -> dict:  # fmt: skip
    """The words document: the text-layer reader's batch pages for the whole PDF, the
    outline, and how they were recognised."""
    words = sum(len(p["words"]) for p in pages)
    return {
        "version": DOCUMENT_VERSION,
        "kind": "ocr",
        "pdf_sha256": src.book.sha256,
        "engine": engine_label,
        "language": language,
        "dpi": dpi,
        "pages": pages,
        "toc": src.scan.get("toc") or [],
        "truncated": False,
        "stats": {
            "pages": len(pages),
            "pages_with_words": sum(1 for p in pages if p["words"]),
            "pages_failed": sum(1 for p in pages if p.get("failed")),
            "words": words,
            "chars": sum(p["chars"] for p in pages),
        },
    }


def dumps(doc: dict) -> bytes:
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def publish(db: Session, settings: Settings, job: Job, src: Source, doc: dict) -> dict:
    """Write the searchable PDF and the words, then install both and finish the job in one
    step, holding the lease. Raises ``LeaseLost`` when the lease (or the file) is gone."""
    from lectern.align.jobs import LeaseLost
    from lectern.fsops import LIBRARY_LOCK
    from lectern.jobs import _place
    from lectern.security.tokens import new_secret

    stage = queue.staging_dir(settings.library_dir, job.id, new_secret()[:8])
    try:
        stage.mkdir(parents=True)
        words_file = stage / "ocr.json"
        data = dumps(doc)
        words_file.write_bytes(data)
        pdf_file = stage / "ocr.pdf"
        child.assemble(src.path, doc["pages"], pdf_file, memory_mb=settings.ocr_memory_limit_mb)
        pdf_digest = _sha256_file(pdf_file)
        result = {
            **doc["stats"],
            "language": doc["language"],
            "engine": doc["engine"],
            "pdf_size": pdf_file.stat().st_size,
            "words_sha256": hashlib.sha256(data).hexdigest(),
        }
        with LIBRARY_LOCK:
            db.refresh(src.work)
            db.refresh(src.book)
            if src.work.trashed_at is not None or src.book.sha256 != doc["pdf_sha256"]:
                raise LeaseLost()
            if not queue.hold_lease(db, job):
                raise LeaseLost()
            try:
                dest_pdf = pdf_path(settings.library_dir, src.work, src.book.sha256)
                dest_words = words_path(settings.library_dir, src.work, src.book.sha256)
                dest_pdf.parent.mkdir(parents=True, exist_ok=True)
                _place(pdf_file, dest_pdf, pdf_digest)
                _place(words_file, dest_words, result["words_sha256"])
                now = utcnow()
                db.execute(
                    update(Job)
                    .where(Job.id == job.id)
                    .values(
                        status=JobStatus.SUCCEEDED,
                        result=result,
                        finished_at=now,
                        error=None,
                        lease_expires_at=None,
                        updated_at=now,
                    )  # fmt: skip
                    .execution_options(synchronize_session=False)
                )
            except BaseException:
                db.rollback()
                raise
            db.commit()
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    db.refresh(job)
    shutil.rmtree(queue.artifacts_dir(settings.data_dir, job.id), ignore_errors=True)
    log.info("ocr job %s: %d words on %d pages", job.id, result["words"], result["pages"])
    return {"job_id": job.id, "status": "succeeded", **result}


def _sha256_file(path: Path) -> str:
    from lectern.ingest.hashing import sha256_file

    return sha256_file(path)


# --- running a batch (the server's runner and workers) -----------------------------------------


def recognise_batch(
    path: Path, first: int, last: int, *, tesseract: str, language: str, dpi: int,
    memory_mb: int, threads: int = 1, on_page=None,
) -> dict:  # fmt: skip
    """Pages ``[first, last)`` of ``path``: ``{"first", "pages": [{"index", "words", "conf",
    "dpi", "failed"?}]}``. A page that runs out of time or memory is tried once more at half
    the resolution, then kept empty (``failed``). Other errors raise :class:`child.OcrError`."""
    out = []
    for index in range(first, last):
        page = None
        for attempt_dpi in (dpi, max(100, dpi // 2)):
            try:
                page = child.ocr_page(path, index, tesseract=tesseract, language=language,
                                      dpi=attempt_dpi, memory_mb=memory_mb, threads=threads)  # fmt: skip
                break
            except child.OcrError as exc:
                if exc.code not in ("timed_out", "out_of_memory", "crashed", "output_limit"):
                    raise
                log.warning("page %d at %d dpi: %s", index + 1, attempt_dpi, exc)
        if page is None:
            page = {"index": index, "words": [], "conf": None, "dpi": 0, "failed": True}
        out.append(page)
        if on_page is not None:
            on_page(index)
    return {"first": first, "pages": out}


PERMANENT = {
    "engine_missing": "Tesseract is not installed",
    "not_pdf": "the PDF could not be read",
    "pdf_encrypted": "the PDF is password-protected",
    "too_many_pages": "the PDF has too many pages",
}


def failure_message(exc: child.OcrError, language: str) -> tuple[str, bool]:
    """``(message, retryable)`` for a job that could not go on."""
    if exc.code == "language_missing":
        return (f"Tesseract has no data for {language!r} here (install tesseract-ocr-"
                f"{language.split('+')[0].replace('_', '-')}, or pick another language)",
                False)  # fmt: skip
    if exc.code in PERMANENT:
        return PERMANENT[exc.code], False
    return f"text recognition failed: {exc}", True


class OcrRunner:
    """The server's own OCR runner: one page at a time, one job at a time, in one thread.
    It leaves new jobs to an online ``ocr`` worker for ``LECTERN_LOCAL_DEFER_SECONDS``."""

    def __init__(self, session_factory: sessionmaker, settings: Settings, *,
                 poll_s: float = 10.0) -> None:  # fmt: skip
        self.session_factory = session_factory
        self.settings = settings
        self.poll_s = poll_s
        self._stop = threading.Event()
        self.wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lectern-ocr", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.wake.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        with self.session_factory() as db:
            recover_local(db)
        while not self._stop.is_set():
            try:
                while not self._stop.is_set() and self.run_once():
                    pass
            except Exception:  # noqa: BLE001
                log.exception("ocr runner iteration failed")
            self.wake.wait(self.poll_s)
            self.wake.clear()

    def run_once(self) -> bool:
        """Claim and run one job; ``False`` when there was none."""
        from lectern.align import jobs as align_jobs

        found = local_engine(self.settings)
        if found is None:
            return False
        with self.session_factory() as db:
            claimed = align_jobs.claim(
                db,
                kinds=[KIND],
                capabilities=[queue.OCR],
                worker="local",
                lease_s=LEASE_S,
                credential_id=None,
                defer_s=self.settings.local_defer_seconds,
                stall_s=self.settings.align_stall_seconds,
            )
            if claimed is None:
                return False
            job, _token = claimed
            self._run_job(db, job, found)
        return True

    def _run_job(self, db: Session, job: Job, found: tuple[str, tuple[str, ...]]) -> None:
        from lectern.align import jobs as align_jobs
        from lectern.align.jobs import LeaseLost

        version, langs = found
        payload = job.payload or {}
        language = str(payload.get("language") or engine.DEFAULT_LANGUAGE)
        try:
            missing = engine.missing_languages(language, langs)
            if missing:
                raise child.OcrError("language_missing", "+".join(missing))
            src = source_of(db, self.settings, job)
            total = int(payload.get("pages") or 0)
            if total != len(src.scan["pages"]):
                raise Stale()
            store = queue.artifacts_dir(self.settings.data_dir, job.id)
            store.mkdir(parents=True, exist_ok=True)
            dpi = int(payload.get("dpi") or self.settings.ocr_dpi)
            for first, last in batches(total, int(payload.get("batch_pages") or BATCH_PAGES)):
                out = store / batch_name(first)
                if out.is_file():
                    continue

                def on_page(index: int, total=total) -> None:
                    if self._stop.is_set():
                        raise _Stopping()
                    db.refresh(job)
                    if job.status != JobStatus.LEASED:
                        raise LeaseLost()
                    align_jobs.heartbeat(db, job, progress=(index + 1) / max(1, total),
                                         message=f"page {index + 1} of {total}")  # fmt: skip

                data = recognise_batch(
                    src.path, first, last, tesseract=self.settings.tesseract_path,
                    language=language, dpi=dpi, memory_mb=self.settings.ocr_memory_limit_mb,
                    threads=self.settings.ocr_threads, on_page=on_page,
                )  # fmt: skip
                _write_json(out, data)
            pages = read_batches(src, {f: store / batch_name(f) for f, _ in batches(
                total, int(payload.get("batch_pages") or BATCH_PAGES))})  # fmt: skip
            doc = build_document(src, pages, engine_label=engine.label(version) or "tesseract",
                                 language=language, dpi=dpi)  # fmt: skip
            publish(db, self.settings, job, src, doc)
        except _Stopping:
            db.rollback()
            align_jobs.release(db, db.get(Job, job.id))
        except LeaseLost:
            db.rollback()  # cancelled, or the work went away: nothing to record
        except Stale:
            db.rollback()
            self._fail(db, job, "the PDF changed or was removed since it was queued", False)
        except child.OcrError as exc:
            db.rollback()
            self._fail(db, job, *failure_message(exc, language))
        except Exception as exc:  # noqa: BLE001 - recorded on the job
            log.exception("ocr job %s failed", job.id)
            db.rollback()
            self._fail(db, job, f"text recognition failed: {exc}"[:500], True)

    def _fail(self, db: Session, job: Job, message: str, retryable: bool) -> None:
        from lectern.align import jobs as align_jobs

        job = db.get(Job, job.id, populate_existing=True)
        if job is not None and job.status == JobStatus.LEASED:
            align_jobs.fail(db, job, message, retryable=retryable)


class _Stopping(Exception):
    pass


def recover_local(db: Session) -> int:
    """Leases the server's own runner held when the server stopped: retried (with backoff,
    resuming from the stored batches) or failed when attempts are used up."""
    from lectern.align import jobs as align_jobs

    n = 0
    for job in db.scalars(
        select(Job).where(
            Job.kind == KIND, Job.status == JobStatus.LEASED, Job.leased_by_id.is_(None)
        )
    ).all():
        align_jobs.fail(db, job, "the server restarted during text recognition", retryable=True)
        n += 1
    return n


# --- external workers -----------------------------------------------------------------------------


class ResultBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first: int = Field(ge=0, le=100_000)
    artifact: str = Field(pattern=ARTIFACT_RE)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class OcrResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: str = Field(min_length=1, max_length=64)
    language: str = Field(max_length=64)
    batches: list[ResultBatch] = Field(max_length=10_000)


def _worker_inputs(db: Session, settings: Settings, job: Job) -> dict | None:
    try:
        src = source_of(db, settings, job)
    except Stale:
        return None
    return {"pdf": queue.InputFile(src.path, formats.PDF, src.book.sha256)}


def _worker_result(db: Session, settings: Settings, job: Job, body: bytes, artifacts: Path) -> dict:
    """Check a worker's batches (pinned, like every artifact) and publish them."""
    from lectern.align.jobs import LeaseLost, ResultError
    from lectern.jobs import _pin
    from lectern.security.tokens import new_secret

    if job.status == JobStatus.SUCCEEDED:
        return {"job_id": job.id, "status": "succeeded", **(job.result or {})}
    if len(body) > RESULT_MAX_BYTES:
        raise ResultError(413, "result_too_large", [])
    try:
        parsed = OcrResult.model_validate_json(body)
    except (ValidationError, ValueError) as exc:
        raise ResultError(400, "invalid_result", [str(exc)[:500]]) from exc
    payload = job.payload or {}
    if parsed.language != payload.get("language"):
        raise ResultError(422, "input_mismatch", ["language"])
    try:
        src = source_of(db, settings, job)
    except Stale:
        raise LeaseLost() from None
    expected = {f for f, _ in batches(len(src.scan["pages"]),
                                       int(payload.get("batch_pages") or BATCH_PAGES))}  # fmt: skip
    got = [b.first for b in parsed.batches]
    if len(got) != len(set(got)) or set(got) != expected:
        raise ResultError(
            422, "missing_batches", [batch_name(f) for f in sorted(expected - set(got))][:20]
        )
    pin = settings.data_dir / "tmp" / f"ocr-{job.id}-{new_secret()[:8]}"
    pin.mkdir(parents=True)
    try:
        files = {}
        for b in parsed.batches:
            copy = pin / batch_name(b.first)
            try:
                digest = _pin(artifacts / b.artifact, copy)
            except FileNotFoundError:
                digest = None
            if digest != b.artifact_sha256:
                raise ResultError(422, "missing_artifact", [b.artifact])
            files[b.first] = copy
        try:
            pages = read_batches(src, files)
        except ValueError as exc:
            raise ResultError(422, "invalid_pages", [str(exc)[:300]]) from exc
        doc = build_document(src, pages, engine_label=parsed.engine,
                             language=parsed.language, dpi=int(payload.get("dpi") or 0))  # fmt: skip
        try:
            return publish(db, settings, job, src, doc)
        except child.OcrError as exc:
            raise ResultError(422, "assembly_failed", [str(exc)[:300]]) from exc
    finally:
        shutil.rmtree(pin, ignore_errors=True)


queue.register(
    queue.KindHandler(
        kind=KIND,
        capability=lambda _payload: queue.OCR,
        inputs=_worker_inputs,
        apply_result=_worker_result,
    )
)


# --- status for the API ---------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    from lectern.align.jobs import _iso as iso

    return iso(dt)


def status_json(db: Session, settings: Settings, book: BookFile, work: Work) -> dict:
    """What the work's card shows for one PDF (docs/ocr.md, "API")."""
    found = local_engine(settings)
    scan = load_scan(settings.data_dir, book.sha256)
    history = jobs_for(db, book)
    latest = history[0] if history else None
    done = has_output(settings.library_dir, work, book.sha256)
    default_lang = engine.language_for(work.language)
    out: dict = {
        "file_id": book.id,
        "work_id": work.id,
        "status": "checking",
        "pages": len(scan["pages"]) if scan and "pages" in scan else None,
        "pages_with_text": scan.get("pages_with_text") if scan else None,
        "language": (latest.payload or {}).get("language") if latest else default_lang,
        "default_language": default_lang,
        "languages": list(found[1]) if found else [],
        "engine": engine.label(found[0]) if found else None,
        "worker": worker_can_ocr(db),
        "job": None,
        "progress": None,
        "message": None,
        "error": None,
        "result": None,
        "download_url": None,
        "size": None,
    }
    if latest is not None:
        out["job"] = {
            "id": latest.id,
            "status": latest.status.value,
            "attempts": latest.attempts,
            "max_attempts": latest.max_attempts,
            "created_at": _iso(latest.created_at),
            "finished_at": _iso(latest.finished_at),
            "run_after": _iso(latest.run_after),
        }
    if latest is not None and latest.status in JOB_ACTIVE_STATUSES:
        res = latest.result or {}
        out["status"] = "running" if latest.status == JobStatus.LEASED else "queued"
        out["progress"] = res.get("progress") if latest.status == JobStatus.LEASED else None
        out["message"] = res.get("message")
        if latest.status == JobStatus.QUEUED:
            if latest.error:
                out["error"] = latest.error  # a retry is waiting for its backoff
            if found is None and not queue.capability_online(db, queue.OCR):
                out["message"] = "Waiting for a worker with text recognition"
        return out
    if done:
        path = pdf_path(settings.library_dir, work, book.sha256)
        ok = latest if latest is not None and latest.status == JobStatus.SUCCEEDED else None
        out.update(
            status="done",
            result=(ok.result if ok else None),
            download_url=f"/api/files/{book.id}/ocr/pdf",
            size=path.stat().st_size,
        )
        return out
    if scan is None:
        return out
    if "error" in scan:
        out.update(status="unreadable", error=scan["error"])
        return out
    if not scan.get("needs_ocr"):
        out["status"] = "not_needed"
        return out
    if latest is not None and latest.status == JobStatus.FAILED:
        out.update(status="failed", error=latest.error)
        return out
    if found is None and not out["worker"]:
        out.update(status="unavailable", message=NO_ENGINE)
        return out
    out["status"] = "needs_ocr"
    return out
