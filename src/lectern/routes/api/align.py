"""Alignment as seen by users: jobs and worker credentials (tracks: ``routes/api/works.py``).

Visibility: a work the caller may not see is ``404`` (``lectern.access``). Job management is
owner + member; worker credentials are owner only. Everything here is session + CSRF, exactly
like the rest of ``/api``; the worker-facing half of the protocol is ``routes/api/worker.py``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern.align import jobs
from lectern.align.transcribe import MODELS
from lectern.models import (
    AlignmentStatus,
    Job,
    JobStatus,
    Role,
    WorkerCredential,
    utcnow,
)
from lectern.routes.api.readalong import visible_work
from lectern.routes.api.selection import Selection
from lectern.routes.api.selection import resolve as resolve_selection
from lectern.routes.api.serializers import iso
from lectern.security import audit
from lectern.security.deps import (
    WORKER_KEY_PREFIX,
    AuthContext,
    DbDep,
    OwnerOnly,
    SettingsDep,
    require_role,
    require_role_read,
)
from lectern.security.sessions import client_ip
from lectern.security.tokens import new_secret, sha256_hex

router = APIRouter()

JobManager = Annotated[AuthContext, Depends(require_role(Role.OWNER, Role.MEMBER))]
JobViewer = Annotated[AuthContext, Depends(require_role_read(Role.OWNER, Role.MEMBER))]
JOBS_PAGE = 50


# --- jobs ---------------------------------------------------------------------------------------


class AlignIn(BaseModel):
    model: str | None = Field(default=None, max_length=64)
    force: bool = False


@router.get("/jobs")
def list_jobs(
    auth: JobViewer,
    db: DbDep,
    status_: str | None = Query(default=None, alias="status", max_length=16),
    work_id: int | None = Query(default=None, ge=1),
    kind: str | None = Query(default=None, max_length=32),
) -> dict:
    stmt = select(Job)
    if status_:
        try:
            stmt = stmt.where(Job.status == JobStatus(status_))
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad status") from exc
    if work_id is not None:
        stmt = stmt.where(Job.work_id == work_id)
    if kind:
        stmt = stmt.where(Job.kind == kind)
    rows = db.scalars(stmt.order_by(Job.id.desc()).limit(JOBS_PAGE)).all()
    workers = _worker_names(db)
    speed = jobs.measured_speed(db, None)
    return {
        "jobs": [jobs.job_json(db, j, workers) for j in rows],
        "measured_speed": ({"speed": round(speed[0], 3), "samples": speed[1]} if speed else None),
    }


@router.get("/jobs/{job_id}")
def job_detail(auth: JobViewer, db: DbDep, job_id: int) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return jobs.job_json(db, job, _worker_names(db))


@router.post("/works/{work_id}/align")
def request_align(
    request: Request,
    auth: JobManager,
    db: DbDep,
    settings: SettingsDep,
    response: Response,
    work_id: int,
    body: AlignIn | None = None,
) -> dict:
    work = visible_work(db, auth.user, work_id)
    body = body or AlignIn()
    if body.model is not None and body.model not in MODELS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown_model")
    jobs.refresh_work(db, settings.library_dir, work, auto_enqueue=False)
    db.commit()
    existing = jobs.active_job(db, work.id)
    if body.force and existing is not None and existing.status == JobStatus.LEASED:
        # A worker is spending CPU-hours on it; force would throw that away on every click.
        # Cancelling is still possible, explicitly, with POST /api/jobs/{id}/cancel.
        raise HTTPException(status.HTTP_409_CONFLICT, "job_running")
    alignment = jobs.work_alignment(db, work.id)
    retry = _retry_model(db, work.id)
    if (
        body.model is None
        and retry is None
        and alignment is not None
        and alignment.status == AlignmentStatus.FAILED
        and jobs.out_of_memory(alignment.error)
    ):
        # Out of memory with the smallest model: never quietly rerun something as big or
        # bigger. A rerun names its model (the editor's Re-run alignment always does).
        raise HTTPException(status.HTTP_409_CONFLICT, "out_of_memory_choose_model")
    try:
        job, created = jobs.enqueue_align(
            db,
            settings.library_dir,
            work,
            model=body.model or retry or settings.align_model,
            created_by=auth.user.id,
            force=body.force,
        )
    except jobs.NoInputs as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "no_inputs") from exc
    if created:
        audit.record(
            db,
            "align.request",
            actor_user_id=auth.user.id,
            target=f"work:{work.id}",
            ip=client_ip(request),
            detail=job.payload.get("model") if job.payload else None,
        )
    db.commit()
    response.status_code = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
    return jobs.job_json(db, job, _worker_names(db))


# Bulk alignment checks each work's files in the request (refresh_work), and every job is
# CPU-hours of worker time: a small cap keeps both the request and the commitment bounded.
ALIGN_MANY_MAX = 50


class AlignManyIn(BaseModel):
    work_ids: list[int] = Field(default_factory=list, max_length=ALIGN_MANY_MAX)
    selection: Selection | None = None
    model: str | None = Field(default=None, max_length=64)


@router.post("/works/align")
def request_align_many(
    request: Request, auth: JobManager, db: DbDep, settings: SettingsDep, body: AlignManyIn
) -> dict:
    """Queue alignment for many works (the Library's selection bar, after a confirmation).
    Never forces and never re-runs: a work with an active job reports ``already_queued``, one
    whose alignment is current ``already_aligned`` (re-run one work from its editor). One
    result per work, in order: ``queued``, ``already_queued``, ``already_aligned``,
    ``no_inputs``, ``out_of_memory_choose_model``, or ``not_found``. At most
    ``ALIGN_MANY_MAX`` works."""
    if body.model is not None and body.model not in MODELS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown_model")
    ids = resolve_selection(db, auth.user, body.work_ids, body.selection, limit=ALIGN_MANY_MAX)
    results = []
    for wid in ids:
        try:
            work = visible_work(db, auth.user, wid)
        except HTTPException:
            results.append({"work_id": wid, "status": "not_found"})
            continue
        if (job := jobs.active_job(db, work.id)) is not None:
            # Already on its way; a stale snapshot is re-queued by the worker sweep and edits.
            results.append({"work_id": wid, "status": "already_queued", "job_id": job.id})
            continue
        # Marks a changed book's alignment stale (and re-queues it with its own model).
        requeued = jobs.refresh_work(db, settings.library_dir, work, auto_enqueue=False)
        db.commit()
        if requeued is not None:
            results.append({"work_id": wid, "status": "queued", "job_id": requeued.id})
            continue
        alignment = jobs.work_alignment(db, work.id)
        if alignment is not None and alignment.status == AlignmentStatus.SUCCEEDED:
            results.append({"work_id": wid, "status": "already_aligned"})
            continue
        # The same rules as POST /api/works/{id}/align: retry with the smaller model a
        # failure chose, and never rerun an out-of-memory failure without a named model.
        alignment = jobs.work_alignment(db, work.id)
        retry = _retry_model(db, work.id)
        if (
            body.model is None
            and retry is None
            and alignment is not None
            and alignment.status == AlignmentStatus.FAILED
            and jobs.out_of_memory(alignment.error)
        ):
            results.append({"work_id": wid, "status": "out_of_memory_choose_model"})
            continue
        try:
            job, created = jobs.enqueue_align(
                db,
                settings.library_dir,
                work,
                model=body.model or retry or settings.align_model,
                created_by=auth.user.id,
            )
        except jobs.NoInputs:
            db.rollback()
            results.append({"work_id": wid, "status": "no_inputs"})
            continue
        if created:
            audit.record(
                db,
                "align.request",
                actor_user_id=auth.user.id,
                target=f"work:{work.id}",
                ip=client_ip(request),
                detail=job.payload.get("model") if job.payload else None,
            )
        db.commit()
        results.append(
            {"work_id": wid, "status": "queued" if created else "already_queued", "job_id": job.id}
        )
    return {"results": results}


@router.post("/jobs/{job_id}/cancel")
def cancel(request: Request, auth: JobManager, db: DbDep, job_id: int) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if jobs.cancel_job(db, job):
        audit.record(
            db,
            "job.cancel",
            actor_user_id=auth.user.id,
            target=f"job:{job.id}",
            ip=client_ip(request),
        )
        db.commit()
    return jobs.job_json(db, job, _worker_names(db))


def _retry_model(db: Session, work_id: int) -> str | None:
    """A Retry (no model named) of a failed alignment: the model that failed, or a smaller
    one after running out of memory (``lectern.align.jobs.retry_model``)."""
    return jobs.retry_model(jobs.work_alignment(db, work_id))


def _worker_names(db: Session) -> dict[int, str]:
    return dict(db.execute(select(WorkerCredential.id, WorkerCredential.label)).all())


# --- worker credentials (owner only) ------------------------------------------------------------


class CredentialIn(BaseModel):
    label: str = Field(min_length=1, max_length=128)


def _credential_json(c: WorkerCredential) -> dict:
    return {
        "id": c.id,
        "label": c.label,
        "created_at": iso(c.created_at),
        "last_used_at": iso(c.last_used_at),
        "revoked_at": iso(c.revoked_at),
    }


@router.get("/worker-credentials")
def list_credentials(auth: OwnerOnly, db: DbDep) -> dict:
    rows = db.scalars(select(WorkerCredential).order_by(WorkerCredential.id)).all()
    return {"credentials": [_credential_json(c) for c in rows]}


@router.post("/worker-credentials", status_code=status.HTTP_201_CREATED)
def create_credential(request: Request, auth: OwnerOnly, db: DbDep, body: CredentialIn) -> dict:
    secret = WORKER_KEY_PREFIX + new_secret()
    row = WorkerCredential(
        label=body.label.strip(), key_hash=sha256_hex(secret), created_by=auth.user.id
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        "worker_credential.create",
        actor_user_id=auth.user.id,
        target=f"worker_credential:{row.id}",
        ip=client_ip(request),
        detail=row.label,
    )
    db.commit()
    return {**_credential_json(row), "secret": secret}


@router.delete("/worker-credentials/{credential_id}")
def revoke_credential(
    request: Request, auth: OwnerOnly, db: DbDep, settings: SettingsDep, credential_id: int
) -> dict:
    row = db.get(WorkerCredential, credential_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        audit.record(
            db,
            "worker_credential.revoke",
            actor_user_id=auth.user.id,
            target=f"worker_credential:{row.id}",
            ip=client_ip(request),
            detail=row.label,
        )
        # its leases go back to the queue now, not at the next sweep
        jobs.reclaim_expired(db, stall_s=settings.align_stall_seconds)
        db.commit()
    return _credential_json(row)
