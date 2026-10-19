"""Signed-in user's own settings: passkeys, TOTP, device credentials."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select

from lectern.models import AppPassword, Passkey, SyncKey, utcnow
from lectern.security import audit
from lectern.security.deps import Auth, AuthCsrf, DbDep, SettingsDep
from lectern.security.passwords import hash_password, validate_password_strength, verify_password
from lectern.security.sessions import client_ip, revoke_all_sessions
from lectern.security.tokens import new_secret, sha256_hex, sync_key_hash
from lectern.security.totp import new_totp_secret, provisioning_uri, verify_totp
from lectern.security.webauthn import registration_options, verify_registration
from lectern.templating import render

log = logging.getLogger("lectern.account")
router = APIRouter()

WA_REG_COOKIE = "lectern_wa_reg"


def _account_context(request: Request, auth, db, extra: dict | None = None):
    sync_keys = db.scalars(
        select(SyncKey).where(SyncKey.user_id == auth.user.id).order_by(SyncKey.created_at.desc())
    ).all()
    app_passwords = db.scalars(
        select(AppPassword)
        .where(AppPassword.user_id == auth.user.id)
        .order_by(AppPassword.created_at.desc())
    ).all()
    passkeys = db.scalars(select(Passkey).where(Passkey.user_id == auth.user.id)).all()
    settings = request.app.state.settings
    ctx = {
        "sync_keys": sync_keys,
        "app_passwords": app_passwords,
        "passkeys": passkeys,
        "passkeys_possible": settings.is_https,
        "sync_url": f"{settings.base_url}/",
        "opds_url": f"{settings.base_url}/opds",
    }
    if extra:
        ctx.update(extra)
    return ctx


@router.get("/", include_in_schema=False)
def home(request: Request, auth: Auth):
    request.state.auth = auth
    return RedirectResponse("/library", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/account", include_in_schema=False)
def account_page(request: Request, auth: Auth, db: DbDep, welcome: str = ""):
    request.state.auth = auth
    return render(
        request, "account.html", _account_context(request, auth, db, {"welcome": bool(welcome)})
    )


# --- Sync keys (KOReader) ---------------------------------------------------------------


@router.post("/account/sync-keys", include_in_schema=False)
def create_sync_key(
    request: Request,
    auth: AuthCsrf,
    db: DbDep,
    label: Annotated[str, Form(min_length=1, max_length=128)],
):
    request.state.auth = auth
    secret = new_secret(24)
    row = SyncKey(user_id=auth.user.id, label=label.strip(), key_hash=sync_key_hash(secret))
    db.add(row)
    audit.record(
        db, "sync_key.create", actor_user_id=auth.user.id, target=label, ip=client_ip(request)
    )
    db.commit()
    return render(
        request,
        "account.html",
        _account_context(request, auth, db, {"new_sync_key": secret, "new_sync_label": label}),
    )


@router.post("/account/sync-keys/{key_id}/revoke", include_in_schema=False)
def revoke_sync_key(request: Request, auth: AuthCsrf, db: DbDep, key_id: int):
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
    return RedirectResponse("/account", status_code=status.HTTP_303_SEE_OTHER)


# --- App passwords (OPDS) ---------------------------------------------------------------


@router.post("/account/app-passwords", include_in_schema=False)
def create_app_password(
    request: Request,
    auth: AuthCsrf,
    db: DbDep,
    label: Annotated[str, Form(min_length=1, max_length=128)],
):
    request.state.auth = auth
    secret = new_secret(24)
    row = AppPassword(user_id=auth.user.id, label=label.strip(), key_hash=sha256_hex(secret))
    db.add(row)
    audit.record(
        db, "app_password.create", actor_user_id=auth.user.id, target=label, ip=client_ip(request)
    )
    db.commit()
    return render(
        request,
        "account.html",
        _account_context(request, auth, db, {"new_app_password": secret, "new_app_label": label}),
    )


@router.post("/account/app-passwords/{key_id}/revoke", include_in_schema=False)
def revoke_app_password(request: Request, auth: AuthCsrf, db: DbDep, key_id: int):
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
    return RedirectResponse("/account", status_code=status.HTTP_303_SEE_OTHER)


# --- Password + TOTP ---------------------------------------------------------------------


@router.post("/account/password", include_in_schema=False)
def change_password(
    request: Request,
    auth: AuthCsrf,
    db: DbDep,
    current_password: Annotated[str, Form(max_length=256)],
    new_password: Annotated[str, Form(max_length=256)],
    new_password_confirm: Annotated[str, Form(max_length=256)],
):
    request.state.auth = auth
    if not verify_password(auth.user.password_hash, current_password):
        return render(
            request,
            "account.html",
            _account_context(request, auth, db, {"error": "Current password is wrong."}),
            400,
        )
    error = validate_password_strength(new_password) or (
        None if new_password == new_password_confirm else "Passwords do not match."
    )
    if error:
        return render(
            request, "account.html", _account_context(request, auth, db, {"error": error}), 400
        )
    auth.user.password_hash = hash_password(new_password)
    revoke_all_sessions(db, auth.user.id)
    audit.record(db, "password.change", actor_user_id=auth.user.id, ip=client_ip(request))
    db.commit()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/account/totp/begin", include_in_schema=False)
def totp_begin(request: Request, auth: AuthCsrf, db: DbDep):
    request.state.auth = auth
    if auth.user.totp_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "TOTP already enabled")
    secret = new_totp_secret()
    auth.user.totp_secret = secret  # pending until confirmed
    db.commit()
    uri = provisioning_uri(secret, auth.user.username)
    return render(
        request,
        "account.html",
        _account_context(request, auth, db, {"totp_uri": uri, "totp_secret": secret}),
    )


@router.post("/account/totp/confirm", include_in_schema=False)
def totp_confirm(
    request: Request, auth: AuthCsrf, db: DbDep, code: Annotated[str, Form(max_length=16)]
):
    request.state.auth = auth
    if not auth.user.totp_secret or not verify_totp(auth.user.totp_secret, code):
        return render(
            request,
            "account.html",
            _account_context(request, auth, db, {"error": "That code did not match."}),
            400,
        )
    auth.user.totp_enabled = True
    audit.record(db, "totp.enable", actor_user_id=auth.user.id, ip=client_ip(request))
    db.commit()
    return RedirectResponse("/account", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/account/totp/disable", include_in_schema=False)
def totp_disable(
    request: Request, auth: AuthCsrf, db: DbDep, password: Annotated[str, Form(max_length=256)]
):
    request.state.auth = auth
    if not verify_password(auth.user.password_hash, password):
        return render(
            request,
            "account.html",
            _account_context(request, auth, db, {"error": "Password is wrong."}),
            400,
        )
    auth.user.totp_enabled = False
    auth.user.totp_secret = None
    audit.record(db, "totp.disable", actor_user_id=auth.user.id, ip=client_ip(request))
    db.commit()
    return RedirectResponse("/account", status_code=status.HTTP_303_SEE_OTHER)


# --- Passkeys ----------------------------------------------------------------------------


@router.post("/account/passkeys/options", include_in_schema=False)
def passkey_register_options(request: Request, auth: AuthCsrf, db: DbDep, settings: SettingsDep):
    existing = db.scalars(select(Passkey).where(Passkey.user_id == auth.user.id)).all()
    options_json, challenge = registration_options(settings, auth.user, existing)
    key = request.app.state.challenges.put(challenge, auth.user.id)
    resp = JSONResponse({"publicKey": options_json})
    resp.set_cookie(
        WA_REG_COOKIE,
        key,
        max_age=300,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/account",
    )
    return resp


@router.post("/account/passkeys/verify", include_in_schema=False)
async def passkey_register_verify(
    request: Request, auth: AuthCsrf, db: DbDep, settings: SettingsDep
):
    pending = request.app.state.challenges.pop(request.cookies.get(WA_REG_COOKIE))
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
    audit.record(
        db, "passkey.register", actor_user_id=auth.user.id, target=label, ip=client_ip(request)
    )
    db.commit()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(WA_REG_COOKIE, path="/account")
    return resp


@router.post("/account/passkeys/{passkey_id}/delete", include_in_schema=False)
def passkey_delete(request: Request, auth: AuthCsrf, db: DbDep, passkey_id: int):
    row = db.get(Passkey, passkey_id)
    if row is None or row.user_id != auth.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    db.delete(row)
    audit.record(
        db, "passkey.delete", actor_user_id=auth.user.id, target=row.label, ip=client_ip(request)
    )
    db.commit()
    return RedirectResponse("/account", status_code=status.HTTP_303_SEE_OTHER)
