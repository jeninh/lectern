"""Per-device credentials: KOReader sync keys and OPDS app passwords."""

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
    return {
        "sync_keys": [sync_key_json(k) for k in keys],
        "app_passwords": [app_password_json(p) for p in pws],
    }


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
