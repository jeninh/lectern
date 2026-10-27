"""PDF → EPUB conversion of a work (issue #28), as a job (docs/pdf-epub.md).

- ``GET    /api/works/{id}/pdf-epub``  eligibility, the latest job, the converted edition, the report
- ``POST   /api/works/{id}/pdf-epub``  queue a conversion of the work's text PDF
- ``POST   /api/works/{id}/pdf-epub/retry``  queue the last failed one again
- ``DELETE /api/works/{id}/pdf-epub``  remove the converted edition (the PDF stays)
- ``POST   /api/works/pdf-epub``  queue conversions for many works (the selection bar, #61)
- ``GET    /api/pdf-epub/jobs?ids=``  the state of those jobs, for per-book progress

Job management (list, cancel) is the shared ``/api/jobs`` (``routes/api/align.py``).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from lectern.access import visible_works
from lectern.ingest import formats
from lectern.models import BookFile, Job, JobStatus, Role, Work
from lectern.pdf2epub import jobs as pj
from lectern.pdf2epub.build import PDF2EPUB
from lectern.routes.api.readalong import visible_work
from lectern.routes.api.selection import Selection
from lectern.routes.api.selection import resolve as resolve_selection
from lectern.security import audit
from lectern.security.deps import (
    AuthContext,
    DbDep,
    SettingsDep,
    require_role,
    require_role_read,
)
from lectern.security.sessions import client_ip

router = APIRouter()

Manager = Annotated[AuthContext, Depends(require_role(Role.OWNER, Role.MEMBER))]
Viewer = Annotated[AuthContext, Depends(require_role_read(Role.OWNER, Role.MEMBER))]


def _wake(request: Request) -> None:
    runner = getattr(request.app.state, "jobs", None)
    if runner is not None:
        runner.wake.set()


def _job_json(job) -> dict | None:
    if job is None:
        return None
    result = job.result or {}
    payload = job.payload or {}
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status.value,
        "error": job.error,
        "pdf_sha256": payload.get("pdf_sha256"),
        "sections": result.get("report", {}).get("sections") if result else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


@router.get("/works/{work_id}/pdf-epub")
def state(auth: Viewer, db: DbDep, settings: SettingsDep, work_id: int) -> dict:
    work = visible_work(db, auth.user, work_id)
    reason = pj.eligibility(db, settings, work)
    job = pj.latest_job(db, work_id)
    edition = pj.converted_file(db, work.id)
    report = (job.result or {}).get("report") if job else None
    return {
        "work_id": work.id,
        "eligible": reason is None,
        "reason": reason,
        "job": _job_json(job),
        "edition": (
            {
                "file": edition.rel_path,
                "size": edition.size,
                "sha256": edition.sha256,
                "added_at": edition.added_at.isoformat(),
            }
            if edition
            else None
        ),
        "report": report,
    }


@router.post("/works/{work_id}/pdf-epub", status_code=status.HTTP_202_ACCEPTED)
def convert(
    request: Request, auth: Manager, db: DbDep, settings: SettingsDep, work_id: int
) -> dict:
    work = visible_work(db, auth.user, work_id)
    reason = pj.eligibility(db, settings, work)
    if reason == "converted":
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": "converted"})
    if reason is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": reason})
    job = pj.enqueue(db, settings, work, auth.user.id)
    audit.record(db, "pdf2epub.request", actor_user_id=auth.user.id, target=f"work:{work.id}",
                 ip=client_ip(request), detail=f"job {job.id}")  # fmt: skip
    db.commit()
    _wake(request)
    return {"status": job.status.value, "job_id": job.id}


@router.post("/works/{work_id}/pdf-epub/retry", status_code=status.HTTP_202_ACCEPTED)
def retry(request: Request, auth: Manager, db: DbDep, settings: SettingsDep, work_id: int) -> dict:
    work = visible_work(db, auth.user, work_id)
    if pj.active_job(db, work.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": "already_running"})
    last = pj.latest_job(db, work.id)
    if last is None or last.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": "nothing_to_retry"})
    reason = pj.eligibility(db, settings, work)
    if reason is not None and reason != "converted":
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": reason})
    job = pj.enqueue(db, settings, work, auth.user.id)
    audit.record(db, "pdf2epub.retry", actor_user_id=auth.user.id, target=f"work:{work.id}",
                 ip=client_ip(request), detail=f"job {job.id}")  # fmt: skip
    db.commit()
    _wake(request)
    return {"status": job.status.value, "job_id": job.id}


@router.delete("/works/{work_id}/pdf-epub")
def remove_edition(
    request: Request, auth: Manager, db: DbDep, settings: SettingsDep, work_id: int
) -> dict:
    work = visible_work(db, auth.user, work_id)
    if pj.active_job(db, work.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": "already_running"})
    if not pj.remove(db, settings, work):
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"detail": "no_converted_edition"})
    audit.record(db, "pdf2epub.delete", actor_user_id=auth.user.id, target=f"work:{work.id}",
                 ip=client_ip(request))  # fmt: skip
    db.commit()
    return {"removed": True}


# --- many works at once (issue #61) ----------------------------------------------------------------

log = logging.getLogger("lectern.pdf2epub")

# Each conversion runs in the server, one at a time (lectern.jobs.run_pending claims them
# serially), so a big selection queues work rather than running it all at once; the cap keeps
# a single request (and the queue it leaves behind) bounded on a Raspberry Pi.
CONVERT_MANY_MAX = 200
JOB_IDS_MAX = CONVERT_MANY_MAX


class ConvertManyIn(BaseModel):
    work_ids: list[int] = Field(default_factory=list, max_length=CONVERT_MANY_MAX)
    selection: Selection | None = None


def _skip_reason(db, settings, work: Work) -> str | None:
    """Why a selected work is not converted: it already has an EPUB edition (``already_epub``,
    an uploaded one, a Kindle book converted at ingest, or an earlier conversion), it has no
    PDF at all (``unsupported``: comics, audiobooks), or its PDF has no usable text yet
    (``no_text_pdf``: a scan that needs OCR, or text still being read)."""
    media = set(db.scalars(select(BookFile.media_type).where(BookFile.work_id == work.id)))
    if formats.EPUB in media:
        return "already_epub"
    if formats.PDF not in media:
        return "unsupported"
    return pj.eligibility(db, settings, work)


@router.post("/works/pdf-epub")
def convert_many(
    request: Request, auth: Manager, db: DbDep, settings: SettingsDep, body: ConvertManyIn
) -> dict:
    """Queue a PDF → EPUB conversion for every selected work that can have one. One result per
    work, in order, each its own transaction (a failure never stops the rest): ``ok`` with
    ``status`` ``queued`` or ``already_queued`` (and the ``job_id``), or not ``ok`` with
    ``status`` ``skipped`` or ``failed`` and an ``error`` (:func:`_skip_reason`, ``not_found``,
    ``internal_error``); each with the work's ``title`` when it was found. At most
    ``CONVERT_MANY_MAX`` works per request."""
    if not request.app.state.limiters["bulk_jobs"].allow(f"user:{auth.user.id}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited")
    ids = resolve_selection(db, auth.user, body.work_ids, body.selection, limit=CONVERT_MANY_MAX)
    results, queued = [], []
    for wid in ids:
        item: dict = {"work_id": wid}
        try:
            work = visible_work(db, auth.user, wid)
            item["title"] = work.title
            if (job := pj.active_job(db, work.id)) is not None:
                item.update(ok=True, status="already_queued", job_id=job.id)
            elif (reason := _skip_reason(db, settings, work)) is not None:
                item.update(ok=False, status="skipped", error=reason)
            else:
                job = pj.enqueue(db, settings, work, auth.user.id)
                db.commit()
                queued.append(job.id)
                item.update(ok=True, status="queued", job_id=job.id)
        except HTTPException:
            db.rollback()
            item.update(ok=False, status="failed", error="not_found")
        except Exception:  # noqa: BLE001 - one work failing must not stop the rest
            log.exception("bulk pdf2epub: work %s failed", wid)
            db.rollback()
            item.update(ok=False, status="failed", error="internal_error")
        results.append(item)
    audit.record(db, "pdf2epub.bulk_request", actor_user_id=auth.user.id,
                 target=f"{len(queued)}/{len(ids)} works", ip=client_ip(request),
                 detail=("jobs " + ",".join(map(str, queued)))[:500] if queued else None)  # fmt: skip
    db.commit()
    if queued:
        _wake(request)
    return {"results": results}


@router.get("/pdf-epub/jobs")
def jobs_state(
    auth: Viewer, db: DbDep, ids: str = Query(default="", max_length=JOB_IDS_MAX * 12)
) -> dict:
    """Where each of these conversion jobs is (``ids``: comma-separated, at most
    ``JOB_IDS_MAX``): the selection bar's per-book progress. Jobs of works the caller may not
    see, and jobs of other kinds, are left out."""
    try:
        wanted = list(dict.fromkeys(int(x) for x in ids.split(",") if x.strip()))
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "bad_ids") from None
    if len(wanted) > JOB_IDS_MAX:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "too_many_ids")
    rows = db.scalars(
        select(Job)
        .join(Work, Work.id == Job.work_id)
        .where(Job.id.in_(wanted), Job.kind == PDF2EPUB, visible_works(auth.user))
    ).all()
    by_id = {j.id: j for j in rows}
    return {
        "jobs": [{**_job_json(by_id[i]), "work_id": by_id[i].work_id} for i in wanted if i in by_id]
    }
