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


def _limited(request: Request, bucket: str, key: str) -> None:
    if not request.app.state.limiters[bucket].allow(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited")


class BootstrapIn(BaseModel):
    secret: str = Field(min_length=1, max_length=128)


@router.post("/bootstrap", include_in_schema=False)
def bootstrap(request: Request, db: DbDep, body: BootstrapIn) -> dict:
    ip = client_ip(request)
    _limited(request, "koplugin", f"ip:{ip or '?'}")
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
    koplugin.record(db, "koplugin.bootstrap", result.user, result.label, ip)
    db.commit()
    return result.payload


class PairIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=16)


@router.post("/pair", include_in_schema=False)
def pair(request: Request, db: DbDep, body: PairIn) -> dict:
    ip = client_ip(request)
    username = body.username.strip()
    # Two limits: per address (one device guessing for many people) and per username (many
    # addresses guessing for one person). The code's own 5 attempts come on top.
    _limited(request, "koplugin", f"ip:{ip or '?'}")
    _limited(request, "koplugin_pair_user", f"user:{username.lower()}")
    code = "".join(ch for ch in body.code if ch.isdigit())
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active or len(code) != koplugin.PAIRING_DIGITS:
        if user is not None and user.is_active:
            # A malformed code still costs an attempt: it was a guess.
            result = koplugin.redeem_pairing_code(db, user, "x")
            koplugin.record(db, "koplugin.pair_failed", user, result.label, ip, result.reason)
            db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_code")
    result = koplugin.redeem_pairing_code(db, user, code)
    if result.payload is None:
        koplugin.record(db, "koplugin.pair_failed", user, result.label, ip, result.reason)
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_code")
    koplugin.record(db, "koplugin.paired", user, result.label, ip)
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
