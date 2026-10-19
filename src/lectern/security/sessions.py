"""Cookie sessions. The cookie carries a random token; the DB stores only its SHA-256."""

from __future__ import annotations

from datetime import timedelta

from fastapi import Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from lectern.config import Settings
from lectern.models import Session, User, utcnow
from lectern.security.tokens import new_secret, sha256_hex

COOKIE_NAME = "lectern_session"


def client_ip(request: Request) -> str | None:
    # Trust only the direct peer. A reverse proxy should be configured with
    # uvicorn --proxy-headers / forwarded-allow-ips so request.client reflects the real IP.
    return request.client.host if request.client else None


def create_session(db: DbSession, user: User, request: Request, settings: Settings) -> str:
    token = new_secret()
    row = Session(
        user_id=user.id,
        token_hash=sha256_hex(token),
        csrf_token=new_secret(),
        expires_at=utcnow() + timedelta(seconds=settings.session_ttl_seconds),
        ip=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:256],
    )
    db.add(row)
    user.last_login_at = utcnow()
    db.flush()
    return token


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        COOKIE_NAME, path="/", httponly=True, secure=settings.cookie_secure, samesite="lax"
    )


def load_session(db: DbSession, request: Request) -> tuple[Session, User] | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token or len(token) > 128:
        return None
    row = db.scalar(select(Session).where(Session.token_hash == sha256_hex(token)))
    if row is None or row.revoked or row.expires_at <= utcnow():
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    return row, user


def revoke_session(db: DbSession, request: Request) -> None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return
    row = db.scalar(select(Session).where(Session.token_hash == sha256_hex(token)))
    if row is not None:
        row.revoked = True


def revoke_all_sessions(db: DbSession, user_id: int) -> None:
    for row in db.scalars(
        select(Session).where(Session.user_id == user_id, Session.revoked.is_(False))
    ):
        row.revoked = True
