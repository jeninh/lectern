"""Security page: password, TOTP, passkeys, sessions."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from lectern.models import KnownDevice, Passkey, Session, utcnow
from lectern.routes.api.serializers import iso, passkey_json, session_json
from lectern.security import audit, downloads, stepup
from lectern.security.deps import Auth, AuthCsrf, DbDep, SettingsDep
from lectern.security.passwords import hash_password, validate_password_strength, verify_password
from lectern.security.sessions import client_ip, revoke_all_sessions
from lectern.security.totp import new_totp_secret, provisioning_uri, verify_totp
from lectern.security.webauthn import registration_options, verify_registration

log = logging.getLogger("lectern.security")
router = APIRouter(prefix="/security")

WA_REG_COOKIE = "lectern_wa_reg"


@router.get("")
def overview(auth: Auth, db: DbDep, settings: SettingsDep) -> dict:
    passkeys = db.scalars(select(Passkey).where(Passkey.user_id == auth.user.id)).all()
    sessions = db.scalars(
        select(Session)
        .where(
            Session.user_id == auth.user.id,
            Session.revoked.is_(False),
            Session.expires_at > utcnow(),
        )
        .order_by(Session.created_at.desc())
    ).all()
    return {
        "passkeys": [passkey_json(p) for p in passkeys],
        "passkeys_possible": settings.is_https,
        "totp_enabled": auth.user.totp_enabled,
        "has_password": bool(auth.user.password_hash),
        "sessions": [session_json(s, auth.session.id) for s in sessions],
        "downloads": downloads.usage(db, auth.user, settings),
        "known_devices": [
            {
                "id": d.id,
                "label": d.label,
                "first_seen_at": iso(d.first_seen_at),
                "last_seen_at": iso(d.last_seen_at),
                "last_ip": d.last_ip,
            }
            for d in db.scalars(
                select(KnownDevice)
                .where(KnownDevice.user_id == auth.user.id)
                .order_by(KnownDevice.last_seen_at.desc())
            )
        ],
    }


@router.post("/known-devices/{device_id}/forget")
def forget_device(request: Request, auth: AuthCsrf, db: DbDep, device_id: int) -> dict:
    """Forget a recognised browser: its next sign-in alerts as a new device again."""
    row = db.get(KnownDevice, device_id)
    if row is None or row.user_id != auth.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    db.delete(row)
    audit.record(
        db, "device.forget", actor_user_id=auth.user.id, target=row.label, ip=client_ip(request)
    )
    db.commit()
    return {"ok": True}


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


@router.post("/password")
def change_password(request: Request, auth: AuthCsrf, db: DbDep, body: PasswordChange) -> dict:
    if not verify_password(auth.user.password_hash, body.current_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is wrong.")
    error = validate_password_strength(body.new_password)
    if error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, error)
    auth.user.password_hash = hash_password(body.new_password)
    revoke_all_sessions(db, auth.user.id)
    audit.record(db, "password.change", actor_user_id=auth.user.id, ip=client_ip(request))
    db.commit()
    return {"ok": True, "signed_out": True}


@router.post("/totp/begin")
def totp_begin(auth: AuthCsrf, db: DbDep) -> dict:
    if auth.user.totp_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "TOTP is already enabled.")
    secret = new_totp_secret()
    auth.user.totp_secret = secret
    db.commit()
    return {"secret": secret, "uri": provisioning_uri(secret, auth.user.username)}


class CodeIn(BaseModel):
    code: str = Field(max_length=16)


@router.post("/totp/confirm")
def totp_confirm(request: Request, auth: AuthCsrf, db: DbDep, body: CodeIn) -> dict:
    if auth.user.totp_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "TOTP is already enabled.")
    if not auth.user.totp_secret or not verify_totp(auth.user.totp_secret, body.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That code did not match.")
    auth.user.totp_enabled = True
    audit.record(db, "totp.enable", actor_user_id=auth.user.id, ip=client_ip(request))
    db.commit()
    return {"ok": True}


class PasswordIn(BaseModel):
    password: str = Field(max_length=256)


@router.post("/totp/disable")
def totp_disable(request: Request, auth: AuthCsrf, db: DbDep, body: PasswordIn) -> dict:
    if not verify_password(auth.user.password_hash, body.password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Password is wrong.")
    auth.user.totp_enabled = False
    auth.user.totp_secret = None
    audit.record(db, "totp.disable", actor_user_id=auth.user.id, ip=client_ip(request))
    db.commit()
    return {"ok": True}


@router.post("/step-up/options")
def step_up_options(request: Request, auth: AuthCsrf, db: DbDep, settings: SettingsDep):
    """A passkey challenge to confirm a sensitive action (``lectern.security.stepup``)."""
    return stepup.options(request, db, auth.user, settings)


@router.post("/passkeys/options")
def passkey_options(
    request: Request,
    auth: AuthCsrf,
    db: DbDep,
    settings: SettingsDep,
    body: stepup.StepUpIn | None = None,
):
    # A new passkey is itself a step-up factor, so a session alone cannot add one.
    stepup.require(
        request, db, auth.user, settings, body or stepup.StepUpIn(),
        failed_action="passkey.step_up_failed",
    )  # fmt: skip
    db.commit()  # a passkey step-up updated that passkey's counter
    existing = db.scalars(select(Passkey).where(Passkey.user_id == auth.user.id)).all()
    options_json, challenge = registration_options(settings, auth.user, existing)
    key = request.app.state.challenges.put(challenge, auth.user.id, "register")
    resp = JSONResponse({"publicKey": options_json})
    resp.set_cookie(
        WA_REG_COOKIE,
        key,
        max_age=300,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/api/security/passkeys",
    )
    return resp


@router.post("/passkeys/verify")
async def passkey_verify(request: Request, auth: AuthCsrf, db: DbDep, settings: SettingsDep):
    pending = request.app.state.challenges.pop(request.cookies.get(WA_REG_COOKIE), "register")
    body = await request.json()
    if pending is None or pending.user_id != auth.user.id or not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Challenge expired; try again")
    label = str(body.pop("label", "Passkey"))[:128] or "Passkey"
    try:
        result = verify_registration(settings, body, pending.challenge)
    except Exception as exc:  # noqa: BLE001
        log.info("passkey registration failed: %s", exc)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Passkey registration failed") from None
    row = Passkey(
        user_id=auth.user.id,
        credential_id=result.credential_id,
        public_key=result.credential_public_key,
        sign_count=result.sign_count,
        transports=",".join(body.get("response", {}).get("transports", []) or [])[:128] or None,
        label=label,
        backed_up=bool(result.credential_backed_up),
    )
    db.add(row)
    db.flush()
    audit.record(
        db, "passkey.register", actor_user_id=auth.user.id, target=label, ip=client_ip(request)
    )
    db.commit()
    resp = JSONResponse({"passkey": passkey_json(row)})
    resp.delete_cookie(WA_REG_COOKIE, path="/api/security/passkeys")
    return resp


@router.post("/passkeys/{passkey_id}/delete")
def passkey_delete(request: Request, auth: AuthCsrf, db: DbDep, passkey_id: int) -> dict:
    row = db.get(Passkey, passkey_id)
    if row is None or row.user_id != auth.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    db.delete(row)
    audit.record(
        db, "passkey.delete", actor_user_id=auth.user.id, target=row.label, ip=client_ip(request)
    )
    db.commit()
    return {"ok": True}


@router.post("/sessions/{session_id}/revoke")
def session_revoke(request: Request, auth: AuthCsrf, db: DbDep, session_id: int) -> dict:
    row = db.get(Session, session_id)
    if row is None or row.user_id != auth.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    row.revoked = True
    audit.record(
        db, "session.revoke", actor_user_id=auth.user.id, target=str(row.id), ip=client_ip(request)
    )
    db.commit()
    return {"ok": True, "current": row.id == auth.session.id}


@router.post("/sessions/revoke-others")
def sessions_revoke_others(request: Request, auth: AuthCsrf, db: DbDep) -> dict:
    n = 0
    for row in db.scalars(
        select(Session).where(Session.user_id == auth.user.id, Session.revoked.is_(False))
    ):
        if row.id != auth.session.id:
            row.revoked = True
            n += 1
    audit.record(
        db,
        "session.revoke",
        actor_user_id=auth.user.id,
        target="others",
        ip=client_ip(request),
        detail=str(n),
    )
    db.commit()
    return {"ok": True, "revoked": n}
