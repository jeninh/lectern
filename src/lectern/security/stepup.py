"""Step-up: a fresh password or passkey for a sensitive action, inside the same request.

A logged-in session alone (a stolen cookie, an unlocked browser, XSS) must not be enough to:

- give an AI key more scopes (``POST /api/ai-keys/{id}/scopes``),
- create an AI key with more than the default scopes (``POST /api/ai-keys``),
- register a passkey (``POST /api/security/passkeys/options``). Without this, a session
  holder could register their own passkey and then use it as the step-up for the others.

The action's JSON body carries ``password`` or ``passkey``: an assertion against a single-use
challenge from ``POST /api/security/step-up/options`` (cookie bound to this person, five
minutes, user verification required, only one of their own passkeys). Without either the
action answers 403 ``step_up_required`` with the ``methods`` this person has; a wrong one is
403 ``step_up_failed``, counted against the login rate limit and audited.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from lectern.config import Settings
from lectern.models import Passkey, User, utcnow
from lectern.security import audit
from lectern.security.passwords import verify_password
from lectern.security.sessions import client_ip
from lectern.security.webauthn import (
    authentication_options,
    credential_id_from_body,
    verify_authentication,
)

log = logging.getLogger("lectern.security")

COOKIE = "lectern_step_up"
PURPOSE = "step_up"


class StepUpIn(BaseModel):
    """Fields a sensitive action's body may carry to confirm it is the person."""

    password: str | None = Field(default=None, max_length=256)
    passkey: dict | None = None

    @property
    def offered(self) -> bool:
        return self.password is not None or self.passkey is not None


def commit_or_busy(db: Session) -> None:
    """Commit, or 503 ``busy`` with ``Retry-After`` while another request holds SQLite's write
    lock past its busy timeout (an AI tool filing a book). Nothing was changed; retry."""
    try:
        db.commit()
    except OperationalError as exc:
        db.rollback()
        if "locked" not in str(exc) and "busy" not in str(exc):
            raise
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "busy", headers={"Retry-After": "2"}
        ) from None


def methods(db: Session, user: User, settings: Settings) -> list[str]:
    """How this person can confirm: ``passkey`` and/or ``password``."""
    has_passkey = settings.is_https and db.scalar(
        select(Passkey.id).where(Passkey.user_id == user.id).limit(1)
    )
    return (["passkey"] if has_passkey else []) + (["password"] if user.password_hash else [])


def options(request: Request, db: Session, user: User, settings: Settings) -> JSONResponse:
    if "passkey" not in methods(db, user, settings):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no_passkey")
    mine = list(db.scalars(select(Passkey).where(Passkey.user_id == user.id)))
    options_json, challenge = authentication_options(settings, mine, require_uv=True)
    resp = JSONResponse({"publicKey": options_json})
    resp.set_cookie(
        COOKIE,
        request.app.state.challenges.put(challenge, user.id, PURPOSE),
        max_age=300,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/api",
    )
    return resp


def _confirmed(request, db, user, settings, proof: StepUpIn) -> str | None:
    if proof.passkey is not None:
        pending = request.app.state.challenges.pop(request.cookies.get(COOKIE), PURPOSE)
        if pending is None or pending.user_id != user.id:
            return None
        if "passkey" not in methods(db, user, settings):
            return None
        cred_id = credential_id_from_body(proof.passkey)
        passkey = cred_id and db.scalar(
            select(Passkey).where(Passkey.credential_id == cred_id, Passkey.user_id == user.id)
        )
        if not passkey:
            return None
        try:
            result = verify_authentication(
                settings, proof.passkey, pending.challenge, passkey, require_uv=True
            )
        except Exception as exc:  # noqa: BLE001 - the library raises many subclasses
            log.info("step-up passkey failed: %s", exc)
            return None
        passkey.sign_count = result.new_sign_count
        passkey.last_used_at = utcnow()
        return "passkey"
    if proof.password and verify_password(user.password_hash, proof.password):
        return "password"
    return None


def require(
    request: Request,
    db: Session,
    user: User,
    settings: Settings,
    proof: StepUpIn,
    *,
    failed_action: str,
    target: str | None = None,
    detail: str | None = None,
) -> str:
    """The method that confirmed it is the person, or raise 403 (``step_up_required`` /
    ``step_up_failed``). A failure is audited as ``failed_action`` and committed."""
    if not proof.offered:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {"detail": "step_up_required", "methods": methods(db, user, settings)},
        )
    if not request.app.state.limiters["login"].allow(f"step_up:{user.id}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")
    method = _confirmed(request, db, user, settings, proof)
    if method is None:
        db.rollback()
        audit.record(
            db, failed_action, actor_user_id=user.id, target=target, ip=client_ip(request),
            detail=detail,
        )  # fmt: skip
        commit_or_busy(db)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {"detail": "step_up_failed", "methods": methods(db, user, settings)},
        )
    return method
