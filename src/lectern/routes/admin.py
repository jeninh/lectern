"""Owner-only user management."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from lectern.models import AuditLog, Role, User
from lectern.security import audit
from lectern.security.deps import AuthContext, DbDep, OwnerOnly, require_role_read
from lectern.security.passwords import hash_password, validate_password_strength
from lectern.security.sessions import client_ip, revoke_all_sessions
from lectern.templating import render

router = APIRouter(prefix="/admin")
OwnerRead = Annotated[AuthContext, Depends(require_role_read(Role.OWNER))]

USERNAME_RE = r"^[a-z0-9][a-z0-9._-]{1,31}$"


@router.get("/users", include_in_schema=False)
def users_page(request: Request, auth: OwnerRead, db: DbDep):
    request.state.auth = auth
    users = db.scalars(select(User).order_by(User.username)).all()
    return render(request, "admin_users.html", {"users": users, "roles": [r.value for r in Role]})


@router.post("/users", include_in_schema=False)
def create_user(
    request: Request,
    auth: OwnerOnly,
    db: DbDep,
    username: Annotated[str, Form(pattern=USERNAME_RE)],
    role: Annotated[str, Form()],
    password: Annotated[str, Form(max_length=256)],
    display_name: Annotated[str, Form(max_length=128)] = "",
):
    request.state.auth = auth
    if role not in (Role.MEMBER.value, Role.READER.value):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Role must be member or reader")
    users = db.scalars(select(User).order_by(User.username)).all()
    error = validate_password_strength(password)
    if error is None and db.scalar(select(User).where(User.username == username)) is not None:
        error = "Username already exists."
    if error:
        return render(
            request,
            "admin_users.html",
            {"users": users, "roles": [r.value for r in Role], "error": error},
            400,
        )
    user = User(
        username=username,
        display_name=display_name.strip() or username,
        role=Role(role),
        password_hash=hash_password(password),
        webauthn_user_handle=secrets.token_bytes(32),
    )
    db.add(user)
    audit.record(
        db,
        "user.create",
        actor_user_id=auth.user.id,
        target=username,
        ip=client_ip(request),
        detail=role,
    )
    db.commit()
    return RedirectResponse("/admin/users", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/disable", include_in_schema=False)
def disable_user(request: Request, auth: OwnerOnly, db: DbDep, user_id: int):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if user.id == auth.user.id or user.role == Role.OWNER:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot disable the owner")
    user.is_active = False
    revoke_all_sessions(db, user.id)
    audit.record(
        db, "user.disable", actor_user_id=auth.user.id, target=user.username, ip=client_ip(request)
    )
    db.commit()
    return RedirectResponse("/admin/users", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/enable", include_in_schema=False)
def enable_user(request: Request, auth: OwnerOnly, db: DbDep, user_id: int):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    user.is_active = True
    audit.record(
        db, "user.enable", actor_user_id=auth.user.id, target=user.username, ip=client_ip(request)
    )
    db.commit()
    return RedirectResponse("/admin/users", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/audit", include_in_schema=False)
def audit_page(request: Request, auth: OwnerRead, db: DbDep):
    request.state.auth = auth
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(200)).all()
    users = {u.id: u.username for u in db.scalars(select(User)).all()}
    return render(request, "admin_audit.html", {"rows": rows, "users": users})
