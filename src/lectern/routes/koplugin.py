"""Routes the KOReader plugin calls (docs/koreader-plugin.md). No browser session here.

``POST /koplugin/bootstrap``  one-time setup secret from the zip -> the device's credentials
``POST /koplugin/pair``       username + 6-digit code from the web app -> the same
``GET  /koplugin/status``     HTTP Basic with the device's app password: who it is, its queue,
                              its shelf-sync settings, and the books to clean up
``POST /koplugin/hello``      device id + nonce -> proof that this is the device's Lectern, so the
                              plugin can trust the home Wi-Fi address (lectern.lan)
``PUT  /koplugin/annotations`` kosync headers (the device's sync key): one book's highlights and
                              notes on the device (lectern.annotations)
``PUT  /koplugin/report``     HTTP Basic with the device's app password: battery, versions, free
                              space, and the Lectern books on the device (lectern.device_report)

Failures say as little as possible (one error code each), and the audit log records why.
Secrets, codes, and issued credentials never reach the log stream or the audit log.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from lectern import access, annotations, device_queue, device_report, guests, koplugin, lan
from lectern.models import BookFile, DeviceFollow, FileKind, Shelf, SyncKey, User, Work, utcnow
from lectern.routes.api.serializers import iso
from lectern.security.deps import DbDep, KoSync, OpdsUser, SettingsDep
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
def bootstrap(request: Request, db: DbDep, settings: SettingsDep, body: BootstrapIn) -> dict:
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
    return {**result.payload, "lan_url": koplugin.home_url(settings)}


class PairIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=16)


@router.post("/pair", include_in_schema=False)
def pair(request: Request, db: DbDep, settings: SettingsDep, body: PairIn) -> dict:
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
    # Guests are never paired with a device: their account is not "active" here.
    active = user if guests.account_usable(user) and user.guest is None else None

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
    # The Tailscale address too, so a device paired over the home address keeps a fallback
    # that is not the home address (the plugin never uses that one unverified).
    remote = koplugin.default_server_url(settings)[0]
    return {**result.payload, "server_url": remote, "lan_url": koplugin.home_url(settings)}


class HelloIn(BaseModel):
    device_id: str = Field(min_length=2, max_length=12)
    nonce: str = Field(pattern=r"^[0-9a-f]{32,128}$")
    setup: bool = False


@router.post("/hello", include_in_schema=False)
def hello(request: Request, db: DbDep, body: HelloIn) -> dict:
    """Prove to a device that this is its Lectern before it sends a credential to the home
    Wi-Fi address (docs/home-network.md). Unauthenticated by design: the answer is an HMAC of
    the device's own nonce under a key only this server and the device have."""
    ip = client_ip(request)
    if not request.app.state.limiters["koplugin_hello"].allow(f"ip:{ip or '?'}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited")
    key = koplugin.hello_key(db, body.device_id, setup=body.setup)
    if key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown_device")
    return {"proof": lan.proof(key, body.nonce)}


def _file_json(f: BookFile) -> dict:
    return {
        "id": f.id,
        "media_type": f.media_type,
        "size": f.size,
        "name": f.rel_path.rsplit("/", 1)[-1],
        "download_url": f"/opds/files/{f.id}/download",
    }


@router.get("/status", include_in_schema=False)
def plugin_status(request: Request, user: OpdsUser, db: DbDep, settings: SettingsDep) -> dict:
    """What the plugin shows under Lectern → Connection status and Books sent here, and what
    its quiet automatic sync acts on: ``auto_download`` (download the queue's books by
    itself) and ``remove`` (works that left the followed shelf with clean-up on; the plugin
    deletes only copies it downloaded). Following a shelf tops up the queue first."""
    pw = request.state.app_password
    device_queue.sync_follow(db, pw.id)
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
        # The plugin keeps its home address in step with the server (null: LAN access is off).
        "lan_url": koplugin.home_url(settings),
        "last_sync_at": iso(key.last_used_at) if key else None,
        "queue": {"pending": total, "items": items},
        "auto_download": pw.auto_download,
        "follow": _follow_json(db, pw, user),
        "remove": device_queue.cleanup_work_ids(db, pw),
    }


def _follow_json(db, pw, user: User) -> dict | None:
    follow = db.get(DeviceFollow, pw.id)
    if follow is None:
        return None
    shelf = db.get(Shelf, follow.shelf_id)
    # The name only if the device's user may still see the shelf.
    name = shelf.name if shelf and access.shelf_permission(db, user, shelf) else None
    return {"shelf": name, "cleanup": follow.cleanup}


class ReportBookIn(BaseModel):
    """One Lectern book on the device: the work id of a file the plugin downloaded from
    Lectern, or the KOReader document digest of a book with a sidecar; its position 0..1."""

    # Bounded: the ids are later bound into SQLite queries (64-bit INTEGER).
    work_id: int | None = Field(default=None, ge=1, le=2**63 - 1)
    document: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,128}$")
    percentage: float | None = Field(default=None, ge=0, le=1)


class ReportIn(BaseModel):
    # Seconds since the plugin read these values (a report may wait in its outbox): the
    # server dates it, so the device's clock does not matter.
    age: int = Field(default=0, ge=0, le=10**9)
    battery: int | None = Field(default=None, ge=0, le=100)
    charging: bool | None = None
    free_bytes: int | None = Field(default=None, ge=0, le=2**53)
    plugin_version: str | None = Field(default=None, max_length=32)
    koreader_version: str | None = Field(default=None, max_length=64)
    # Missing means none (a Lua JSON encoder writes an empty list as an object).
    books: list[ReportBookIn] = Field(default_factory=list, max_length=device_report.MAX_BOOKS)


@router.put("/report", include_in_schema=False)
def put_report(request: Request, user: OpdsUser, db: DbDep, body: ReportIn) -> dict:
    """The device's state for the Devices dashboard (lectern.device_report): sent with the
    plugin's quiet sync. The answer says only whether it was kept (an older report than the
    stored one is not), never which books Lectern recognised."""
    pw = request.state.app_password
    kept = device_report.store(
        db,
        pw,
        age=body.age,
        battery=body.battery,
        charging=body.charging,
        free_bytes=body.free_bytes,
        plugin_version=body.plugin_version or None,
        koreader_version=body.koreader_version or None,
        books=[
            {"w": b.work_id, "d": b.document, "p": b.percentage}
            for b in body.books
            if b.work_id is not None or b.document
        ],
    )
    db.commit()
    return {"stored": kept}


class AnnotationIn(BaseModel):
    """One KOReader highlight, as the plugin reads it from the book's sidecar."""

    datetime: str = Field(min_length=1, max_length=32)
    datetime_updated: str | None = Field(default=None, max_length=32)
    text: str = Field(max_length=annotations.MAX_TEXT)
    note: str | None = Field(default=None, max_length=annotations.MAX_TEXT)
    chapter: str | None = Field(default=None, max_length=512)
    color: str | None = Field(default=None, max_length=32)
    drawer: str | None = Field(default=None, max_length=32)
    pos0: str = Field(min_length=1, max_length=4096)
    pos1: str | None = Field(default=None, max_length=4096)
    page: int | None = Field(default=None, ge=1, le=1_000_000)
    pageno: int | None = Field(default=None, ge=0, le=1_000_000)


class AnnotationsIn(BaseModel):
    document: str = Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")
    device: str = Field(default="", max_length=128)
    # Missing means none left (a Lua JSON encoder writes an empty list as an object).
    annotations: list[AnnotationIn] = Field(
        default_factory=list, max_length=annotations.MAX_PER_DOCUMENT
    )


@router.put("/annotations", include_in_schema=False)
def put_annotations(auth: KoSync, db: DbDep, settings: SettingsDep, body: AnnotationsIn) -> dict:
    """One device's highlights and notes of one book, all of them (lectern.annotations). Same
    credentials as progress sync. The answer only counts: it never says whether the book is in
    the library. Nothing about the highlights is logged."""
    items = [
        annotations.Incoming(
            created=a.datetime,
            updated=max(a.datetime_updated or a.datetime, a.datetime),
            text=a.text,
            note=a.note or None,
            chapter=a.chapter or None,
            color=a.color or None,
            drawer=a.drawer or None,
            pos0=a.pos0,
            pos1=a.pos1 or None,
            page=a.page,
            pageno=a.pageno,
        )
        for a in body.annotations
    ]
    result = annotations.sync_document(
        db, settings.library_dir, auth.user, auth.key.id, body.document, body.device, items
    )
    db.commit()
    return result
