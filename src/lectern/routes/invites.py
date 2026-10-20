"""Household invites: owner-issued, expiring, single-use sign-up links.

``/api/invites`` is owner-only management. ``/invite/{token}`` is the only unauthenticated
route milestone 2 adds: the invitee chooses their own username and password and is signed in.
Used, expired, revoked, and unknown tokens all answer the same 404 page.
"""

from __future__ import annotations

import re
import secrets
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from lectern import notify
from lectern.models import Invite, Role, User, utcnow
from lectern.routes.api.serializers import iso
from lectern.security import alerts, audit
from lectern.security.csrf import _origin_ok
from lectern.security.deps import AuthContext, DbDep, SettingsDep, require_role
from lectern.security.passwords import hash_password, validate_password_strength
from lectern.security.sessions import client_ip, create_session, set_session_cookie
from lectern.security.tokens import new_secret, sha256_hex
from lectern.templating import render

api = APIRouter(prefix="/api/invites")
pages = APIRouter()
OwnerOnly = Annotated[AuthContext, Depends(require_role(Role.OWNER))]
USERNAME_RE = r"^[a-z0-9][a-z0-9._-]{1,31}$"


def _state(inv: Invite) -> str:
    if inv.used_at is not None:
        return "used"
    if inv.revoked_at is not None:
        return "revoked"
    if inv.expires_at <= utcnow():
        return "expired"
    return "pending"


def invite_json(inv: Invite, users: dict[int, str]) -> dict:
    return {
        "id": inv.id,
        "role": inv.role.value,
        "note": inv.note,
        "state": _state(inv),
        "created_by": users.get(inv.created_by),
        "created_at": iso(inv.created_at),
        "expires_at": iso(inv.expires_at),
        "used_at": iso(inv.used_at),
        "used_by": users.get(inv.used_by),
        "revoked_at": iso(inv.revoked_at),
    }


class InviteIn(BaseModel):
    role: Literal["member", "reader"]
    note: str = Field(default="", max_length=128)
    expires_in_hours: int = Field(default=72, ge=1, le=720)


@api.post("", status_code=status.HTTP_201_CREATED)
def create_invite(
    request: Request, auth: OwnerOnly, db: DbDep, settings: SettingsDep, body: InviteIn
) -> dict:
    token = new_secret(32)
    inv = Invite(
        token_hash=sha256_hex(token),
        role=Role(body.role),
        note=body.note.strip(),
        created_by=auth.user.id,
        expires_at=utcnow() + timedelta(hours=body.expires_in_hours),
    )
    db.add(inv)
    db.flush()
    audit.record(
        db,
        "invite.create",
        actor_user_id=auth.user.id,
        target=inv.note or f"invite:{inv.id}",
        ip=client_ip(request),
        detail=body.role,
    )
    db.commit()
    return {
        **invite_json(inv, {auth.user.id: auth.user.username}),
        "url": f"{settings.base_url}/invite/{token}",
        "token": token,
    }


@api.get("")
def list_invites(auth: OwnerOnly, db: DbDep) -> dict:
    users = {u.id: u.username for u in db.scalars(select(User))}
    rows = db.scalars(select(Invite).order_by(Invite.created_at.desc()).limit(200))
    return {"invites": [invite_json(i, users) for i in rows]}


@api.delete("/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_invite(request: Request, auth: OwnerOnly, db: DbDep, invite_id: int) -> Response:
    inv = db.get(Invite, invite_id)
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if inv.revoked_at is None and inv.used_at is None:
        inv.revoked_at = utcnow()
        audit.record(
            db,
            "invite.revoke",
            actor_user_id=auth.user.id,
            target=inv.note or f"invite:{inv.id}",
            ip=client_ip(request),
        )
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _limit(request: Request) -> None:
    if not request.app.state.limiters["login"].allow(client_ip(request) or "?"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts")


def _usable(db, token: str) -> Invite | None:
    if not token or len(token) > 128:
        return None
    inv = db.scalar(select(Invite).where(Invite.token_hash == sha256_hex(token)))
    return inv if inv is not None and inv.usable_at(utcnow()) else None


def _invalid(request: Request):
    return render(request, "invite_invalid.html", status_code=status.HTTP_404_NOT_FOUND)


def _form(request: Request, db, inv: Invite, token: str, status_code: int = 200, **extra):
    inviter = db.get(User, inv.created_by) if inv.created_by else None
    return render(
        request,
        "invite.html",
        {
            "token": token,
            "role": inv.role.value,
            "inviter": inviter.display_name if inviter else None,
            "expires": inv.expires_at.strftime("%d %b %Y %H:%M UTC"),
            **extra,
        },
        status_code=status_code,
    )


@pages.get("/invite/{token}", include_in_schema=False)
def invite_form(request: Request, db: DbDep, token: str):
    _limit(request)
    inv = _usable(db, token)
    if inv is None:
        return _invalid(request)
    return _form(request, db, inv, token)


@pages.post("/invite/{token}", include_in_schema=False)
def invite_accept(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    token: str,
    username: Annotated[str, Form(max_length=64)],
    password: Annotated[str, Form(max_length=256)],
    password_confirm: Annotated[str, Form(max_length=256)],
    display_name: Annotated[str, Form(max_length=128)] = "",
):
    _limit(request)
    if not _origin_ok(request, settings):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-site request blocked")
    inv = _usable(db, token)
    if inv is None:
        return _invalid(request)
    username = username.strip().lower()
    error = None
    if not re.match(USERNAME_RE, username):
        error = "Usernames are 2-32 lowercase letters, digits, dots, dashes, or underscores."
    elif db.scalar(select(User).where(User.username == username)) is not None:
        error = "That username is taken."
    else:
        error = validate_password_strength(password)
        if error is None and password != password_confirm:
            error = "Passwords do not match."
    if error:
        return _form(
            request, db, inv, token, 400, error=error, username=username, display_name=display_name
        )

    user = User(
        username=username,
        display_name=display_name.strip() or username,
        role=inv.role,
        password_hash=hash_password(password),
        webauthn_user_handle=secrets.token_bytes(32),
    )
    db.add(user)
    db.flush()
    inv.used_at, inv.used_by = utcnow(), user.id
    ip = client_ip(request)
    audit.record(
        db, "invite.accept", actor_user_id=user.id, target=username, ip=ip, detail=inv.role.value
    )
    audit.record(db, "user.create", actor_user_id=user.id, target=username, ip=ip, detail="invite")
    for owner in notify.owners(db):
        notify.notify(
            db,
            owner,
            "invite.accepted",
            f"{user.display_name} joined as a {inv.role.value}",
            f"@{username} used the invite{f' for {inv.note}' if inv.note else ''}."
            + (" Grant them shelves in the Library." if inv.role == Role.READER else ""),
            data={"user_id": user.id},
        )
    session_token = create_session(db, user, request, settings)
    device_token = alerts.remember_device_quietly(db, request, user)
    db.commit()
    response = RedirectResponse("/app?welcome=1", status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, session_token, settings)
    alerts.set_device_cookie(response, device_token, settings)
    return response
