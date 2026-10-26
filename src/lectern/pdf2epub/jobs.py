"""The ``pdf2epub`` job: convert a work's text PDF into an EPUB edition, in-process.

Like ``transcode`` (lectern.jobs), the local runner claims it from the queue and runs it in
the server; unlike alignment and narration it is never offered to external workers (it only
needs the already-extracted text, which a Raspberry Pi handles fine, and the canonical text
document lives on the server). The payload is the input snapshot: ``{"pdf_file_id",
"pdf_sha256", "text_sha256"}``; a result is only applied while the PDF and its text document
are still current.

What a successful job publishes, under the library lock:

- ``book-converted-from-pdf.epub`` in the work's folder, registered as a ``BookFile`` (the
  same kind of edition an uploaded EPUB is; the original PDF stays);
- ``derived/pdf2epub-report.json``, the conversion report (warnings, flagged pages);
- the work's sentence list moves to the EPUB's (the existing PDF↔EPUB projection carries
  positions over, so progress and read-along survive the new text timeline).

Deleting the edition (:func:`remove`) takes the file, the row, and the report back out.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from lectern import queue
from lectern.config import Settings
from lectern.ingest import formats
from lectern.models import (
    JOB_ACTIVE_STATUSES,
    BookFile,
    FileKind,
    Job,
    JobStatus,
    Work,
    utcnow,
)
from lectern.pdf2epub.build import (
    CONVERTED_NAME,
    PDF2EPUB,
    REPORT_NAME,
    ConversionError,
    build_epub,
)
from lectern.pdftext import store as pdf_store

log = logging.getLogger("lectern.pdf2epub")


def converted_file(db: Session, work_id: int) -> BookFile | None:
    """The work's converted EPUB edition, if it (still) exists as a file row."""
    return db.scalar(
        select(BookFile).where(
            BookFile.work_id == work_id,
            BookFile.rel_path == CONVERTED_NAME,
            BookFile.kind == FileKind.EBOOK,
        )
    )


def active_job(db: Session, work_id: int) -> Job | None:
    return db.scalar(
        select(Job).where(
            Job.work_id == work_id, Job.kind == PDF2EPUB, Job.status.in_(JOB_ACTIVE_STATUSES)
        )
    )


def latest_job(db: Session, work_id: int) -> Job | None:
    return db.scalar(
        select(Job)
        .where(Job.work_id == work_id, Job.kind == PDF2EPUB)
        .order_by(Job.id.desc())
        .limit(1)
    )


def eligibility(db: Session, settings: Settings, work: Work) -> str | None:
    """Why the work cannot be converted, or None when it can."""
    pair = pdf_store.text_pdf(db, work.id)
    if pair is None:
        return "no_text_pdf"
    book, _row = pair
    path = settings.library_dir / work.dir_path / book.rel_path
    if not path.is_file():
        return "no_text_pdf"
    if converted_file(db, work.id) is not None:
        return "converted"
    return None


def enqueue(db: Session, settings: Settings, work: Work, user_id: int | None = None) -> Job:
    """Queue a conversion for the work's text PDF (the caller checked eligibility)."""
    existing = active_job(db, work.id)
    if existing is not None:
        return existing
    book, row = pdf_store.text_pdf(db, work.id)  # checked by eligibility
    job = Job(
        kind=PDF2EPUB,
        work_id=work.id,
        status=JobStatus.QUEUED,
        priority=30,  # after alignment (0), transcodes (10), and narration (20)
        max_attempts=3,
        payload={
            "pdf_file_id": book.id,
            "pdf_sha256": book.sha256,
            "text_sha256": row.text_sha256 or "",
        },
        created_by=user_id,
    )
    db.add(job)
    db.flush()
    return job


def run(db: Session, settings: Settings, job: Job) -> None:
    work = db.get(Work, job.work_id)
    payload = job.payload or {}
    try:
        if work is None or work.trashed_at is not None:
            raise ConversionError("work_gone")
        book = db.get(BookFile, payload.get("pdf_file_id") or 0)
        if book is None or book.work_id != work.id or book.sha256 != payload.get("pdf_sha256"):
            raise ConversionError("pdf_changed")
        row = pdf_store.pdf_text_of(db, book.id)
        doc = (
            pdf_store.load_document(settings.library_dir, work, book, row)
            if row is not None and row.text_sha256 == payload.get("text_sha256")
            else None
        )
        if doc is None:
            raise ConversionError("text_changed")
        epub, report = build_epub(
            doc,
            title=work.title,
            author=work.author,
            language=work.language,
            source_name=book.rel_path,
            pdf_sha256=book.sha256,
            isbn=work.isbn,
            cover_jpeg=_cover(settings, work),
        )
        _publish(db, settings, job, work, epub, report)
    except Exception as exc:  # noqa: BLE001 - recorded on the job
        log.warning("pdf2epub job %s failed: %s", job.id, exc)
        db.rollback()
        job = db.get(Job, job.id)
        job.status = JobStatus.FAILED
        job.error = str(exc)[:2000]
        job.finished_at = utcnow()
        db.commit()
        return
    job = db.get(Job, job.id)
    if job.status == JobStatus.LEASED:  # not claimed away or finished by _publish's guard
        job.status = JobStatus.SUCCEEDED
        job.finished_at = utcnow()
        db.commit()
    log.info("pdf2epub job %s: work %s converted (%s)", job.id, work.id, (job.result or {}).get("file"))  # fmt: skip


def _cover(settings: Settings, work: Work) -> bytes | None:
    path = settings.library_dir / work.dir_path / "cover.jpg"
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError:
        return None


def _publish(
    db: Session, settings: Settings, job: Job, work: Work, epub: bytes, report: dict
) -> None:
    """Stage the EPUB outside the library, then publish file, row, and report under the
    library lock, re-reading the work's folder (an edit may rename or trash it meanwhile)."""
    from lectern.fsops import LIBRARY_LOCK, move_file

    scratch = settings.data_dir / "tmp" / f"{job.id}-{CONVERTED_NAME}"
    report_scratch = settings.data_dir / "tmp" / f"{job.id}-{REPORT_NAME}"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_bytes(epub)
    from lectern.pdftext.document import dumps

    report_scratch.write_bytes(dumps(report))
    try:
        with LIBRARY_LOCK:
            db.refresh(work)
            db.refresh(job)
            if work.trashed_at is not None or job.status != JobStatus.LEASED:
                return
            work_dir = settings.library_dir / work.dir_path
            work_dir.mkdir(parents=True, exist_ok=True)
            dest = work_dir / CONVERTED_NAME
            dest.unlink(missing_ok=True)
            move_file(scratch, dest)
            report_dest = work_dir / "derived" / REPORT_NAME
            report_dest.parent.mkdir(parents=True, exist_ok=True)
            report_dest.unlink(missing_ok=True)
            move_file(report_scratch, report_dest)
            from lectern.ingest.hashing import sha256_file

            existing = converted_file(db, work.id)
            if existing is not None:
                db.delete(existing)
                db.flush()
            db.add(
                BookFile(
                    work_id=work.id,
                    kind=FileKind.EBOOK,
                    rel_path=CONVERTED_NAME,
                    media_type=formats.EPUB,
                    size=dest.stat().st_size,
                    sha256=sha256_file(dest),
                )
            )
            # The work's text is this EPUB the moment the row exists: move the sentence list
            # and every stored position onto it now (the PDF↔EPUB projection carries them).
            pdf_store.ensure_pdf_sentences(db, settings.library_dir, work)
            job.result = {
                "file": CONVERTED_NAME,
                "sha256": sha256_file(dest),
                "report": report,
            }
            db.commit()
    finally:
        scratch.unlink(missing_ok=True)
        report_scratch.unlink(missing_ok=True)


def remove(db: Session, settings: Settings, work: Work) -> bool:
    """Delete the converted edition (file, row, report). Returns whether there was one."""
    from lectern.fsops import LIBRARY_LOCK

    with LIBRARY_LOCK:
        db.refresh(work)
        book = converted_file(db, work.id)
        if book is None:
            return False
        (settings.library_dir / work.dir_path / CONVERTED_NAME).unlink(missing_ok=True)
        (settings.library_dir / work.dir_path / "derived" / REPORT_NAME).unlink(missing_ok=True)
        db.delete(book)
        db.commit()
    return True


# The in-process runner claims this kind too; never offered to external workers.
queue.register(queue.KindHandler(kind=PDF2EPUB, capability=lambda _payload: PDF2EPUB, remote=False))


def run_pending(session_factory: sessionmaker, settings: Settings) -> int:
    """Run every queued conversion now (the local runner calls the shared one, which claims
    transcodes as well; this claims only ours). Returns how many ran."""
    from lectern.jobs import claim

    n = 0
    while True:
        with session_factory() as db:
            claimed = claim(db, kinds=(PDF2EPUB,))
            if claimed is None:
                return n
            run(db, settings, claimed[0])
        n += 1
