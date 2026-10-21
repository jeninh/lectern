"""The worker half of the job protocol (docs/contracts.md, "Worker protocol").

Only a worker credential (``Authorization: Bearer lw_…``) is accepted here, and every call
after the claim also needs the job's lease token (``X-Lease-Token``). The one exception is
``POST /api/worker/enroll``, which exchanges a one-time enrollment code for a credential.
The rules live in ``lectern.align.jobs`` and ``lectern.queue``; this module maps them onto
HTTP.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import update

from lectern import queue
from lectern.align import jobs
from lectern.align.tracks import work_epub, work_tracks
from lectern.http_range import file_response
from lectern.models import Job, Work, WorkerCredential, WorkerEnrollment, utcnow
from lectern.security import audit
from lectern.security.deps import WORKER_KEY_PREFIX, DbDep, SettingsDep, WorkerAuth
from lectern.security.sessions import client_ip
from lectern.security.tokens import new_secret, sha256_hex

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


def _capability_list(v: list[str] | None) -> list[str] | None:
    return None if v is None else queue.valid_capabilities(v)


class WorkerInfo(BaseModel):
    """What a worker says about itself, shown to the owner (never trusted for routing)."""

    model_config = ConfigDict(extra="ignore")

    version: str | None = Field(default=None, max_length=32)
    platform: str | None = Field(default=None, max_length=64)
    engines: dict[str, str] | None = Field(default=None, max_length=16)
    reason: str | None = Field(default=None, max_length=200)

    @field_validator("engines")
    @classmethod
    def _short(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v and any(len(k) > 48 or len(str(x)) > 64 for k, x in v.items()):
            raise ValueError("engine names and versions are short strings")
        return v


class ClaimIn(BaseModel):
    # Old workers send only kinds (["align"]): their capabilities are their kinds. New workers
    # send capabilities and may leave kinds out (any kind registered for external workers).
    kinds: list[str] | None = Field(default=None, min_length=1, max_length=8)
    capabilities: list[str] | None = Field(default=None, min_length=1, max_length=32)
    worker: str = Field(default="worker", max_length=128)
    lease_s: int = Field(default=600, ge=1, le=86400)
    info: WorkerInfo | None = None

    _caps = field_validator("capabilities")(_capability_list)


class StatusIn(BaseModel):
    worker: str = Field(default="worker", max_length=128)
    state: str = Field(pattern="^(ready|busy|paused|off)$")
    capabilities: list[str] | None = Field(default=None, max_length=32)
    info: WorkerInfo | None = None

    _caps = field_validator("capabilities")(_capability_list)


class EnrollIn(BaseModel):
    code: str = Field(min_length=8, max_length=128)
    name: str = Field(default="worker", max_length=128)


class HeartbeatIn(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    progress: float | None = Field(default=None, ge=0, le=1)
    message: str | None = Field(default=None, max_length=200)
    # align: lectern.align.jobs.AlignStats; other kinds: their handler validates (or drops)
    stats: dict | None = None


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


def claim_body(job: Job, token: str, lease_s: int, inputs: dict | None = None) -> dict:
    """``inputs`` for align is ``{"epub": url, "tracks": [url, …]}`` (unchanged); for every
    other kind it is ``{name: {"url", "sha256", "media_type", "size"}}``."""
    payload = job.payload or {}
    if job.kind == jobs.KIND_ALIGN:
        inputs = {
            "epub": f"/api/worker/jobs/{job.id}/inputs/epub",
            "tracks": [
                f"/api/worker/jobs/{job.id}/inputs/tracks/{t['position']}"
                for t in payload.get("tracks", [])
            ],
        }
    return {
        "job": {
            "id": job.id,
            "kind": job.kind,
            "capability": job.capability,
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
        "inputs": inputs or {},
        "artifacts_url": f"/api/worker/jobs/{job.id}/artifacts",
        "result_url": f"/api/worker/jobs/{job.id}/result",
    }


def _input_listing(db, settings, job: Job) -> dict:
    found = queue.handler(job.kind)
    files = found.inputs(db, settings, job) if found and found.inputs else {}
    return {
        name: {
            "url": f"/api/worker/jobs/{job.id}/inputs/{name}",
            "sha256": f.sha256,
            "media_type": f.media_type,
            "size": f.path.stat().st_size if f.path.is_file() else None,
        }
        for name, f in (files or {}).items()
    }


@router.post("/jobs/claim")
def claim(cred: WorkerAuth, db: DbDep, settings: SettingsDep, body: ClaimIn = small_json(ClaimIn)):
    if body.kinds is None:
        kinds = queue.remote_kinds() if body.capabilities else [jobs.KIND_ALIGN]
    else:
        kinds = body.kinds
    advertised = body.capabilities if body.capabilities is not None else list(kinds)
    queue.record_presence(
        cred,
        state="ready",
        advertised=advertised,
        info=body.info.model_dump(exclude_none=True) if body.info else None,
    )
    db.commit()
    claimed = jobs.claim(
        db,
        kinds=kinds,
        capabilities=advertised,
        credential=cred,
        worker=body.worker,
        lease_s=body.lease_s,
        credential_id=cred.id,
        stall_s=settings.align_stall_seconds,
    )
    if claimed is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    job, token = claimed
    cred.state = "busy"
    db.commit()
    log.info("job %d (%s) leased by %s (%s)", job.id, job.capability, body.worker, cred.label)
    lease_s = int((job.result or {}).get("lease_s") or body.lease_s)
    inputs = None if job.kind == jobs.KIND_ALIGN else _input_listing(db, settings, job)
    return claim_body(job, token, lease_s, inputs)


@router.post("/status")
def worker_status(cred: WorkerAuth, db: DbDep, body: StatusIn = small_json(StatusIn)) -> dict:
    """Presence while the worker has no job: ready, paused (battery, network, switched off in
    the menu bar), or off. A paused worker does not count as online, so the server's own
    runners stop leaving jobs to it."""
    queue.record_presence(
        cred,
        state=body.state,
        advertised=body.capabilities,
        info=body.info.model_dump(exclude_none=True) if body.info else None,
    )
    db.commit()
    return {"state": cred.state, "capabilities": queue.claimable(cred.advertised or [], cred)}


@router.post("/enroll", status_code=status.HTTP_201_CREATED)
def enroll(request: Request, db: DbDep, settings: SettingsDep, body: EnrollIn) -> dict:
    """Exchange a one-time enrollment code (``POST /api/worker-enrollments``, owner) for a
    worker credential. The code is used up atomically; failures share the per-address limit
    of bad worker keys."""
    limiters = request.app.state.limiters
    ip = client_ip(request) or "?"
    ip_key = f"worker:{ip}"
    if limiters["worker_fail"].exhausted(ip_key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")
    now = utcnow()
    row = db.query(WorkerEnrollment).filter_by(code_hash=sha256_hex(body.code.strip())).first()
    claimed = 0
    if row is not None:
        claimed = db.execute(
            update(WorkerEnrollment)
            .where(
                WorkerEnrollment.id == row.id,
                WorkerEnrollment.used_at.is_(None),
                WorkerEnrollment.revoked_at.is_(None),
                WorkerEnrollment.expires_at > now,
            )
            .values(used_at=now, used_ip=ip[:64])
        ).rowcount
    if claimed != 1:
        db.rollback()
        limiters["worker_fail"].allow(ip_key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_code")
    secret = WORKER_KEY_PREFIX + new_secret()
    cred = WorkerCredential(
        label=row.label,
        key_hash=sha256_hex(secret),
        created_by=row.created_by,
        capabilities=row.capabilities,
    )
    db.add(cred)
    db.flush()
    row.credential_id = cred.id
    audit.record(
        db,
        "worker_credential.enroll",
        actor_user_id=row.created_by,
        target=f"worker_credential:{cred.id}",
        ip=client_ip(request),
        detail=f"{row.label} ({body.name[:64]})",
    )
    db.commit()
    log.info("worker enrolled: %s as credential %d", row.label, cred.id)
    return {
        "credential_id": cred.id,
        "label": cred.label,
        "secret": secret,
        "capabilities": cred.capabilities,
        "server": settings.base_url,
    }


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


@router.get("/jobs/{job_id}/inputs/{name}")
def input_named(
    request: Request,
    cred: WorkerAuth,
    db: DbDep,
    settings: SettingsDep,
    job_id: int,
    name: str,
    x_lease_token: LeaseToken = None,
):
    """A named input of a non-align job (``KindHandler.inputs``), e.g. ``script`` of a
    ``tts`` job or ``track-<id>`` of a ``transcode`` job."""
    job = _leased(db, cred, job_id, x_lease_token)
    found = queue.handler(job.kind)
    if found is None or found.inputs is None or not queue.ARTIFACT_NAME_RE.match(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    files = found.inputs(db, settings, job)
    if files is None:
        raise _lease_lost()
    f = files.get(name)
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if not f.path.is_file():
        raise _lease_lost()
    return _input(request, f.path, f.media_type, f.sha256)


@router.post("/jobs/{job_id}/heartbeat")
def heartbeat(
    cred: WorkerAuth,
    db: DbDep,
    job_id: int,
    body: HeartbeatIn = small_json(HeartbeatIn),
    x_lease_token: LeaseToken = None,
) -> dict:
    job = _leased(db, cred, job_id, x_lease_token)
    try:
        expires = jobs.heartbeat(
            db, job, progress=body.progress, message=body.message, stats=body.stats
        )
    except ValidationError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid request") from exc
    return {"expires_at": jobs._iso(expires)}


@router.post("/jobs/{job_id}/release")
def release(cred: WorkerAuth, db: DbDep, job_id: int, x_lease_token: LeaseToken = None) -> dict:
    """Give a job back without spending an attempt (the worker was paused or switched off,
    not broken). It is queued again at once; uploaded artifacts are kept for the next try."""
    job = _leased(db, cred, job_id, x_lease_token)
    jobs.release(db, job)
    cred.state = "paused"
    db.commit()
    log.info("job %d released by %s", job.id, cred.label)
    return {"status": "queued"}


# --- artifacts: partial outputs, kept across attempts ----------------------------------------

ARTIFACTS_MAX = 5000


def _artifact_path(settings, job: Job, name: str):
    if not queue.ARTIFACT_NAME_RE.match(name) or name.endswith(".part"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_name")
    return queue.artifacts_dir(settings.data_dir, job.id) / name


def _artifact_list(settings, job: Job) -> list[dict]:
    root = queue.artifacts_dir(settings.data_dir, job.id)
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.iterdir()):
        side = p.with_name(p.name + ".sha256")
        if p.suffix in (".sha256", ".part") or not p.is_file() or not side.is_file():
            continue
        out.append({"name": p.name, "size": p.stat().st_size, "sha256": side.read_text().strip()})
    return out


@router.get("/jobs/{job_id}/artifacts")
def list_artifacts(
    cred: WorkerAuth,
    db: DbDep,
    settings: SettingsDep,
    job_id: int,
    x_lease_token: LeaseToken = None,
) -> dict:
    job = _leased(db, cred, job_id, x_lease_token)
    return {"artifacts": _artifact_list(settings, job)}


@router.get("/jobs/{job_id}/artifacts/{name}")
def get_artifact(
    request: Request,
    cred: WorkerAuth,
    db: DbDep,
    settings: SettingsDep,
    job_id: int,
    name: str,
    x_lease_token: LeaseToken = None,
):
    """What an earlier attempt uploaded, so a resumed attempt can pick up from it."""
    job = _leased(db, cred, job_id, x_lease_token)
    path = _artifact_path(settings, job, name)
    side = path.with_name(path.name + ".sha256")
    if not path.is_file() or not side.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return _input(request, path, "application/octet-stream", side.read_text().strip())


@router.put("/jobs/{job_id}/artifacts/{name}", status_code=status.HTTP_201_CREATED)
async def put_artifact(
    request: Request,
    cred: WorkerAuth,
    db: DbDep,
    settings: SettingsDep,
    job_id: int,
    name: str,
    x_lease_token: LeaseToken = None,
    x_content_sha256: Annotated[str | None, Header(alias="X-Content-SHA256", max_length=64)] = None,
) -> dict:
    """Stream one output file (a generated chapter, a rendition) into the job's artifact
    directory. The body must hash to ``X-Content-SHA256``; it is written to a temporary file
    and renamed, so a half upload never shows. Re-uploading a name replaces it."""
    job = await run_in_threadpool(_leased, db, cred, job_id, x_lease_token)
    expected = (x_content_sha256 or "").lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "sha256_required")
    dest = _artifact_path(settings, job, name)
    limit = settings.worker_artifact_max_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "too_large")
    dest.parent.mkdir(parents=True, exist_ok=True)
    existing = [p for p in dest.parent.iterdir() if p.suffix == ".sha256"]
    if len(existing) >= ARTIFACTS_MAX and not dest.exists():
        raise HTTPException(status.HTTP_409_CONFLICT, "too_many_artifacts")
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.{new_secret()[:8]}.part")
    digest = hashlib.sha256()
    size = 0
    try:
        with tmp.open("wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "too_large")
                digest.update(chunk)
                await run_in_threadpool(fh.write, chunk)
        if digest.hexdigest() != expected:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "checksum_mismatch")
        # the lease may have been lost while the body streamed in
        await run_in_threadpool(_leased, db, cred, job_id, x_lease_token)
        os.replace(tmp, dest)
        dest.with_name(dest.name + ".sha256").write_text(expected)
    finally:
        tmp.unlink(missing_ok=True)
    return {"name": name, "size": size, "sha256": expected}


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
        if job.kind == jobs.KIND_ALIGN:
            return await run_in_threadpool(
                jobs.apply_result, db, settings.data_dir, settings.library_dir, job, body
            )
        found = queue.handler(job.kind)
        if found is None or found.apply_result is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "kind_not_supported")
        return await run_in_threadpool(
            found.apply_result,
            db,
            settings,
            job,
            body,
            queue.artifacts_dir(settings.data_dir, job.id),
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
    cred.state = "ready"
    db.commit()
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
