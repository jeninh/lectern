"""FastAPI dependencies that gate every route except /healthz, /setup and /login."""

from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from lectern.config import Settings
from lectern.db import get_db
from lectern.models import AppPassword, Role, Session, SyncKey, User, utcnow
from lectern.security.csrf import verify_csrf
from lectern.security.sessions import load_session
from lectern.security.tokens import sha256_hex

log = logging.getLogger("lectern.auth")


def app_settings(request: Request) -> Settings:
    """Settings bound to this app instance (create_app may be given explicit settings)."""
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(app_settings)]
DbDep = Annotated[DbSession, Depends(get_db)]


class LoginRequired(Exception):
    """Raised for browser routes; the handler redirects to /login."""


@dataclass
class AuthContext:
    user: User
    session: Session


def _rate_limited(request: Request, bucket: str, key: str) -> None:
    limiter = request.app.state.limiters[bucket]
    if not limiter.allow(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")


def current_auth_optional(request: Request, db: DbDep) -> AuthContext | None:
    found = load_session(db, request)
    if found is None:
        return None
    session, user = found
    return AuthContext(user=user, session=session)


AuthOptional = Annotated[AuthContext | None, Depends(current_auth_optional)]


def require_auth(auth: AuthOptional) -> AuthContext:
    if auth is None:
        raise LoginRequired()
    return auth


Auth = Annotated[AuthContext, Depends(require_auth)]


async def csrf_protected(request: Request, auth: Auth, settings: SettingsDep) -> AuthContext:
    await verify_csrf(request, settings, auth.session.csrf_token)
    return auth


AuthCsrf = Annotated[AuthContext, Depends(csrf_protected)]


def require_role(*roles: Role):
    def _dep(auth: AuthCsrf) -> AuthContext:
        if auth.user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return auth

    return _dep


def require_role_read(*roles: Role):
    def _dep(auth: Auth) -> AuthContext:
        if auth.user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return auth

    return _dep


OwnerOnly = Annotated[AuthContext, Depends(require_role(Role.OWNER))]


# --- OPDS: HTTP Basic with a per-device app password -----------------------------------

_BASIC_CHALLENGE = {"WWW-Authenticate": 'Basic realm="Lectern", charset="UTF-8"'}


def _basic_unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, "Authentication required", headers=_BASIC_CHALLENGE
    )


def opds_user(request: Request, db: DbDep) -> User:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        raise _basic_unauthorized()
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        username, _, secret = decoded.partition(":")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise _basic_unauthorized() from exc
    ip = request.client.host if request.client else "?"
    _rate_limited(request, "basic", f"{ip}:{username[:64]}")
    row = db.scalar(select(AppPassword).where(AppPassword.key_hash == sha256_hex(secret)))
    if row is None or row.revoked_at is not None:
        raise _basic_unauthorized()
    user = db.get(User, row.user_id)
    if user is None or not user.is_active or user.username != username:
        raise _basic_unauthorized()
    row.last_used_at = utcnow()
    db.commit()
    return user


OpdsUser = Annotated[User, Depends(opds_user)]


# --- KOReader sync: x-auth-user / x-auth-key ----------------------------------------------


@dataclass
class SyncAuth:
    user: User
    key: SyncKey


def _sync_unauthorized() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, {"message": "Unauthorized"})


def kosync_auth(request: Request, db: DbDep) -> SyncAuth:
    username = request.headers.get("x-auth-user", "")
    key = request.headers.get("x-auth-key", "")
    if not username or not key or len(username) > 64 or len(key) > 128:
        raise _sync_unauthorized()
    ip = request.client.host if request.client else "?"
    _rate_limited(request, "kosync", f"{ip}:{username[:64]}")
    # KOReader sends md5(password) as x-auth-key; we store sha256 of that string.
    row = db.scalar(select(SyncKey).where(SyncKey.key_hash == sha256_hex(key.lower())))
    if row is None or row.revoked_at is not None:
        raise _sync_unauthorized()
    user = db.get(User, row.user_id)
    if user is None or not user.is_active or user.username != username:
        raise _sync_unauthorized()
    row.last_used_at = utcnow()
    db.commit()
    return SyncAuth(user=user, key=row)


KoSync = Annotated[SyncAuth, Depends(kosync_auth)]
