"""The worker half of the job protocol (docs/contracts.md, "Worker protocol").

Only a worker credential (``Authorization: Bearer lw_…``) is accepted here, and every call
after the claim also needs the job's lease token (``X-Lease-Token``). The rules live in
``lectern.align.jobs``; this module maps them onto HTTP.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lectern.align import jobs
from lectern.align.tracks import work_epub, work_tracks
from lectern.http_range import file_response
from lectern.models import Job, Work
from lectern.security.deps import DbDep, SettingsDep, WorkerAuth

log = logging.getLogger("lectern.worker")

router = APIRouter(prefix="/worker")

LeaseToken = Annotated[str | None, Header(alias="X-Lease-Token", max_length=128)]


# Small JSON bodies (claim, heartbeat, fail) are read with a hard cap before parsing, so a
# worker key cannot make the server buffer megabytes per request. Results have their own cap.
SMALL_BODY_MAX_BYTES = 64 * 1024


async def read_capped(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "too_large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def small_json(model: type[BaseModel]):
    """Dependency: the request body parsed as ``model`` (empty body = defaults), ≤ 64 KiB."""

    async def _dep(request: Request) -> BaseModel:
        raw = await read_capped(request, SMALL_BODY_MAX_BYTES)
        try:
            return model.model_validate_json(raw) if raw.strip() else model()
        except ValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid request") from exc

    return Depends(_dep)


class ClaimIn(BaseModel):
    kinds: list[str] = Field(default=[jobs.KIND_ALIGN], min_length=1, max_length=8)
    worker: str = Field(default="worker", max_length=128)
    lease_s: int = Field(default=600, ge=1, le=86400)


class HeartbeatIn(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    progress: float | None = Field(default=None, ge=0, le=1)
    message: str | None = Field(default=None, max_length=200)
    stats: jobs.AlignStats | None = None


class FailIn(BaseModel):
    error: str = Field(default="", max_length=2000)
    retryable: bool = True


def _lease_lost() -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, "lease_lost")


def _leased(db, cred, job_id: int, token: str | None, *, allow_succeeded: bool = False) -> Job:
    try:
        return jobs.verify_lease(
            db, job_id, token, credential_id=cred.id, allow_succeeded=allow_succeeded
        )
    except jobs.LeaseLost as exc:
        raise _lease_lost() from exc


def claim_body(job: Job, token: str, lease_s: int) -> dict:
    payload = job.payload or {}
    return {
        "job": {
            "id": job.id,
            "kind": job.kind,
            "work_id": job.work_id,
            "attempt": job.attempts,
            "max_attempts": job.max_attempts,
            "payload": payload,
        },
        "lease": {
            "token": token,
            "expires_at": jobs._iso(job.lease_expires_at),
            "lease_s": lease_s,
        },
        "inputs": {
            "epub": f"/api/worker/jobs/{job.id}/inputs/epub",
            "tracks": [
                f"/api/worker/jobs/{job.id}/inputs/tracks/{t['position']}"
                for t in payload.get("tracks", [])
            ],
        },
        "result_url": f"/api/worker/jobs/{job.id}/result",
    }


@router.post("/jobs/claim")
def claim(cred: WorkerAuth, db: DbDep, body: ClaimIn = small_json(ClaimIn)):
    claimed = jobs.claim(
        db, kinds=body.kinds, worker=body.worker, lease_s=body.lease_s, credential_id=cred.id
    )
    if claimed is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    job, token = claimed
    log.info("job %d leased by %s (%s)", job.id, body.worker, cred.label)
    return claim_body(job, token, int((job.result or {}).get("lease_s") or body.lease_s))


@router.get("/jobs/{job_id}/inputs/epub")
def input_epub(
    request: Request,
    cred: WorkerAuth,
    db: DbDep,
    settings: SettingsDep,
    job_id: int,
    x_lease_token: LeaseToken = None,
):
    job = _leased(db, cred, job_id, x_lease_token)
    if not jobs.inputs_current(db, settings.library_dir, job):
        raise _lease_lost()
    work = db.get(Work, job.work_id)
    found = work_epub(db, settings.library_dir, work) if work else None
    if found is None:
        raise _lease_lost()
    book, path = found
    return _input(request, path, book.media_type, book.sha256)


@router.get("/jobs/{job_id}/inputs/tracks/{position}")
def input_track(
    request: Request,
    cred: WorkerAuth,
    db: DbDep,
    settings: SettingsDep,
    job_id: int,
    position: int,
    x_lease_token: LeaseToken = None,
):
    job = _leased(db, cred, job_id, x_lease_token)
    if not jobs.inputs_current(db, settings.library_dir, job):
        raise _lease_lost()
    work = db.get(Work, job.work_id)
    track = next((t for t in work_tracks(db, work.id) if t.position == position), None)
    if track is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    path = (settings.library_dir / work.dir_path / track.rel_path).resolve()
    if settings.library_dir.resolve() not in path.parents or not path.is_file():
        raise _lease_lost()
    return _input(request, path, track.media_type, track.sha256)


@router.post("/jobs/{job_id}/heartbeat")
def heartbeat(
    cred: WorkerAuth,
    db: DbDep,
    job_id: int,
    body: HeartbeatIn = small_json(HeartbeatIn),
    x_lease_token: LeaseToken = None,
) -> dict:
    job = _leased(db, cred, job_id, x_lease_token)
    expires = jobs.heartbeat(
        db, job, progress=body.progress, message=body.message, stats=body.stats
    )
    return {"expires_at": jobs._iso(expires)}


@router.put("/jobs/{job_id}/result")
async def result(
    request: Request,
    cred: WorkerAuth,
    db: DbDep,
    settings: SettingsDep,
    job_id: int,
    x_lease_token: LeaseToken = None,
) -> dict:
    job = await run_in_threadpool(_leased, db, cred, job_id, x_lease_token, allow_succeeded=True)
    body = await read_capped(request, jobs.RESULT_MAX_BYTES)
    try:
        # Parsing, hashing, and spine extraction take seconds on a big book; off the loop.
        return await run_in_threadpool(
            jobs.apply_result, db, settings.data_dir, settings.library_dir, job, body
        )
    except jobs.LeaseLost as exc:
        db.rollback()
        raise _lease_lost() from exc
    except jobs.ResultError as exc:
        db.rollback()
        detail = {"detail": exc.code}
        if exc.errors:
            detail["errors"] = exc.errors
        raise HTTPException(exc.status, detail) from exc


@router.post("/jobs/{job_id}/fail")
def fail(
    cred: WorkerAuth,
    db: DbDep,
    job_id: int,
    body: FailIn = small_json(FailIn),
    x_lease_token: LeaseToken = None,
) -> dict:
    job = _leased(db, cred, job_id, x_lease_token)
    out = jobs.fail(db, job, body.error, retryable=body.retryable)
    log.warning("job %d failed on %s: %s (%s)", job.id, cred.label, body.error, out["status"])
    if out.get("run_after"):
        out["run_after"] = jobs._iso(out["run_after"])
    return out


def _input(request: Request, path, media_type: str, sha256: str) -> Response:
    """A job input with its content hash, which the worker checks against the job payload.
    Not metered: worker downloads are not a user's downloads."""
    return file_response(
        request,
        path,
        media_type,
        sha256,
        cache_control="no-store",
        headers={"X-Content-SHA256": sha256},
    )
