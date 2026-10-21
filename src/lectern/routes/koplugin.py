"""Routes the KOReader plugin calls (docs/koreader-plugin.md). No browser session here.

``POST /koplugin/bootstrap``  one-time setup secret from the zip -> the device's credentials
``POST /koplugin/pair``       username + 6-digit code from the web app -> the same
``GET  /koplugin/status``     HTTP Basic with the device's app password: who it is, its queue

Failures say as little as possible (one error code each), and the audit log records why.
Secrets, codes, and issued credentials never reach the log stream or the audit log.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from lectern import device_queue, koplugin
from lectern.models import BookFile, FileKind, SyncKey, User, Work, utcnow
from lectern.routes.api.serializers import iso
from lectern.security.deps import DbDep, OpdsUser
from lectern.security.sessions import client_ip

router = APIRouter(prefix="/koplugin")

QUEUE_LIMIT = 50


def _audit_refusal(request: Request, key: str) -> bool:
    """Audit a rate-limit refusal at most once a minute per key, so a flood stays visible in
    Activity without writing a row per request."""
    return request.app.state.limiters["koplugin_audit"].allow(key)


class BootstrapIn(BaseModel):
    secret: str = Field(min_length=1, max_length=128)


@router.post("/bootstrap", include_in_schema=False)
def bootstrap(request: Request, db: DbDep, body: BootstrapIn) -> dict:
    ip = client_ip(request)
    if not request.app.state.limiters["koplugin"].allow(f"ip:{ip or '?'}"):
        if _audit_refusal(request, f"bootstrap:{ip}"):
            koplugin.record(db, "koplugin.bootstrap_failed", None, None, ip, "rate_limited")
            db.commit()
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited")
    result = koplugin.exchange_bootstrap(db, body.secret.strip(), ip)
    if result.payload is None:
        db.rollback()
        koplugin.record(
            db, "koplugin.bootstrap_failed", result.user, result.label, ip, result.reason
        )
        db.commit()
        # Used, expired, revoked, or unknown: the plugin deletes its copy and asks for a
        # pairing code instead.
        raise HTTPException(status.HTTP_410_GONE, "setup_secret_invalid")
    koplugin.record(
        db,
        "koplugin.bootstrap",
        result.user,
        result.label,
        ip,
        "retry" if result.reason == "retry" else None,
    )
    db.commit()
    return result.payload


class PairIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=16)


@router.post("/pair", include_in_schema=False)
def pair(request: Request, db: DbDep, body: PairIn) -> dict:
    """Limits: per address (one device guessing for many people) counts every request; per
    username (many addresses guessing for one person) counts only wrong guesses at a live code
    and is checked without being charged, so junk requests, unknown names, and requests while
    no code is shown cannot lock a person out; a successful pairing clears it. Malformed codes can never match, so they cost the
    address bucket only, never one of the code's 5 attempts. Refusals are audited."""
    ip = client_ip(request)
    username = body.username.strip()
    limiters = request.app.state.limiters
    user_key = f"user:{username.lower()}"
    user = db.scalar(select(User).where(User.username == username))
    active = user if user is not None and user.is_active else None

    def refuse(
        reason: str, label: str | None = None, code: int = 401, detail: str = "invalid_code"
    ):
        if code == 429 and not _audit_refusal(request, f"pair:{reason}:{ip}:{user_key}"):
            raise HTTPException(code, detail)
        koplugin.record(
            db,
            "koplugin.pair_failed",
            active,
            label,
            ip,
            reason if active else f"{reason} (user {username[:64]!r})",
        )
        db.commit()
        raise HTTPException(code, detail)

    if not limiters["koplugin"].allow(f"ip:{ip or '?'}"):
        refuse("rate_limited_address", code=429, detail="rate_limited")
    if limiters["koplugin_pair_user"].exhausted(user_key):
        refuse("rate_limited_user", code=429, detail="rate_limited")
    code = "".join(ch for ch in body.code if ch.isdigit())
    if len(code) != koplugin.PAIRING_DIGITS:
        refuse("malformed")
    if active is None:
        refuse("unknown_user")
    result = koplugin.redeem_pairing_code(db, active, code)
    if result.payload is None:
        if result.reason in ("wrong_code", "burned"):
            # Only a real guess at a live code counts against the person.
            limiters["koplugin_pair_user"].allow(user_key)
        refuse(result.reason, result.label)
    limiters["koplugin_pair_user"].reset(user_key)
    koplugin.record(db, "koplugin.paired", active, result.label, ip)
    db.commit()
    return result.payload


def _file_json(f: BookFile) -> dict:
    return {
        "id": f.id,
        "media_type": f.media_type,
        "size": f.size,
        "name": f.rel_path.rsplit("/", 1)[-1],
        "download_url": f"/opds/files/{f.id}/download",
    }


@router.get("/status", include_in_schema=False)
def plugin_status(request: Request, user: OpdsUser, db: DbDep) -> dict:
    """What the plugin shows under Lectern → Connection status and Books sent here."""
    pw = request.state.app_password
    key = db.get(SyncKey, pw.sync_key_id) if pw.sync_key_id else None
    total = device_queue.pending_count(db, pw.id, user)
    rows = db.execute(
        device_queue.pending_query(pw.id, user).options(selectinload(Work.files)).limit(QUEUE_LIMIT)
    ).all()
    items = []
    for item, work in rows:
        files = sorted(
            (f for f in work.files if f.kind == FileKind.EBOOK),
            # EPUB first: KOReader reflows it; then PDF, then comics.
            key=lambda f: (f.media_type != "application/epub+zip", f.id),
        )
        items.append(
            {
                "item_id": item.id,
                "work_id": work.id,
                "title": work.title,
                "author": work.author,
                "queued_at": iso(item.queued_at),
                "files": [_file_json(f) for f in files],
            }
        )
    return {
        "username": user.username,
        "device": {"id": f"k{key.id}" if key else f"p{pw.id}", "label": pw.label},
        "server_time": iso(utcnow()),
        "last_sync_at": iso(key.last_used_at) if key else None,
        "queue": {"pending": total, "items": items},
    }
