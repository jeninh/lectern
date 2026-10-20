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
    Job,
    JobStatus,
    Role,
    WorkerCredential,
    utcnow,
)
from lectern.routes.api.readalong import visible_work
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
    try:
        job, created = jobs.enqueue_align(
            db,
            settings.library_dir,
            work,
            model=body.model or settings.align_model,
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
def revoke_credential(request: Request, auth: OwnerOnly, db: DbDep, credential_id: int) -> dict:
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
        jobs.reclaim_expired(db)  # its leases go back to the queue now, not at the next sweep
        db.commit()
    return _credential_json(row)
