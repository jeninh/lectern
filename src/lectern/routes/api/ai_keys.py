"""AI keys for MCP clients (``lectern.ai_keys``; docs/mcp.md).

- ``GET  /api/ai-keys``: the caller's keys, the scopes they may grant, and the endpoint URL.
- ``POST /api/ai-keys {"label", "scopes"}``: create one; the secret is in this response only.
- ``GET  /api/ai-keys/{id}``: one key; polled by "Connect an AI app" to show the first use.
- ``POST /api/ai-keys/{id}/scopes {"scopes", "password" | "passkey"}``: change what a key
  may do. The secret is neither shown nor rotated, so the AI app keeps working; tools read the
  scopes again on every call, so the change applies to its next request. Removing scopes is
  immediate. Adding any scope is a sensitive action: like disabling two-factor codes, it needs
  the person's password in the same request, or a passkey assertion against a challenge from
  ``POST /api/ai-keys/step-up/options``. Without one it answers 403 ``step_up_required``.
- ``POST /api/ai-keys/{id}/revoke``.

Session + CSRF like the rest of ``/api``: an AI key itself can never list, create, edit, or
revoke keys (``/api`` does not accept it). Someone else's key id answers 404.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from lectern import ai_keys
from lectern.models import AIKey, Passkey, utcnow
from lectern.routes.api.serializers import iso
from lectern.security import audit
from lectern.security.deps import Auth, AuthCsrf, DbDep, SettingsDep
from lectern.security.passwords import verify_password
from lectern.security.sessions import client_ip
from lectern.security.webauthn import (
    authentication_options,
    credential_id_from_body,
    verify_authentication,
)
from lectern.workmeta import NoControls

log = logging.getLogger("lectern.security")
router = APIRouter(prefix="/ai-keys")

MAX_ACTIVE_KEYS = 50
STEP_UP_COOKIE = "lectern_wa_ai_key"
Scope = Literal["upload", "file", "read", "organize"]


def key_json(k: AIKey) -> dict:
    return {
        "id": k.id,
        "label": k.label,
        "prefix": k.prefix,
        "scopes": list(k.scopes or []),
        "created_at": iso(k.created_at),
        "last_used_at": iso(k.last_used_at),
        "last_ip": k.last_ip,
        "last_client": k.last_client,
        "revoked_at": iso(k.revoked_at),
    }


def _step_up_methods(db, auth, settings) -> list[str]:
    """How this person can confirm a sensitive action: ``passkey`` and/or ``password``."""
    has_passkey = settings.is_https and db.scalar(
        select(Passkey.id).where(Passkey.user_id == auth.user.id).limit(1)
    )
    return (["passkey"] if has_passkey else []) + (["password"] if auth.user.password_hash else [])


def _owned(db, auth, key_id: int) -> AIKey:
    row = db.get(AIKey, key_id)
    if row is None or row.user_id != auth.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return row


@router.get("")
def list_keys(auth: Auth, db: DbDep, settings: SettingsDep) -> dict:
    rows = db.scalars(
        select(AIKey).where(AIKey.user_id == auth.user.id).order_by(AIKey.created_at.desc())
    ).all()
    return {
        "keys": [key_json(k) for k in rows],
        "scopes": ai_keys.allowed_scopes(auth.user),
        "default_scopes": ai_keys.default_scopes(auth.user),
        "step_up": _step_up_methods(db, auth, settings),
        "endpoint": f"{settings.base_url}/mcp",
    }


class CreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Annotated[str, NoControls] = Field(min_length=1, max_length=128)
    scopes: list[Scope] | None = Field(default=None, min_length=1, max_length=4)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_key(
    request: Request, auth: AuthCsrf, db: DbDep, settings: SettingsDep, body: CreateIn
) -> dict:
    label = body.label.strip()
    if not label:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "label_required")
    scopes = list(dict.fromkeys(body.scopes or ai_keys.default_scopes(auth.user)))
    _check_allowed(auth, scopes)
    active = db.scalars(
        select(AIKey.id).where(AIKey.user_id == auth.user.id, AIKey.revoked_at.is_(None))
    ).all()
    if len(active) >= MAX_ACTIVE_KEYS:
        raise HTTPException(status.HTTP_409_CONFLICT, "too_many_keys")
    row, secret = ai_keys.new_key(auth.user, label, scopes)
    db.add(row)
    db.flush()
    audit.record(
        db,
        "ai_key.create",
        actor_user_id=auth.user.id,
        target=label,
        ip=client_ip(request),
        detail=",".join(row.scopes),
    )
    db.commit()
    return {"key": key_json(row), "secret": secret, "endpoint": f"{settings.base_url}/mcp"}


def _check_allowed(auth, scopes) -> None:
    if set(scopes) - set(ai_keys.allowed_scopes(auth.user)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "readers_cannot_upload")


@router.post("/step-up/options")
def step_up_options(request: Request, auth: AuthCsrf, db: DbDep, settings: SettingsDep):
    """A passkey challenge for adding scopes to a key (single use, five minutes)."""
    if "passkey" not in _step_up_methods(db, auth, settings):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no_passkey")
    mine = db.scalars(select(Passkey).where(Passkey.user_id == auth.user.id)).all()
    options_json, challenge = authentication_options(settings, list(mine))
    resp = JSONResponse({"publicKey": options_json})
    resp.set_cookie(
        STEP_UP_COOKIE,
        request.app.state.challenges.put(challenge, auth.user.id),
        max_age=300,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/api/ai-keys",
    )
    return resp


class ScopesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scopes: list[Scope] = Field(min_length=1, max_length=4)
    # Step-up for adding scopes: one of these, fresh in this request.
    password: str | None = Field(default=None, max_length=256)
    passkey: dict | None = None


def _step_up(request: Request, auth, db, settings, body: ScopesIn) -> str | None:
    """The method that confirmed it is the person (``password``/``passkey``), or None."""
    if body.passkey is not None:
        pending = request.app.state.challenges.pop(request.cookies.get(STEP_UP_COOKIE))
        if pending is None or pending.user_id != auth.user.id:
            return None
        cred_id = credential_id_from_body(body.passkey)
        passkey = cred_id and db.scalar(
            select(Passkey).where(Passkey.credential_id == cred_id, Passkey.user_id == auth.user.id)
        )
        if not passkey:
            return None
        try:
            result = verify_authentication(settings, body.passkey, pending.challenge, passkey)
        except Exception as exc:  # noqa: BLE001 - the library raises many subclasses
            log.info("AI key step-up passkey failed: %s", exc)
            return None
        passkey.sign_count = result.new_sign_count
        passkey.last_used_at = utcnow()
        return "passkey"
    if body.password and verify_password(auth.user.password_hash, body.password):
        return "password"
    return None


@router.post("/{key_id}/scopes")
def change_scopes(
    request: Request,
    auth: AuthCsrf,
    db: DbDep,
    settings: SettingsDep,
    key_id: int,
    body: ScopesIn,
):
    row = _owned(db, auth, key_id)
    if row.revoked_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "revoked")
    after = ai_keys.ordered(body.scopes)
    _check_allowed(auth, after)
    before = list(row.scopes or [])
    if after == before:
        return {"key": key_json(row), "changed": False}
    added = [s for s in after if s not in before]
    method = None
    if added:
        if body.password is None and body.passkey is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                {"detail": "step_up_required", "methods": _step_up_methods(db, auth, settings)},
            )
        ip = client_ip(request)
        if not request.app.state.limiters["login"].allow(f"step_up:{auth.user.id}"):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")
        method = _step_up(request, auth, db, settings, body)
        if method is None:
            audit.record(
                db,
                "ai_key.step_up_failed",
                actor_user_id=auth.user.id,
                target=row.label,
                ip=ip,
                detail=f"adding {','.join(added)}",
            )
            db.commit()
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                {"detail": "step_up_failed", "methods": _step_up_methods(db, auth, settings)},
            )
    row.scopes = after
    audit.record(
        db,
        "ai_key.scopes_change",
        actor_user_id=auth.user.id,
        target=row.label,
        ip=client_ip(request),
        detail=(
            f"before: {','.join(before) or '-'}; after: {','.join(after)}"
            + (f"; confirmed with {method}" if method else "")
        ),
    )
    db.commit()
    resp = JSONResponse({"key": key_json(row), "changed": True})
    if body.passkey is not None:
        resp.delete_cookie(STEP_UP_COOKIE, path="/api/ai-keys")
    return resp


@router.get("/{key_id}")
def key_status(auth: Auth, db: DbDep, key_id: int) -> dict:
    row = _owned(db, auth, key_id)
    return {"key": key_json(row), "connected": row.last_used_at is not None}


@router.post("/{key_id}/revoke")
def revoke_key(request: Request, auth: AuthCsrf, db: DbDep, key_id: int) -> dict:
    row = _owned(db, auth, key_id)
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        audit.record(
            db, "ai_key.revoke", actor_user_id=auth.user.id, target=row.label, ip=client_ip(request)
        )
        db.commit()
    return {"key": key_json(row)}
