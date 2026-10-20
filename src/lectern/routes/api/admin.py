"""Owner-only user management and audit log."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from lectern.models import AuditLog, Role, User
from lectern.routes.api.serializers import iso, user_json
from lectern.security import audit
from lectern.security.deps import AuthContext, DbDep, require_role, require_role_read
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
