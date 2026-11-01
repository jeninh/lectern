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

from lectern import guests
from lectern.config import Settings
from lectern.db import get_db
from lectern.models import AppPassword, Role, Session, SyncKey, User, WorkerCredential, utcnow
from lectern.security.csrf import verify_csrf
from lectern.security.downloads import Device
from lectern.security.sessions import client_ip, load_session
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


def require_auth(request: Request, auth: AuthOptional) -> AuthContext:
    """Every authenticated route depends on this (directly or through ``Auth``/``AuthCsrf``/
    ``require_role``), so it is where a guest is held to the allowlist in ``lectern.guests``."""
    if auth is None:
        raise LoginRequired()
    guests.enforce(request, auth.user, "session")
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
    ip = client_ip(request) or "?"
    _rate_limited(request, "basic", f"{ip}:{username[:64]}")
    row = db.scalar(select(AppPassword).where(AppPassword.key_hash == sha256_hex(secret)))
    if row is None or row.revoked_at is not None:
        raise _basic_unauthorized()
    user = db.get(User, row.user_id)
    if not guests.account_usable(user) or user.username != username:
        raise _basic_unauthorized()
    guests.enforce(request, user, "basic")
    if row.last_used_at is None:
        from lectern.security.alerts import on_credential_first_use

        on_credential_first_use(db, request, user, "app_password", row.label)
    row.last_used_at = utcnow()
    db.commit()
    request.state.app_password = row
    return user


OpdsUser = Annotated[User, Depends(opds_user)]


@dataclass
class LibraryAuth:
    """A /library/* caller: a browser session or an OPDS app password, and its device."""

    user: User
    device: Device


def library_auth(request: Request, db: DbDep) -> LibraryAuth:
    """Session cookie if present, else HTTP Basic with an app password (no browser prompt)."""
    found = load_session(db, request)
    if found is not None:
        session, user = found
        guests.enforce(request, user, "session")
        return LibraryAuth(user, Device.session(session.id))
    if request.headers.get("authorization", "").lower().startswith("basic "):
        user = opds_user(request, db)
        return LibraryAuth(user, Device.app_password(request.state.app_password))
    raise LoginRequired()


LibraryUser = Annotated[LibraryAuth, Depends(library_auth)]


# --- External alignment workers: Bearer worker credential ---------------------------------
#
# Accepted only by /api/worker/*. A worker key is not a user: no session, no role, no CSRF
# token. Sessions and app passwords are not accepted here either, so a browser cannot be
# tricked into claiming jobs and a worker key cannot read anything but its jobs' inputs.

_BEARER_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="Lectern workers"'}
WORKER_KEY_PREFIX = "lw_"


def _bearer_unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, "Worker credential required", headers=_BEARER_CHALLENGE
    )


def worker_credential(request: Request, db: DbDep) -> WorkerCredential:
    """Rate limits: *failed* checks are limited per client address (``worker_fail``, the
    ordinary per-minute limit), and an address over it is refused before any lookup, so keys
    cannot be guessed. A valid key gets its own, much larger bucket (``worker``), because one
    alignment legitimately issues many range requests for its inputs. ``/fail`` is exempt
    from that bucket: a worker that cannot report a failure leaves its lease to expire."""
    limiters = request.app.state.limiters
    ip_key = f"worker:{client_ip(request) or '?'}"
    if limiters["worker_fail"].exhausted(ip_key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")
    header = request.headers.get("authorization", "")
    secret = header[7:].strip() if header.lower().startswith("bearer ") else ""
    row = None
    if secret.startswith(WORKER_KEY_PREFIX) and len(secret) <= 128:
        row = db.scalar(
            select(WorkerCredential).where(WorkerCredential.key_hash == sha256_hex(secret))
        )
    if row is None or row.revoked_at is not None:
        limiters["worker_fail"].allow(ip_key)
        raise _bearer_unauthorized()
    if not request.url.path.endswith("/fail") and not limiters["worker"].allow(f"cred:{row.id}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")
    row.last_used_at = utcnow()
    db.commit()
    return row


WorkerAuth = Annotated[WorkerCredential, Depends(worker_credential)]


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
    ip = client_ip(request) or "?"
    _rate_limited(request, "kosync", f"{ip}:{username[:64]}")
    # KOReader sends md5(password) as x-auth-key; we store sha256 of that string.
    row = db.scalar(select(SyncKey).where(SyncKey.key_hash == sha256_hex(key.lower())))
    if row is None or row.revoked_at is not None:
        raise _sync_unauthorized()
    user = db.get(User, row.user_id)
    # KOReader pairing is not offered to guests: a guest's sync key (there should be none) is dead.
    if not guests.account_usable(user) or user.guest is not None or user.username != username:
        raise _sync_unauthorized()
    if row.last_used_at is None:
        from lectern.security.alerts import on_credential_first_use

        on_credential_first_use(db, request, user, "sync_key", row.label)
    row.last_used_at = utcnow()
    db.commit()
    return SyncAuth(user=user, key=row)


KoSync = Annotated[SyncAuth, Depends(kosync_auth)]
