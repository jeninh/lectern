"""AI keys for MCP clients (``lectern.ai_keys``; docs/mcp.md).

- ``GET  /api/ai-keys``: the caller's keys, the scopes they may grant, and the endpoint URL.
- ``POST /api/ai-keys {"label", "scopes"}``: create one; the secret is in this response only.
- ``GET  /api/ai-keys/{id}``: one key; polled by "Connect an AI app" to show the first use.
- ``POST /api/ai-keys/{id}/revoke``.

Session + CSRF like the rest of ``/api``: an AI key itself can never list, create, or revoke
keys (``/api`` does not accept it). Someone else's key id answers 404.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from lectern import ai_keys
from lectern.models import AIKey, utcnow
from lectern.routes.api.serializers import iso
from lectern.security import audit
from lectern.security.deps import Auth, AuthCsrf, DbDep, SettingsDep
from lectern.security.sessions import client_ip
from lectern.workmeta import NoControls

router = APIRouter(prefix="/ai-keys")

MAX_ACTIVE_KEYS = 50


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
        "endpoint": f"{settings.base_url}/mcp",
    }


class CreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Annotated[str, NoControls] = Field(min_length=1, max_length=128)
    scopes: list[Literal["upload", "read", "organize"]] | None = Field(
        default=None, min_length=1, max_length=3
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_key(
    request: Request, auth: AuthCsrf, db: DbDep, settings: SettingsDep, body: CreateIn
) -> dict:
    label = body.label.strip()
    if not label:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "label_required")
    scopes = list(dict.fromkeys(body.scopes or ai_keys.default_scopes(auth.user)))
    if set(scopes) - set(ai_keys.allowed_scopes(auth.user)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "readers_cannot_upload")
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
