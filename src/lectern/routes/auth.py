"""Login (passkey or password+TOTP) and logout."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select

from lectern.models import LoginAttempt, Passkey, User, utcnow
from lectern.security import audit
from lectern.security.deps import AuthOptional, DbDep, SettingsDep
from lectern.security.passwords import hash_password, needs_rehash, verify_password
from lectern.security.sessions import (
    clear_session_cookie,
    client_ip,
    create_session,
    revoke_session,
    set_session_cookie,
)
from lectern.security.totp import verify_totp
from lectern.security.webauthn import (
    authentication_options,
    credential_id_from_body,
    verify_authentication,
)
from lectern.setup_state import setup_completed
from lectern.templating import render

log = logging.getLogger("lectern.auth")
router = APIRouter()

WA_COOKIE = "lectern_wa"


def _safe_next(nxt: str | None) -> str:
    if not nxt or not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
        return "/"
    return nxt


def _rate_limit(request: Request, bucket: str = "login") -> None:
    ip = client_ip(request) or "?"
    if not request.app.state.limiters[bucket].allow(ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts; try again later")


def _locked_out(db, settings, username: str) -> bool:
    since = utcnow() - timedelta(seconds=settings.login_lockout_seconds)
    failures = db.scalar(
        select(func.count())
        .select_from(LoginAttempt)
        .where(
            LoginAttempt.subject == username,
            LoginAttempt.success.is_(False),
            LoginAttempt.at >= since,
        )
    )
    return (failures or 0) >= settings.login_max_failures


def _record_attempt(db, username: str, success: bool) -> None:
    db.add(LoginAttempt(subject=username, success=success))


@router.get("/login", include_in_schema=False)
def login_form(request: Request, db: DbDep, auth: AuthOptional, next: str = "/"):
    if not setup_completed(db):
        return render(request, "setup_pending.html")
    if auth is not None:
        return RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    return render(request, "login.html", {"next": _safe_next(next)})


@router.post("/login", include_in_schema=False)
def login_password(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    username: Annotated[str, Form(max_length=64)],
    password: Annotated[str, Form(max_length=256)],
    totp: Annotated[str, Form(max_length=16)] = "",
    next: Annotated[str, Form()] = "/",
):
    _rate_limit(request)
    username = username.strip().lower()
    ip = client_ip(request)
    generic = "Invalid username, password, or code."

    if _locked_out(db, settings, username):
        audit.record(db, "login.locked_out", target=username, ip=ip)
        db.commit()
        return render(
            request,
            "login.html",
            {"error": "Too many failed attempts. Try again later.", "next": next},
            status_code=429,
        )

    user = db.scalar(select(User).where(User.username == username))
    ok = verify_password(user.password_hash if user else None, password)
    if ok and user and user.is_active and user.totp_enabled:
        ok = bool(user.totp_secret) and verify_totp(user.totp_secret, totp)
    if not ok or user is None or not user.is_active:
        _record_attempt(db, username, False)
        audit.record(db, "login.failed", target=username, ip=ip, detail="password")
        db.commit()
        return render(request, "login.html", {"error": generic, "next": next}, status_code=401)

    if user.password_hash and needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    _record_attempt(db, username, True)
    token = create_session(db, user, request, settings)
    audit.record(db, "login.success", actor_user_id=user.id, ip=ip, detail="password")
    db.commit()
    request.app.state.limiters["login"].reset(ip or "?")
    response = RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, token, settings)
    return response


@router.post("/login/passkey/options", include_in_schema=False)
def passkey_login_options(request: Request, response: Response, settings: SettingsDep, db: DbDep):
    _rate_limit(request, "webauthn")
    if not setup_completed(db):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Setup incomplete")
    options_json, challenge = authentication_options(settings, None)
    key = request.app.state.challenges.put(challenge, None)
    resp = JSONResponse({"publicKey": options_json})
    resp.set_cookie(
        WA_COOKIE,
        key,
        max_age=300,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/login",
    )
    return resp


@router.post("/login/passkey/verify", include_in_schema=False)
async def passkey_login_verify(request: Request, settings: SettingsDep, db: DbDep):
    _rate_limit(request, "webauthn")
    pending = request.app.state.challenges.pop(request.cookies.get(WA_COOKIE))
    body = await request.json()
    ip = client_ip(request)
    if pending is None or not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Challenge expired; try again")
    cred_id = credential_id_from_body(body)
    passkey = (
        db.scalar(select(Passkey).where(Passkey.credential_id == cred_id)) if cred_id else None
    )
    if passkey is None:
        audit.record(db, "login.failed", ip=ip, detail="passkey unknown credential")
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown passkey")
    user = db.get(User, passkey.user_id)
    try:
        result = verify_authentication(settings, body, pending.challenge, passkey)
    except Exception as exc:  # noqa: BLE001 - library raises many subclasses
        log.info("passkey verification failed: %s", exc)
        audit.record(
            db, "login.failed", target=user.username if user else None, ip=ip, detail="passkey"
        )
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Passkey verification failed") from None
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account disabled")
    passkey.sign_count = result.new_sign_count
    passkey.last_used_at = utcnow()
    token = create_session(db, user, request, settings)
    audit.record(db, "login.success", actor_user_id=user.id, ip=ip, detail="passkey")
    db.commit()
    resp = JSONResponse({"ok": True, "next": _safe_next(request.query_params.get("next"))})
    set_session_cookie(resp, token, settings)
    resp.delete_cookie(WA_COOKIE, path="/login")
    return resp


@router.post("/logout", include_in_schema=False)
async def logout(request: Request, db: DbDep, settings: SettingsDep, auth: AuthOptional):
    # Logout is state-changing but low-risk; we still require the CSRF token when a session exists.
    if auth is not None:
        from lectern.security.csrf import verify_csrf

        await verify_csrf(request, settings, auth.session.csrf_token)
        revoke_session(db, request)
        audit.record(db, "logout", actor_user_id=auth.user.id, ip=client_ip(request))
        db.commit()
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    clear_session_cookie(response, settings)
    return response
