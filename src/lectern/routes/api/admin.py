"""Owner-only user management, audit log, backups, and offline mode."""

from __future__ import annotations

import secrets
import threading
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from lectern import backups, egress, instance, offline_mode, queue
from lectern.db import session_factory
from lectern.models import AuditLog, Role, User, utcnow
from lectern.routes.api.serializers import iso, user_json
from lectern.security import audit
from lectern.security.deps import (
    AuthContext,
    DbDep,
    SettingsDep,
    require_role,
    require_role_read,
)
from lectern.security.passwords import hash_password, validate_password_strength
from lectern.security.sessions import client_ip, revoke_all_sessions

router = APIRouter(prefix="/admin")
OwnerRead = Annotated[AuthContext, Depends(require_role_read(Role.OWNER))]
OwnerWrite = Annotated[AuthContext, Depends(require_role(Role.OWNER))]

USERNAME_RE = r"^[a-z0-9][a-z0-9._-]{1,31}$"


@router.get("/users")
def list_users(auth: OwnerRead, db: DbDep) -> dict:
    return {"users": [user_json(u) for u in db.scalars(select(User).order_by(User.username)).all()]}


class UserIn(BaseModel):
    username: str = Field(pattern=USERNAME_RE)
    display_name: str = Field(default="", max_length=128)
    role: str = Field(pattern="^(member|reader)$")
    password: str = Field(max_length=256)


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(request: Request, auth: OwnerWrite, db: DbDep, body: UserIn) -> dict:
    error = validate_password_strength(body.password)
    if error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, error)
    if db.scalar(select(User).where(User.username == body.username)) is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Username already exists.")
    user = User(
        username=body.username,
        display_name=body.display_name.strip() or body.username,
        role=Role(body.role),
        password_hash=hash_password(body.password),
        webauthn_user_handle=secrets.token_bytes(32),
    )
    db.add(user)
    db.flush()
    audit.record(
        db,
        "user.create",
        actor_user_id=auth.user.id,
        target=user.username,
        ip=client_ip(request),
        detail=body.role,
    )
    db.commit()
    return {"user": user_json(user)}


@router.post("/users/{user_id}/disable")
def disable_user(request: Request, auth: OwnerWrite, db: DbDep, user_id: int) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if user.id == auth.user.id or user.role == Role.OWNER:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The owner cannot be disabled.")
    user.is_active = False
    revoke_all_sessions(db, user.id)
    audit.record(
        db, "user.disable", actor_user_id=auth.user.id, target=user.username, ip=client_ip(request)
    )
    db.commit()
    return {"user": user_json(user)}


@router.post("/users/{user_id}/enable")
def enable_user(request: Request, auth: OwnerWrite, db: DbDep, user_id: int) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    user.is_active = True
    audit.record(
        db, "user.enable", actor_user_id=auth.user.id, target=user.username, ip=client_ip(request)
    )
    db.commit()
    return {"user": user_json(user)}


class RoleIn(BaseModel):
    role: str = Field(pattern="^(member|reader)$")


@router.post("/users/{user_id}/role")
def set_role(request: Request, auth: OwnerWrite, db: DbDep, user_id: int, body: RoleIn) -> dict:
    """Switch a member to reader or back. Takes effect on the user's next request."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if user.role == Role.OWNER:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The owner's role cannot change.")
    if user.role.value != body.role:
        user.role = Role(body.role)
        audit.record(
            db,
            "user.role",
            actor_user_id=auth.user.id,
            target=user.username,
            ip=client_ip(request),
            detail=body.role,
        )
        db.commit()
    return {"user": user_json(user)}


class CapIn(BaseModel):
    # None = use the server default (LECTERN_DOWNLOAD_CAP_PER_HOUR); 0 = no cap.
    cap: int | None = Field(default=None, ge=0, le=100000)


@router.post("/users/{user_id}/download-cap")
def set_download_cap(
    request: Request, auth: OwnerWrite, db: DbDep, user_id: int, body: CapIn
) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    user.download_cap_per_hour = body.cap
    audit.record(
        db,
        "user.download_cap",
        actor_user_id=auth.user.id,
        target=user.username,
        ip=client_ip(request),
        detail="default" if body.cap is None else str(body.cap),
    )
    db.commit()
    return {"user": user_json(user)}


@router.get("/settings")
def server_settings(auth: OwnerRead, settings: SettingsDep) -> dict:
    """Read-only view of the limits configured through the environment."""
    return {
        "download_cap_per_hour": settings.download_cap_per_hour,
        "download_device_cap_per_hour": settings.download_device_cap_per_hour,
        "download_burst_count": settings.download_burst_count,
        "download_burst_window_seconds": settings.download_burst_window_seconds,
        "upload_max_bytes": settings.upload_max_bytes,
        "upload_quota_bytes": settings.upload_quota_bytes,
        "audio_low_codec": settings.audio_low_codec,
        "audio_low_bitrate_kbps": settings.audio_low_bitrate_kbps,
        "audio_low_auto": settings.audio_low_auto,
        "transcode_memory_limit_mb": settings.transcode_memory_limit_mb,
        # Opt-in home Wi-Fi access for e-readers (docs/home-network.md).
        "lan": {"enabled": settings.lan_enabled, "url": settings.lan_url or None},
    }


@router.get("/data-saver")
def data_saver_status(auth: OwnerRead, db: DbDep, settings: SettingsDep) -> dict:
    """Data-saver copies across the library: on or off, what can make them, what exists and
    what it saves, and the jobs still to run (Admin; the switch is ``PUT /api/library-settings``)."""
    from lectern import jobs
    from lectern.models import JOB_ACTIVE_STATUSES, AudioTrack, Job, Work

    tracks: dict[int, list] = {}
    for t in db.scalars(select(AudioTrack)):
        tracks.setdefault(t.work_id, []).append(t)
    works = covered = original = low = 0
    for work in db.scalars(
        select(Work).where(Work.id.in_(list(tracks)), Work.trashed_at.is_(None))
    ):
        works += 1
        have, o, lo = jobs.rendition_sizes(settings, work, tracks[work.id])
        covered += 1 if have else 0
        original, low = original + o, low + lo
    active = db.scalars(
        select(Job.status).where(Job.kind == jobs.TRANSCODE, Job.status.in_(JOB_ACTIVE_STATUSES))
    ).all()
    return {
        "enabled": instance.get(db, "data_saver_enabled"),
        "codec": settings.audio_low_codec,
        "bitrate_kbps": settings.audio_low_bitrate_kbps,
        "auto": settings.audio_low_auto,
        "local_ffmpeg": jobs.local_transcoder(settings),
        "worker_online": queue.capability_online(db, queue.TRANSCODE),
        "unavailable": jobs.transcoder_unavailable(db, settings),
        "audiobooks": works,
        "with_copies": covered,
        "original_bytes": original,
        "low_bytes": low,
        "jobs": {
            "queued": sum(1 for s in active if s.value == "queued"),
            "running": sum(1 for s in active if s.value == "leased"),
        },  # fmt: skip
    }


@router.get("/audit")
def audit_log(auth: OwnerRead, db: DbDep) -> dict:
    users = {u.id: u.username for u in db.scalars(select(User)).all()}
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(300)).all()
    return {
        "rows": [
            {
                "id": r.id,
                "at": iso(r.at),
                "actor": users.get(r.actor_user_id),
                "action": r.action,
                "target": r.target,
                "ip": r.ip,
                "detail": r.detail,
            }
            for r in rows
        ]
    }


# --- backups (docs/backups.md) ------------------------------------------------------------------


def _backup_view(db, settings) -> dict:
    return {
        "config": backups.load_config(db).model_dump(),
        "status": backups.load_status(db),
        "running": backups.is_running(),
        "target": backups.target_info(settings),
    }


@router.get("/backups")
def backup_settings(auth: OwnerRead, db: DbDep, settings: SettingsDep) -> dict:
    return _backup_view(db, settings)


class BackupConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    include_library: bool | None = None
    retention: int | None = Field(default=None, ge=1, le=365)
    hour_utc: int | None = Field(default=None, ge=0, le=23)


@router.put("/backups")
def update_backup_settings(
    request: Request, auth: OwnerWrite, db: DbDep, settings: SettingsDep, body: BackupConfigIn
) -> dict:
    current = backups.load_config(db)
    changes = body.model_dump(exclude_none=True)
    try:
        new = backups.BackupConfig.model_validate({**current.model_dump(), **changes})
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid settings") from exc
    if new.enabled and not backups.target_info(settings)["restic_available"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "restic_unavailable")
    if new != current:
        backups.save_config(db, new, auth.user.id)
        if new.enabled and not current.enabled:
            # The nightly schedule starts now; enabling does not back up on the spot.
            backups.record_status(db, enabled_at=utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
        diff = ", ".join(
            f"{k}={v}" for k, v in new.model_dump().items() if getattr(current, k) != v
        )
        audit.record(
            db,
            "backup.settings",
            actor_user_id=auth.user.id,
            ip=client_ip(request),
            detail=diff[:200],
        )
        db.commit()
    return _backup_view(db, settings)


@router.post("/backups/run", status_code=status.HTTP_202_ACCEPTED)
def run_backup_now(request: Request, auth: OwnerWrite, db: DbDep, settings: SettingsDep) -> dict:
    """Start a backup with the saved settings (even while nightly backups are off) and return;
    poll GET /api/admin/backups for the outcome."""
    if backups.is_running():
        raise HTTPException(status.HTTP_409_CONFLICT, "backup_running")
    audit.record(db, "backup.run", actor_user_id=auth.user.id, ip=client_ip(request))
    db.commit()
    factory = session_factory()
    threading.Thread(
        target=backups.run_and_record,
        args=(factory, settings),
        kwargs={"reason": "manual"},
        name="lectern-backup-now",
        daemon=True,
    ).start()
    return {**_backup_view(db, settings), "running": True}


# --- offline mode (docs/offline-mode.md) ------------------------------------------------------------


@router.get("/offline-mode")
def offline_mode_view(auth: OwnerRead, db: DbDep, settings: SettingsDep) -> dict:
    return offline_mode.report(db, settings)


class OfflineModeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


@router.put("/offline-mode")
def set_offline_mode(
    request: Request, auth: OwnerWrite, db: DbDep, settings: SettingsDep, body: OfflineModeIn
) -> dict:
    if offline_mode.forced(settings) and not body.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "offline_forced")
    if instance.get(db, offline_mode.KEY) != body.enabled:
        instance.set_value(db, offline_mode.KEY, body.enabled, auth.user.id)
        audit.record(
            db,
            "settings.change",
            actor_user_id=auth.user.id,
            target=offline_mode.KEY,
            ip=client_ip(request),
            detail="on" if body.enabled else "off",
        )
        db.commit()
    offline_mode.apply(db, settings)
    return offline_mode.report(db, settings)


@router.post("/offline-mode/prepare", status_code=status.HTTP_202_ACCEPTED)
def prepare_models(request: Request, auth: OwnerWrite, db: DbDep, settings: SettingsDep) -> dict:
    """Download the alignment model and TTS voices now (`lectern prepare-models` in a child
    process); poll GET /api/admin/offline-mode for its progress."""
    if egress.offline():
        raise HTTPException(status.HTTP_409_CONFLICT, "offline")
    if not offline_mode.start_prepare(getattr(request.app.state, "prepare_command", None)):
        raise HTTPException(status.HTTP_409_CONFLICT, "prepare_running")
    audit.record(db, "offline.prepare", actor_user_id=auth.user.id, ip=client_ip(request))
    db.commit()
    return offline_mode.report(db, settings)


# --- storage dashboard (docs/storage.md) ------------------------------------------------------------


def _storage_view(db, settings) -> dict:
    from lectern import storage
    from lectern.models import Job

    cached = storage.load(db)
    job = storage.active_job(db) or db.scalar(
        select(Job).where(Job.kind == storage.CLEANUP).order_by(Job.id.desc()).limit(1)
    )
    return {
        "volumes": storage.volumes(settings),
        "warn_free_percent": settings.storage_warn_free_percent,
        "scan": cached.get("result"),
        "history": cached.get("history") or [],
        "scanning": storage.scanning(),
        "blocked": storage.blocked(settings),
        "job": storage.job_json(job) if job is not None else None,
    }


@router.get("/storage")
def storage_view(auth: OwnerRead, db: DbDep, settings: SettingsDep) -> dict:
    """Free space per volume (live) and the last scan's breakdown (cached; never a walk of
    the disk on a page load). The first visit starts a scan when there is none yet."""
    from lectern import storage

    view = _storage_view(db, settings)
    if view["scan"] is None and not view["scanning"]:
        view["scanning"] = storage.start_scan(session_factory(), settings)
    return view


@router.post("/storage/scan", status_code=status.HTTP_202_ACCEPTED)
def storage_rescan(auth: OwnerWrite, db: DbDep, settings: SettingsDep) -> dict:
    from lectern import storage

    storage.start_scan(session_factory(), settings)
    return {**_storage_view(db, settings), "scanning": True}


class CleanupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(pattern="^(trash|renditions|covers|read_aloud|leftovers|pre_restore)$")
    older_than_days: int | None = Field(default=None, ge=1, le=30)
    work_id: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, pattern=r"^pre-restore-\d{8}T\d{6}Z$")


def _params(body: CleanupIn) -> dict:
    from lectern import storage

    if body.older_than_days is not None and body.action != "trash":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "older_than_days_is_for_trash")
    if body.work_id is not None and body.action not in storage.PER_WORK:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "not_per_work")
    if body.action == "pre_restore" and body.name is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name_required")  # one at a time
    if body.name is not None and body.action != "pre_restore":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name_is_for_pre_restore")
    return body.model_dump(exclude={"action"}, exclude_none=True)


@router.post("/storage/preview")
def storage_preview(
    request: Request, auth: OwnerWrite, db: DbDep, settings: SettingsDep, body: CleanupIn
) -> dict:
    """Exactly what a cleanup would remove now and how much it frees. Changes nothing."""
    from lectern import storage

    return storage.plan(db, settings, body.action, _params(body), _busy(request))


class CleanupConfirmIn(CleanupIn):
    # The keys of the previewed items to remove; the owner saw them in the preview.
    keys: list[str] = Field(min_length=1, max_length=5000)
    confirm: bool


@router.post("/storage/cleanup", status_code=status.HTTP_202_ACCEPTED)
def storage_cleanup(
    request: Request, auth: OwnerWrite, db: DbDep, settings: SettingsDep, body: CleanupConfirmIn
) -> dict:
    """Queue a confirmed cleanup. Only keys that are still in a fresh preview are taken: a
    key that is not (removed meanwhile, or never offered, such as an original) is refused."""
    from lectern import storage

    if not body.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "confirmation_required")
    if storage.active_job(db) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "cleanup_running")
    base = CleanupIn.model_validate(body.model_dump(include=set(CleanupIn.model_fields)))
    preview = storage.plan(db, settings, body.action, _params(base), _busy(request))
    offered = {i["key"] for i in preview["items"]}
    unknown = [k for k in body.keys if k not in offered]
    if unknown:
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": "preview_changed",
                                                       "keys": unknown[:20]})  # fmt: skip
    job = storage.enqueue(db, preview, body.keys, auth.user.id)
    audit.record(
        db,
        "storage.cleanup",
        actor_user_id=auth.user.id,
        target=f"job:{job.id}",
        ip=client_ip(request),
        detail=f"{body.action}: {len(body.keys)} item(s), {job.payload['expected_bytes']} bytes"[
            :200
        ],
    )
    db.commit()
    runner = getattr(request.app.state, "jobs", None)
    if runner is not None:
        runner.wake.set()
    return {**_storage_view(db, settings), "job": storage.job_json(job)}


def _busy(request: Request):
    readaloud = getattr(request.app.state, "readaloud", None)
    return readaloud.is_generating if readaloud is not None else (lambda _id: False)
