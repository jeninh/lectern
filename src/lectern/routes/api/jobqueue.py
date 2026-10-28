"""The job queue page (issue #87, ``lectern.jobqueue``).

- ``GET  /api/job-queue``                          jobs grouped, batches, whether the queue is paused
- ``POST /api/job-queue/pause``  ``{"paused": bool}``  pause or resume the whole queue (owner)
- ``POST /api/job-queue/jobs/{id}/cancel``          any kind; members and readers: their own jobs
- ``POST /api/job-queue/jobs/{id}/retry``           a failed job (owner)
- ``POST /api/job-queue/jobs/{id}/pause|resume``    a waiting job (owner)
- ``POST /api/job-queue/jobs/{id}/move``  ``{"to": "top"|"bottom"}``  reorder (owner)
- ``POST /api/job-queue/batches/{batch}/cancel``    the rest of a batch (owner, or who queued it)
- ``POST /api/job-queue/batches/{batch}/pause|resume``  its waiting jobs (owner)

Session + CSRF like the rest of ``/api``. A job someone may not see is ``404``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel

from lectern import instance, jobqueue
from lectern.models import Job, Role
from lectern.routes.api.align import server_capabilities
from lectern.security import audit
from lectern.security.deps import (
    AuthContext,
    DbDep,
    OwnerOnly,
    SettingsDep,
    require_role,
    require_role_read,
)
from lectern.security.sessions import client_ip

router = APIRouter(prefix="/job-queue")

ANY_ROLE = (Role.OWNER, Role.MEMBER, Role.READER)
Viewer = Annotated[AuthContext, Depends(require_role_read(*ANY_ROLE))]
Actor = Annotated[AuthContext, Depends(require_role(*ANY_ROLE))]
BatchId = Annotated[str, Path(pattern=r"^[0-9a-f]{16}$")]


class PauseIn(BaseModel):
    paused: bool


class MoveIn(BaseModel):
    to: Literal["top", "bottom"]


def _wake(request: Request) -> None:
    runner = getattr(request.app.state, "jobs", None)
    if runner is not None:
        runner.wake.set()


def _job(db, auth: AuthContext, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if job is None or not jobqueue.may_see(job, auth.user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return job


def _audit(db, request: Request, auth: AuthContext, action: str, target: str, detail=None):
    audit.record(db, action, actor_user_id=auth.user.id, target=target, ip=client_ip(request),
                 detail=detail)  # fmt: skip


@router.get("")
def overview(auth: Viewer, db: DbDep, settings: SettingsDep) -> dict:
    return jobqueue.listing(db, auth.user, server_capabilities(db, settings))


@router.post("/pause")
def pause_queue(request: Request, auth: OwnerOnly, db: DbDep, body: PauseIn) -> dict:
    instance.set_value(db, "queue_paused", body.paused, auth.user.id)
    _audit(db, request, auth, "queue.pause" if body.paused else "queue.resume", "job-queue")
    db.commit()
    if not body.paused:
        _wake(request)
    return {"queue_paused": body.paused}


@router.post("/jobs/{job_id}/cancel")
def cancel(request: Request, auth: Actor, db: DbDep, settings: SettingsDep, job_id: int) -> dict:
    job = _job(db, auth, job_id)
    if jobqueue.cancel(db, job):
        _audit(db, request, auth, "job.cancel", f"job:{job.id}")
        db.commit()
        jobqueue.drop_partial(settings.data_dir, job)
    return {"id": job.id, "status": job.status.value}


def _owner_action(request, auth, db, job_id: int, action: str, fn) -> dict:
    job = _job(db, auth, job_id)
    try:
        fn(job)
    except jobqueue.Refused as exc:
        db.rollback()
        raise HTTPException(exc.status, exc.code) from None
    _audit(db, request, auth, f"job.{action}", f"job:{job.id}")
    db.commit()
    _wake(request)
    return {"id": job.id, "status": job.status.value, "group": jobqueue.group_of(job),
            "priority": job.priority}  # fmt: skip


@router.post("/jobs/{job_id}/retry")
def retry(request: Request, auth: OwnerOnly, db: DbDep, job_id: int) -> dict:
    return _owner_action(request, auth, db, job_id, "retry", lambda j: jobqueue.retry(db, j))


@router.post("/jobs/{job_id}/pause")
def pause(request: Request, auth: OwnerOnly, db: DbDep, job_id: int) -> dict:
    return _owner_action(request, auth, db, job_id, "pause", jobqueue.pause)


@router.post("/jobs/{job_id}/resume")
def resume(request: Request, auth: OwnerOnly, db: DbDep, job_id: int) -> dict:
    return _owner_action(request, auth, db, job_id, "resume", jobqueue.resume)


@router.post("/jobs/{job_id}/move")
def move(request: Request, auth: OwnerOnly, db: DbDep, job_id: int, body: MoveIn) -> dict:
    return _owner_action(
        request, auth, db, job_id, f"move_{body.to}", lambda j: jobqueue.move(db, j, body.to)
    )


def _batch(db, auth: AuthContext, batch: str) -> list[Job]:
    found = jobqueue.batch_jobs(db, batch, auth.user)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return found


@router.post("/batches/{batch}/cancel")
def cancel_batch(
    request: Request,
    auth: Actor,
    db: DbDep,
    settings: SettingsDep,
    batch: BatchId,
) -> dict:
    cancelled = [j for j in _batch(db, auth, batch) if jobqueue.cancel(db, j)]
    _audit(db, request, auth, "batch.cancel", f"batch:{batch}", f"{len(cancelled)} jobs")
    db.commit()
    for j in cancelled:
        jobqueue.drop_partial(settings.data_dir, j)
    return {"cancelled": len(cancelled)}


@router.post("/batches/{batch}/pause")
def pause_batch(request: Request, auth: OwnerOnly, db: DbDep, batch: BatchId) -> dict:
    jobs = [j for j in _batch(db, auth, batch) if jobqueue.group_of(j) == "waiting"]
    for j in jobs:
        jobqueue.pause(j)
    _audit(db, request, auth, "batch.pause", f"batch:{batch}", f"{len(jobs)} jobs")
    db.commit()
    return {"paused": len(jobs)}


@router.post("/batches/{batch}/resume")
def resume_batch(request: Request, auth: OwnerOnly, db: DbDep, batch: BatchId) -> dict:
    jobs = [j for j in _batch(db, auth, batch) if jobqueue.group_of(j) == "paused"]
    for j in jobs:
        jobqueue.resume(j)
    _audit(db, request, auth, "batch.resume", f"batch:{batch}", f"{len(jobs)} jobs")
    db.commit()
    _wake(request)
    return {"resumed": len(jobs)}
