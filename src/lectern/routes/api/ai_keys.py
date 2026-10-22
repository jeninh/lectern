"""AI keys for MCP clients (``lectern.ai_keys``; docs/mcp.md).

- ``GET  /api/ai-keys``: the caller's keys, the scopes they may grant, and the endpoint URL.
- ``POST /api/ai-keys {"label", "scopes", "password" | "passkey"}``: create one; the secret is
  in this response only. Scopes beyond the person's defaults need a step-up.
- ``GET  /api/ai-keys/{id}``: one key; polled by "Connect an AI app" to show the first use.
- ``POST /api/ai-keys/{id}/scopes {"scopes", "password" | "passkey"}``: change what a key
  may do. The secret is neither shown nor rotated, so the AI app keeps working; tools read the
  scopes again on every call, so the change applies to its next request. Removing scopes is
  immediate and always allowed. Adding any scope needs a step-up.
- ``POST /api/ai-keys/{id}/revoke``.

Step-up (``lectern.security.stepup``) means a fresh password or passkey in the same request,
so a stolen session alone cannot give an AI app more access: not by widening a key, not by
creating a wider one, and not by registering a passkey of its own to confirm with (that needs
a step-up too).

Session + CSRF like the rest of ``/api``: an AI key itself can never list, create, edit, or
revoke keys (``/api`` does not accept it). Someone else's key id answers 404. If SQLite is
busy (an AI tool filing a book holds the write lock), a write answers 503 ``busy`` with
``Retry-After`` instead of failing.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from lectern import ai_keys
from lectern.models import AIKey, utcnow
from lectern.routes.api.serializers import iso
from lectern.security import audit, stepup
from lectern.security.deps import Auth, AuthCsrf, DbDep, SettingsDep
from lectern.security.sessions import client_ip
from lectern.workmeta import NoControls

router = APIRouter(prefix="/ai-keys")

MAX_ACTIVE_KEYS = 50
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


def _commit(db) -> None:
    """Commit, or 503 ``busy`` while another request holds SQLite's write lock past its
    busy timeout (nothing was changed; the client can simply try again)."""
    try:
        db.commit()
    except OperationalError as exc:
        db.rollback()
        if "locked" not in str(exc) and "busy" not in str(exc):
            raise
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "busy", headers={"Retry-After": "2"}
        ) from None


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
        "step_up": stepup.methods(db, auth.user, settings),
        "endpoint": f"{settings.base_url}/mcp",
    }


class CreateIn(stepup.StepUpIn):
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
    scopes = ai_keys.ordered(body.scopes or ai_keys.default_scopes(auth.user))
    _check_allowed(auth, scopes)
    beyond = [s for s in scopes if s not in ai_keys.default_scopes(auth.user)]
    method = None
    if beyond:
        method = stepup.require(
            request, db, auth.user, settings, body, failed_action="ai_key.step_up_failed",
            target=label, detail=f"creating with {','.join(beyond)}",
        )  # fmt: skip
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
        detail=",".join(row.scopes) + (f"; confirmed with {method}" if method else ""),
    )
    _commit(db)
    return {"key": key_json(row), "secret": secret, "endpoint": f"{settings.base_url}/mcp"}


def _check_allowed(auth, scopes) -> None:
    if set(scopes) - set(ai_keys.allowed_scopes(auth.user)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "readers_cannot_upload")


class ScopesIn(stepup.StepUpIn):
    model_config = ConfigDict(extra="forbid")

    scopes: list[Scope] = Field(min_length=1, max_length=4)


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
    before = list(row.scopes or [])
    if after == before:
        return {"key": key_json(row), "changed": False}
    added = [s for s in after if s not in before]
    # Only what is added must be allowed: a person demoted to reader can still narrow a key
    # that kept upload/file from before (MCP refuses those to readers anyway).
    _check_allowed(auth, added)
    method = None
    if added:
        method = stepup.require(
            request, db, auth.user, settings, body, failed_action="ai_key.step_up_failed",
            target=row.label, detail=f"adding {','.join(added)}",
        )  # fmt: skip
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
    _commit(db)
    return {"key": key_json(row), "changed": True}


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
        _commit(db)
    return {"key": key_json(row)}
