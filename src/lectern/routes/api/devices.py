"""Per-device credentials: KOReader sync keys and OPDS app passwords.

A device is a sync key plus the app password issued alongside it (``AppPassword.sync_key_id``).
Credentials created on their own, including those from before pairing existed, appear as
single-purpose devices.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from lectern.models import AppPassword, BookFile, ReadingProgress, SyncKey, Work, utcnow
from lectern.routes.api.serializers import app_password_json, iso, sync_key_json
from lectern.security import audit
from lectern.security.deps import Auth, AuthCsrf, DbDep
from lectern.security.sessions import client_ip
from lectern.security.tokens import new_secret, sha256_hex, sync_key_hash

router = APIRouter(prefix="/devices")


class LabelIn(BaseModel):
    label: str = Field(min_length=1, max_length=128)


def _latest(*values):
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _device_json(key: SyncKey | None, pw: AppPassword | None) -> dict:
    parts = [p for p in (key, pw) if p is not None]
    return {
        "id": f"k{key.id}" if key else f"p{pw.id}",  # type: ignore[union-attr]
        "label": parts[0].label,
        "created_at": iso(min(p.created_at for p in parts)),
        "last_used_at": iso(_latest(*(p.last_used_at for p in parts))),
        # A device is revoked only once every credential it holds is revoked.
        "revoked_at": iso(_latest(*(p.revoked_at for p in parts)))
        if all(p.revoked_at for p in parts)
        else None,
        "sync_key": sync_key_json(key) if key else None,
        "app_password": app_password_json(pw) if pw else None,
    }


@router.get("")
def list_devices(auth: Auth, db: DbDep) -> dict:
    keys = db.scalars(
        select(SyncKey).where(SyncKey.user_id == auth.user.id).order_by(SyncKey.created_at.desc())
    ).all()
    pws = db.scalars(
        select(AppPassword)
        .where(AppPassword.user_id == auth.user.id)
        .order_by(AppPassword.created_at.desc())
    ).all()
    paired = {p.sync_key_id: p for p in pws if p.sync_key_id is not None}
    devices = [_device_json(k, paired.get(k.id)) for k in keys]
    key_ids = {k.id for k in keys}
    devices += [_device_json(None, p) for p in pws if p.sync_key_id not in key_ids]
    devices.sort(key=lambda d: d["created_at"] or "", reverse=True)
    return {
        "devices": devices,
        "sync_keys": [sync_key_json(k) for k in keys],
        "app_passwords": [app_password_json(p) for p in pws],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_device(request: Request, auth: AuthCsrf, db: DbDep, body: LabelIn) -> dict:
    """Issue a KOReader sync key and an OPDS app password as one device. Secrets shown once."""
    label = body.label.strip()
    sync_secret, opds_secret = new_secret(24), new_secret(24)
    key = SyncKey(user_id=auth.user.id, label=label, key_hash=sync_key_hash(sync_secret))
    db.add(key)
    db.flush()
    pw = AppPassword(
        user_id=auth.user.id, label=label, key_hash=sha256_hex(opds_secret), sync_key_id=key.id
    )
    db.add(pw)
    db.flush()
    audit.record(
        db, "device.create", actor_user_id=auth.user.id, target=label, ip=client_ip(request)
    )
    db.commit()
    return {
        "device": _device_json(key, pw),
        "username": auth.user.username,
        "sync_secret": sync_secret,
        "opds_secret": opds_secret,
    }


@router.post("/{device_id}/revoke")
def revoke_device(request: Request, auth: AuthCsrf, db: DbDep, device_id: str) -> dict:
    """Revoke every credential a device holds (``k<sync key id>`` or ``p<app password id>``)."""
    kind, num = device_id[:1], device_id[1:]
    if kind not in ("k", "p") or not num.isdigit():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    key: SyncKey | None = None
    pw: AppPassword | None = None
    if kind == "k":
        key = db.get(SyncKey, int(num))
        if key is None or key.user_id != auth.user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        pw = db.scalar(select(AppPassword).where(AppPassword.sync_key_id == key.id))
    else:
        pw = db.get(AppPassword, int(num))
        if pw is None or pw.user_id != auth.user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        if pw.sync_key_id is not None:
            key = db.get(SyncKey, pw.sync_key_id)
    now = utcnow()
    changed = False
    for part in (key, pw):
        if part is not None and part.revoked_at is None:
            part.revoked_at = now
            changed = True
    if changed:
        audit.record(
            db,
            "device.revoke",
            actor_user_id=auth.user.id,
            target=(key or pw).label,  # type: ignore[union-attr]
            ip=client_ip(request),
        )
        db.commit()
    return {"device": _device_json(key, pw)}


@router.post("/sync-keys", status_code=status.HTTP_201_CREATED)
def create_sync_key(request: Request, auth: AuthCsrf, db: DbDep, body: LabelIn) -> dict:
    secret = new_secret(24)
    row = SyncKey(user_id=auth.user.id, label=body.label.strip(), key_hash=sync_key_hash(secret))
    db.add(row)
    db.flush()
    audit.record(
        db, "sync_key.create", actor_user_id=auth.user.id, target=row.label, ip=client_ip(request)
    )
    db.commit()
    return {"sync_key": sync_key_json(row), "secret": secret, "username": auth.user.username}


@router.get("/sync-keys/{key_id}")
def sync_key_status(auth: Auth, db: DbDep, key_id: int) -> dict:
    """Polled by the Add Device flow to confirm the first sync from a new device."""
    row = db.get(SyncKey, key_id)
    if row is None or row.user_id != auth.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    latest = db.execute(
        select(ReadingProgress, Work)
        .join(BookFile, BookFile.partial_md5 == ReadingProgress.document)
        .join(Work, Work.id == BookFile.work_id)
        .where(ReadingProgress.sync_key_id == row.id)
        .order_by(ReadingProgress.updated_at.desc())
        .limit(1)
    ).first()
    first_sync = None
    if latest:
        p, work = latest
        first_sync = {
            "work_id": work.id,
            "title": work.title,
            "author": work.author,
            "percentage": p.percentage,
            "device": p.device,
            "at": iso(p.updated_at),
        }
    return {
        "sync_key": sync_key_json(row),
        "connected": row.last_used_at is not None,
        "latest_sync": first_sync,
    }


@router.post("/sync-keys/{key_id}/revoke")
def revoke_sync_key(request: Request, auth: AuthCsrf, db: DbDep, key_id: int) -> dict:
    row = db.get(SyncKey, key_id)
    if row is None or row.user_id != auth.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        audit.record(
            db,
            "sync_key.revoke",
            actor_user_id=auth.user.id,
            target=row.label,
            ip=client_ip(request),
        )
        db.commit()
    return {"sync_key": sync_key_json(row)}


@router.post("/app-passwords", status_code=status.HTTP_201_CREATED)
def create_app_password(request: Request, auth: AuthCsrf, db: DbDep, body: LabelIn) -> dict:
    secret = new_secret(24)
    row = AppPassword(user_id=auth.user.id, label=body.label.strip(), key_hash=sha256_hex(secret))
    db.add(row)
    db.flush()
    audit.record(
        db,
        "app_password.create",
        actor_user_id=auth.user.id,
        target=row.label,
        ip=client_ip(request),
    )
    db.commit()
    return {
        "app_password": app_password_json(row),
        "secret": secret,
        "username": auth.user.username,
    }


@router.post("/app-passwords/{key_id}/revoke")
def revoke_app_password(request: Request, auth: AuthCsrf, db: DbDep, key_id: int) -> dict:
    row = db.get(AppPassword, key_id)
    if row is None or row.user_id != auth.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        audit.record(
            db,
            "app_password.revoke",
            actor_user_id=auth.user.id,
            target=row.label,
            ip=client_ip(request),
        )
        db.commit()
    return {"app_password": app_password_json(row)}


class DeviceCapIn(BaseModel):
    # None = server default (LECTERN_DOWNLOAD_DEVICE_CAP_PER_HOUR); 0 = no cap for this device.
    cap: int | None = Field(default=None, ge=0, le=100000)


@router.post("/app-passwords/{pw_id}/download-cap")
def set_device_download_cap(
    request: Request, auth: AuthCsrf, db: DbDep, pw_id: int, body: DeviceCapIn
) -> dict:
    """Hourly download cap for one OPDS device (its app password)."""
    row = db.get(AppPassword, pw_id)
    if row is None or row.user_id != auth.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    row.download_cap_per_hour = body.cap
    audit.record(
        db,
        "device.download_cap",
        actor_user_id=auth.user.id,
        target=row.label,
        ip=client_ip(request),
        detail="default" if body.cap is None else str(body.cap),
    )
    db.commit()
    return {"app_password": app_password_json(row)}
