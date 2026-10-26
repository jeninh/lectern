"""PDF → EPUB conversion of a work (issue #28), as a job (docs/pdf-epub.md).

- ``GET    /api/works/{id}/pdf-epub``  eligibility, the latest job, the converted edition, the report
- ``POST   /api/works/{id}/pdf-epub``  queue a conversion of the work's text PDF
- ``POST   /api/works/{id}/pdf-epub/retry``  queue the last failed one again
- ``DELETE /api/works/{id}/pdf-epub``  remove the converted edition (the PDF stays)

Job management (list, cancel) is the shared ``/api/jobs`` (``routes/api/align.py``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from lectern.models import JobStatus, Role
from lectern.pdf2epub import jobs as pj
from lectern.routes.api.readalong import visible_work
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
