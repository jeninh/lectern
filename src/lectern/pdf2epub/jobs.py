"""The ``pdf2epub`` job: convert a work's text PDF into an EPUB edition, in-process.

Like ``transcode`` (lectern.jobs), the local runner claims it from the queue and runs it in
the server; unlike alignment and narration it is never offered to external workers (it only
needs the already-extracted text plus a sandboxed look at the pages, which a Raspberry Pi
handles fine, and the canonical text document lives on the server). The payload is the input
snapshot: ``{"pdf_file_id", "pdf_sha256", "text_sha256", "mode", "rules"}``; a result is only
applied while the PDF and its text document are still current.

Two modes (issue #63's analyse → review rules → convert):

- ``analyse`` reads the pages in the sandboxed probe (:mod:`lectern.pdf2epub.probe`), finds
  their structure (:mod:`lectern.pdf2epub.structure`), and stores it as
  ``derived/pdf2epub-structure.json`` with small renders of sample pages
  (``derived/pdf2epub-preview/page-N.jpg``) for the side-by-side review. No edition yet.
- ``convert`` uses that structure (analysing first when it is missing or stale), the rules in
  the payload (else the saved ones, else the detected defaults), renders the figures, tables,
  maths, and pages kept as images, and builds the edition.

What a successful job publishes, under the library lock:

- ``book-converted-from-pdf.epub`` in the work's folder, registered as a ``BookFile`` (the
  same kind of edition an uploaded EPUB is; the original PDF stays);
- ``derived/pdf2epub-report.json``, the conversion report (warnings, flagged pages);
- ``derived/pdf2epub-rules.json``, the rules it was built with (a re-run uses them);
- the work's sentence list moves to the EPUB's (the existing PDF↔EPUB projection carries
  positions over, so progress and read-along survive the new text timeline).

Deleting the edition (:func:`remove`) takes the file, the row, the report, the rules, and
the analysis back out. An edition whose PDF text has changed since (a scan recognised again,
in another language) is :func:`outdated`, and converting again replaces it (the analysis is
redone for the new text; the saved rules are reused).

A scan converts like a text PDF once OCR has given it a ``ready`` text document (issue
#62); the edition is then labelled as generated from OCR (:mod:`lectern.pdf2epub.build`).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

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
from lectern.pdf2epub import probe, structure
from lectern.pdf2epub.build import (
    CONVERTED_NAME,
    PDF2EPUB,
    REPORT_NAME,
    ConversionError,
    build_epub,
    regions,
)
from lectern.pdf2epub.rules import Rules, defaults
from lectern.pdftext import store as pdf_store

log = logging.getLogger("lectern.pdf2epub")

STRUCTURE_NAME = "pdf2epub-structure.json"
RULES_NAME = "pdf2epub-rules.json"
PREVIEW_DIR = "pdf2epub-preview"
PREVIEW_PAGES = 24  # sample pages rendered for the review


def _derived(settings: Settings, work: Work) -> Path:
    return settings.library_dir / work.dir_path / "derived"


def load_structure(settings: Settings, work: Work, text_sha256: str | None) -> dict | None:
    """The stored analysis, while it is of the work's current text document."""
    path = _derived(settings, work) / STRUCTURE_NAME
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not text_sha256 or data.get("text_sha256") != text_sha256:
        return None
    if data.get("version") != structure.STRUCTURE_VERSION:
        return None
    return data


def saved_rules(settings: Settings, work: Work) -> Rules | None:
    """The rules the current edition was built with (a re-run's defaults)."""
    try:
        return Rules.model_validate_json((_derived(settings, work) / RULES_NAME).read_bytes())
    except (OSError, ValueError):
        return None


def sample_pages(st: dict) -> list[int]:
    """1-based pages worth a look in the review: flagged pages, pages with figures, tables,
    maths, or notes, then the first pages with text, at most :data:`PREVIEW_PAGES`."""
    pages = st.get("pages") or []
    picked: list[int] = []

    def add(p: int) -> None:
        if p not in picked and len(picked) < PREVIEW_PAGES:
            picked.append(p)

    for p in pages:
        if p.get("flagged"):
            add(p["page"])
    noted = {n["page"] + 1 for n in st.get("notes") or []}
    for p in pages:
        if p.get("figures") or p.get("tables") or p.get("maths") or p["page"] in noted:
            add(p["page"])
    for p in pages:
        if p.get("content"):
            add(p["page"])
        if len(picked) >= min(PREVIEW_PAGES, 8 + len(picked) // 2):
            break
    return sorted(picked)


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


def edition_job(db: Session, edition: BookFile) -> Job | None:
    """The conversion that produced ``edition`` (its result names the edition's bytes)."""
    for job in db.scalars(
        select(Job)
        .where(Job.work_id == edition.work_id, Job.kind == PDF2EPUB,
               Job.status == JobStatus.SUCCEEDED)
        .order_by(Job.id.desc())
    ):  # fmt: skip
        if (job.result or {}).get("sha256") == edition.sha256:
            return job
    return None


def outdated(db: Session, work_id: int, edition: BookFile | None = None) -> bool:
    """Whether the converted edition was built from another text than the work's text PDF
    has now (the PDF was recognised again, or replaced). Unknown provenance is not outdated."""
    edition = edition or converted_file(db, work_id)
    pair = pdf_store.text_pdf(db, work_id)
    job = edition_job(db, edition) if edition is not None else None
    if job is None or pair is None:
        return False
    return (job.payload or {}).get("text_sha256") != pair[1].text_sha256


def eligibility(db: Session, settings: Settings, work: Work) -> str | None:
    """Why the work cannot be converted, or None when it can (an outdated edition can be
    converted again: the new one replaces it)."""
    pair = pdf_store.text_pdf(db, work.id)
    if pair is None:
        return "no_text_pdf"
    book, _row = pair
    path = settings.library_dir / work.dir_path / book.rel_path
    if not path.is_file():
        return "no_text_pdf"
    edition = converted_file(db, work.id)
    if edition is not None and not outdated(db, work.id, edition):
        return "converted"
    return None


def enqueue(
    db: Session,
    settings: Settings,
    work: Work,
    user_id: int | None = None,
    *,
    mode: str = "convert",
    rules: Rules | None = None,
    batch: str | None = None,
) -> Job:
    """Queue an analysis or a conversion of the work's text PDF (the caller checked
    eligibility). A live job of the work is returned instead of a second one. ``batch``: the
    bulk request it belongs to (the job queue page shows a batch as one row)."""
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
            "mode": mode,
            "rules": rules.model_dump() if rules is not None else None,
            **({"batch": batch} if batch else {}),
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
        if doc.get("status") != "ready":
            raise ConversionError("needs_ocr")
        pdf_path = settings.library_dir / work.dir_path / book.rel_path
        if payload.get("mode") == "analyse":
            st = _analyse(doc, pdf_path, row.text_sha256)
            previews = _render_previews(settings, job, pdf_path, sample_pages(st))
            published = _publish_analysis(db, settings, job, work, st, previews)
            result = {"mode": "analyse", "summary": summary(st)}
        else:
            st = load_structure(settings, work, row.text_sha256)
            if st is None:
                try:
                    st = _analyse(doc, pdf_path, row.text_sha256)
                except Exception as exc:  # noqa: BLE001 - convert the text without it
                    log.warning("pdf2epub job %s: analysis failed (%s); text only", job.id, exc)
            if payload.get("rules") is not None:
                rules = Rules.model_validate(payload["rules"])
            else:
                rules = saved_rules(settings, work) or (defaults(st) if st else Rules())
            images = _render_regions(settings, job, pdf_path, regions(doc, st, rules))
            epub, report = build_epub(
                doc,
                title=work.title,
                author=work.author,
                language=work.language,
                source_name=book.rel_path,
                pdf_sha256=book.sha256,
                isbn=work.isbn,
                cover_jpeg=_cover(settings, work),
                structure=st,
                rules=rules,
                images=images,
            )
            published = _publish(db, settings, job, work, epub, report, rules, st)
            result = {"mode": "convert", "file": CONVERTED_NAME, "sha256": published,
                      "report": report}  # fmt: skip
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
    if job.status == JobStatus.LEASED:  # not claimed away, cancelled, or finished meanwhile
        if published is None:
            job.status = JobStatus.FAILED
            job.error = "the work was trashed (or the job superseded) during the publish"
        else:
            # Recorded after the publish (like transcode's): a cancel committing between the
            # publish and this transition leaves the edition, but never reports a result.
            job.result = result
            job.status = JobStatus.SUCCEEDED
        job.finished_at = utcnow()
        db.commit()
    log.info("pdf2epub job %s: work %s %s", job.id, work.id, result["mode"])


def _cover(settings: Settings, work: Work) -> bytes | None:
    path = settings.library_dir / work.dir_path / "cover.jpg"
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError:
        return None


def _analyse(doc: dict, pdf_path: Path, text_sha256: str | None) -> dict:
    st = structure.analyse(doc, probe.scan(pdf_path))
    st["text_sha256"] = text_sha256
    st["pdf_sha256"] = doc.get("pdf_sha256")
    return st


def summary(st: dict) -> dict:
    pages = st.get("pages") or []
    zones = st.get("zones") or []
    return {
        "pages": len(pages),
        "flagged_pages": [p["page"] for p in pages if p.get("flagged")],
        "figures": sum(1 for z in zones if z["kind"] == "figure"),
        "tables": sum(1 for z in zones if z["kind"] == "table"),
        "complex_tables": sum(1 for z in zones if z["kind"] == "table" and z.get("complex")),
        "maths": sum(1 for z in zones if z["kind"] == "math"),
        "footnotes": len(st.get("notes") or []),
        "dropcaps": len(st.get("dropcaps") or []),
        "styled_words": len(st.get("styles") or {}),
    }


def _scratch(settings: Settings, job: Job) -> Path:
    base = settings.data_dir / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{job.id}-pdf2epub-", dir=base))


def _render_regions(settings: Settings, job: Job, pdf_path: Path, regs: list) -> dict[str, bytes]:
    """The rendered images by name; a region that could not be drawn is left out (its text
    stays in the edition)."""
    if not regs:
        return {}
    tmp = _scratch(settings, job)
    try:
        try:
            names = probe.render(pdf_path, regs, tmp)
        except Exception as exc:  # noqa: BLE001 - convert without the pictures
            log.warning("pdf2epub job %s: rendering failed: %s", job.id, exc)
            return {}
        return {n: (tmp / n).read_bytes() for n in names if (tmp / n).is_file()}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _render_previews(settings: Settings, job: Job, pdf_path: Path, pages: list[int]) -> Path:
    tmp = _scratch(settings, job)
    regs = [[p - 1, 0, 0, 0, 0, f"page-{p}.jpg", "page"] for p in pages]
    try:
        probe.render(pdf_path, regs, tmp)
    except Exception as exc:  # noqa: BLE001 - the review works without the pictures
        log.warning("pdf2epub job %s: preview rendering failed: %s", job.id, exc)
    return tmp


def _publish_analysis(
    db: Session, settings: Settings, job: Job, work: Work, st: dict, previews: Path
) -> str | None:
    from lectern.fsops import LIBRARY_LOCK, move_file
    from lectern.pdftext.document import dumps

    try:
        with LIBRARY_LOCK:
            db.refresh(work)
            db.refresh(job)
            if work.trashed_at is not None or job.status != JobStatus.LEASED:
                return None
            derived = _derived(settings, work)
            derived.mkdir(parents=True, exist_ok=True)
            (derived / STRUCTURE_NAME).write_bytes(dumps(st))
            dest = derived / PREVIEW_DIR
            shutil.rmtree(dest, ignore_errors=True)
            dest.mkdir()
            for f in sorted(previews.glob("page-*.jpg")):
                move_file(f, dest / f.name)
            return "analysed"
    finally:
        shutil.rmtree(previews, ignore_errors=True)


def _publish(
    db: Session,
    settings: Settings,
    job: Job,
    work: Work,
    epub: bytes,
    report: dict,
    rules: Rules | None = None,
    st: dict | None = None,
) -> str | None:
    """Stage the EPUB outside the library, then publish file, row, and report under the
    library lock, re-reading the work's folder (an edit may rename or trash it meanwhile).
    Returns the published file's SHA-256, or None when the guard bailed."""
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
                return None
            work_dir = settings.library_dir / work.dir_path
            work_dir.mkdir(parents=True, exist_ok=True)
            dest = work_dir / CONVERTED_NAME
            # Next to the live edition first, then one atomic replace: a failed move never
            # leaves the edition's row pointing at a missing file.
            staged = work_dir / f".{job.id}-{CONVERTED_NAME}.part"
            staged.unlink(missing_ok=True)
            move_file(scratch, staged)
            os.replace(staged, dest)
            report_dest = work_dir / "derived" / REPORT_NAME
            report_dest.parent.mkdir(parents=True, exist_ok=True)
            report_dest.unlink(missing_ok=True)
            move_file(report_scratch, report_dest)
            if rules is not None:
                (report_dest.parent / RULES_NAME).write_text(rules.model_dump_json(), "utf-8")
            if st is not None:  # the analysis this edition was built from (current text)
                (report_dest.parent / STRUCTURE_NAME).write_bytes(dumps(st))
            from lectern.ingest.hashing import sha256_file

            digest = sha256_file(dest)
            existing = converted_file(db, work.id)
            replaced = existing is not None
            if replaced:
                db.delete(existing)
                db.flush()
            db.add(
                BookFile(
                    work_id=work.id,
                    kind=FileKind.EBOOK,
                    rel_path=CONVERTED_NAME,
                    media_type=formats.EPUB,
                    size=dest.stat().st_size,
                    sha256=digest,
                )
            )
            # The work's text is this EPUB the moment the row exists: move the sentence list
            # and every stored position onto it now (the PDF↔EPUB projection carries them).
            pdf_store.ensure_pdf_sentences(db, settings.library_dir, work)
            if replaced:
                _replace_sentences(db, settings, work, dest)
            db.commit()
            return digest
    finally:
        scratch.unlink(missing_ok=True)
        report_scratch.unlink(missing_ok=True)


def _replace_sentences(db: Session, settings: Settings, work: Work, epub: Path) -> None:
    """A new edition replaced an older one (converted again after the PDF's text changed):
    the work's sentence list is still the old edition's, which ``ensure_pdf_sentences`` leaves
    alone (it is an EPUB's). Rebuild it from the new edition when that is the work's text and
    no alignment owns it, moving positions proportionally (as an edited EPUB does,
    ``lectern.editing``), and let the staleness rules handle an alignment of the old one."""
    from lectern.align import jobs as align_jobs
    from lectern.ingest.sentences import epub_sentences, replace_sentences
    from lectern.models import Alignment, AlignmentStatus, Sentence

    db.flush()
    found = pdf_store.work_text(db, settings.library_dir, work)
    # an alignment that has installed its sentence map owns the list; a queued one has not
    aligned = db.scalar(select(Alignment.id).where(
        Alignment.work_id == work.id,
        Alignment.status.in_([AlignmentStatus.SUCCEEDED, AlignmentStatus.STALE]),
    )) is not None  # fmt: skip
    if found is not None and found[0].kind == "epub" and found[0].file.rel_path == CONVERTED_NAME:
        if not aligned:
            try:
                entries = epub_sentences(epub)
            except Exception:  # noqa: BLE001 - an unreadable edition keeps the list it has
                log.warning("could not segment the converted EPUB of work %d", work.id)
            else:
                old = db.scalar(select(func.count(Sentence.id)).where(Sentence.work_id == work.id))
                move = (lambda n: round(n * len(entries) / old)) if old else None
                replace_sentences(db, work.id, entries, move=move)
    align_jobs.refresh_work(db, settings.library_dir, work, auto_enqueue=False)


def remove(db: Session, settings: Settings, work: Work) -> bool:
    """Delete the converted edition (file, row, report, rules, analysis). Returns whether
    there was one."""
    from lectern.fsops import LIBRARY_LOCK

    with LIBRARY_LOCK:
        db.refresh(work)
        book = converted_file(db, work.id)
        if book is None:
            return False
        (settings.library_dir / work.dir_path / CONVERTED_NAME).unlink(missing_ok=True)
        derived = _derived(settings, work)
        for name in (REPORT_NAME, RULES_NAME, STRUCTURE_NAME):
            (derived / name).unlink(missing_ok=True)
        shutil.rmtree(derived / PREVIEW_DIR, ignore_errors=True)
        db.delete(book)
        db.commit()
    return True


# The in-process runner claims this kind too; never offered to external workers.
queue.register(queue.KindHandler(kind=PDF2EPUB, capability=lambda _payload: PDF2EPUB, remote=False))
