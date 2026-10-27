"""Per-device credentials: KOReader sync keys and OPDS app passwords.

A device is a sync key plus the app password issued alongside it (``AppPassword.sync_key_id``).
Credentials created on their own, including those from before pairing existed, appear as
single-purpose devices.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from lectern import device_queue, koplugin, lan
from lectern.access import shelf_permission
from lectern.models import (
    AppPassword,
    BookFile,
    DeviceBootstrap,
    DeviceFollow,
    ReadingProgress,
    Role,
    Shelf,
    SyncKey,
    User,
    Work,
    utcnow,
)
from lectern.routes.api.serializers import app_password_json, iso, sync_key_json
from lectern.security import audit
from lectern.security.deps import Auth, AuthCsrf, DbDep, SettingsDep
from lectern.security.sessions import client_ip
from lectern.security.tokens import new_secret, sha256_hex, sync_key_hash

router = APIRouter(prefix="/devices")


class LabelIn(BaseModel):
    label: str = Field(min_length=1, max_length=128)


def _latest(*values):
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _plugin_json(boot: DeviceBootstrap | None) -> dict | None:
    """State of the device's newest lectern.koplugin setup secret (never the secret)."""
    if boot is None:
        return None
    return {
        "setup_state": koplugin.bootstrap_state(boot),
        "server_url": boot.server_url,
        "downloaded_at": iso(boot.created_at),
        "expires_at": iso(boot.expires_at),
        "set_up_at": iso(boot.used_at),
    }


def _device_json(
    key: SyncKey | None, pw: AppPassword | None, boot: DeviceBootstrap | None = None
) -> dict:
    parts = [p for p in (key, pw) if p is not None]
    return {
        "id": f"k{key.id}" if key else f"p{pw.id}",  # type: ignore[union-attr]
        "label": parts[0].label,
        "created_at": iso(min(p.created_at for p in parts)),
        "last_used_at": iso(_latest(*(p.last_used_at for p in parts))),
        # A device is revoked only once every credential it holds is revoked.
        "revoked_at": iso(_latest(*(p.revoked_at for p in parts)))
        if all(p.revoked_at for p in parts)
        else None,
        "sync_key": sync_key_json(key) if key else None,
        "app_password": app_password_json(pw) if pw else None,
        # The plugin needs both credentials of one device.
        "koplugin": key is not None and pw is not None,
        "plugin": _plugin_json(boot),
    }


@router.get("")
def list_devices(auth: Auth, db: DbDep) -> dict:
    keys = db.scalars(
        select(SyncKey).where(SyncKey.user_id == auth.user.id).order_by(SyncKey.created_at.desc())
    ).all()
    pws = db.scalars(
        select(AppPassword)
        .where(AppPassword.user_id == auth.user.id)
        .order_by(AppPassword.created_at.desc())
    ).all()
    paired = {p.sync_key_id: p for p in pws if p.sync_key_id is not None}
    newest = (
        select(func.max(DeviceBootstrap.id))
        .where(DeviceBootstrap.user_id == auth.user.id)
        .group_by(DeviceBootstrap.sync_key_id)
    )
    boots = {
        b.sync_key_id: b
        for b in db.scalars(select(DeviceBootstrap).where(DeviceBootstrap.id.in_(newest)))
    }
    devices = [_device_json(k, paired.get(k.id), boots.get(k.id)) for k in keys]
    key_ids = {k.id for k in keys}
    devices += [_device_json(None, p) for p in pws if p.sync_key_id not in key_ids]
    devices.sort(key=lambda d: d["created_at"] or "", reverse=True)
    return {
        "devices": devices,
        "sync_keys": [sync_key_json(k) for k in keys],
        "app_passwords": [app_password_json(p) for p in pws],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_device(request: Request, auth: AuthCsrf, db: DbDep, body: LabelIn) -> dict:
    """Issue a KOReader sync key and an OPDS app password as one device. Secrets shown once."""
    label = body.label.strip()
    sync_secret, opds_secret = new_secret(24), new_secret(24)
    key = SyncKey(user_id=auth.user.id, label=label, key_hash=sync_key_hash(sync_secret))
    db.add(key)
    db.flush()
    pw = AppPassword(
        user_id=auth.user.id, label=label, key_hash=sha256_hex(opds_secret), sync_key_id=key.id
    )
    db.add(pw)
    db.flush()
    audit.record(
        db, "device.create", actor_user_id=auth.user.id, target=label, ip=client_ip(request)
    )
    db.commit()
    return {
        "device": _device_json(key, pw),
        "username": auth.user.username,
        "sync_secret": sync_secret,
        "opds_secret": opds_secret,
    }


@router.post("/{device_id}/revoke")
def revoke_device(request: Request, auth: AuthCsrf, db: DbDep, device_id: str) -> dict:
    """Revoke every credential a device holds (``k<sync key id>`` or ``p<app password id>``)."""
    kind, num = device_id[:1], device_id[1:]
    if kind not in ("k", "p") or not num.isdigit():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    key: SyncKey | None = None
    pw: AppPassword | None = None
    if kind == "k":
        key = db.get(SyncKey, int(num))
        if key is None or key.user_id != auth.user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        pw = db.scalar(select(AppPassword).where(AppPassword.sync_key_id == key.id))
    else:
        pw = db.get(AppPassword, int(num))
        if pw is None or pw.user_id != auth.user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        if pw.sync_key_id is not None:
            key = db.get(SyncKey, pw.sync_key_id)
    now = utcnow()
    changed = False
    for part in (key, pw):
        if part is not None and part.revoked_at is None:
            part.revoked_at = now
            changed = True
    if key is not None:
        koplugin.revoke_setup(db, key.id)
    if changed:
        audit.record(
            db,
            "device.revoke",
            actor_user_id=auth.user.id,
            target=(key or pw).label,  # type: ignore[union-attr]
            ip=client_ip(request),
        )
    db.commit()
    return {"device": _device_json(key, pw, koplugin.latest_bootstrap(db, key.id) if key else None)}


@router.post("/sync-keys", status_code=status.HTTP_201_CREATED)
def create_sync_key(request: Request, auth: AuthCsrf, db: DbDep, body: LabelIn) -> dict:
    secret = new_secret(24)
    row = SyncKey(user_id=auth.user.id, label=body.label.strip(), key_hash=sync_key_hash(secret))
    db.add(row)
    db.flush()
    audit.record(
        db, "sync_key.create", actor_user_id=auth.user.id, target=row.label, ip=client_ip(request)
    )
    db.commit()
    return {"sync_key": sync_key_json(row), "secret": secret, "username": auth.user.username}


@router.get("/sync-keys/{key_id}")
def sync_key_status(auth: Auth, db: DbDep, key_id: int) -> dict:
    """Polled by the Add Device flow to confirm the first sync from a new device."""
    row = db.get(SyncKey, key_id)
    if row is None or row.user_id != auth.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    latest = db.execute(
        select(ReadingProgress, Work)
        .join(BookFile, BookFile.partial_md5 == ReadingProgress.document)
        .join(Work, Work.id == BookFile.work_id)
        .where(ReadingProgress.sync_key_id == row.id)
        .order_by(ReadingProgress.updated_at.desc())
        .limit(1)
    ).first()
    first_sync = None
    if latest:
        p, work = latest
        first_sync = {
            "work_id": work.id,
            "title": work.title,
            "author": work.author,
            "percentage": p.percentage,
            "device": p.device,
            "at": iso(p.updated_at),
        }
    return {
        "sync_key": sync_key_json(row),
        "connected": row.last_used_at is not None,
        "latest_sync": first_sync,
    }


@router.post("/sync-keys/{key_id}/revoke")
def revoke_sync_key(request: Request, auth: AuthCsrf, db: DbDep, key_id: int) -> dict:
    row = db.get(SyncKey, key_id)
    if row is None or row.user_id != auth.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    koplugin.revoke_setup(db, row.id)
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
    return {"sync_key": sync_key_json(row)}


@router.post("/app-passwords", status_code=status.HTTP_201_CREATED)
def create_app_password(request: Request, auth: AuthCsrf, db: DbDep, body: LabelIn) -> dict:
    secret = new_secret(24)
    row = AppPassword(user_id=auth.user.id, label=body.label.strip(), key_hash=sha256_hex(secret))
    db.add(row)
    db.flush()
    audit.record(
        db,
        "app_password.create",
        actor_user_id=auth.user.id,
        target=row.label,
        ip=client_ip(request),
    )
    db.commit()
    return {
        "app_password": app_password_json(row),
        "secret": secret,
        "username": auth.user.username,
    }


@router.post("/app-passwords/{key_id}/revoke")
def revoke_app_password(request: Request, auth: AuthCsrf, db: DbDep, key_id: int) -> dict:
    row = db.get(AppPassword, key_id)
    if row is None or row.user_id != auth.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if row.sync_key_id is not None:
        koplugin.revoke_setup(db, row.sync_key_id)
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
    return {"app_password": app_password_json(row)}


class DeviceCapIn(BaseModel):
    # None = server default (LECTERN_DOWNLOAD_DEVICE_CAP_PER_HOUR); 0 = no cap for this device.
    cap: int | None = Field(default=None, ge=0, le=100000)


@router.post("/app-passwords/{pw_id}/download-cap")
def set_device_download_cap(
    request: Request,
    auth: AuthCsrf,
    db: DbDep,
    settings: SettingsDep,
    pw_id: int,
    body: DeviceCapIn,
) -> dict:
    """Hourly download cap for one OPDS device (its app password).

    Owners may set anything. Members and readers may only tighten their own device's cap:
    the new effective limit must not be higher than the current one (0 counts as unlimited).
    """
    row = db.get(AppPassword, pw_id)
    if row is None or row.user_id != auth.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if auth.user.role != Role.OWNER:

        def limit(cap: int | None) -> float:
            eff = settings.download_device_cap_per_hour if cap is None else cap
            return eff or float("inf")

        if limit(body.cap) > limit(row.download_cap_per_hour):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "only_owner_can_raise_cap")
    row.download_cap_per_hour = body.cap
    audit.record(
        db,
        "device.download_cap",
        actor_user_id=auth.user.id,
        target=row.label,
        ip=client_ip(request),
        detail="default" if body.cap is None else str(body.cap),
    )
    db.commit()
    return {"app_password": app_password_json(row)}


class ShelfSyncIn(BaseModel):
    shelf_id: int | None = None  # None: follow no shelf
    cleanup: bool = False
    auto_download: bool = False


@router.post("/app-passwords/{pw_id}/shelf-sync")
def set_shelf_sync(
    request: Request, auth: AuthCsrf, db: DbDep, pw_id: int, body: ShelfSyncIn
) -> dict:
    """Shelf sync for one of the caller's devices (docs/device-queues.md#following-a-shelf):
    the shelf it follows (any shelf the caller sees, nested or smart), whether books leaving
    it are cleaned up, and whether the KOReader plugin downloads queued books by itself. The
    queue is topped up at once."""
    row = db.get(AppPassword, pw_id)
    if row is None or row.user_id != auth.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if row.revoked_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "device_revoked")
    shelf = db.get(Shelf, body.shelf_id) if body.shelf_id is not None else None
    if body.shelf_id is not None and (
        shelf is None or shelf_permission(db, auth.user, shelf) is None
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "shelf_not_found")
    follow = db.get(DeviceFollow, row.id)
    if shelf is None:
        if follow is not None:
            db.delete(follow)
    elif follow is None:
        db.add(DeviceFollow(app_password_id=row.id, shelf_id=shelf.id, cleanup=body.cleanup))
    else:
        follow.shelf_id, follow.cleanup = shelf.id, body.cleanup
    row.auto_download = body.auto_download
    audit.record(
        db,
        "device.shelf_sync",
        actor_user_id=auth.user.id,
        target=row.label,
        ip=client_ip(request),
        detail=(f"shelf {shelf.name}" if shelf else "no shelf")
        + (", clean-up" if shelf and body.cleanup else "")
        + (", auto-download" if body.auto_download else ""),
    )
    db.commit()
    device_queue.sync_follow(db, row.id)
    db.refresh(row)
    return {"app_password": app_password_json(row)}


# --- KOReader plugin (docs/koreader-plugin.md) ----------------------------------------------


@router.get("/koplugin")
def koplugin_defaults(auth: Auth, settings: SettingsDep) -> dict:
    """The address a new zip would carry: LECTERN_DEVICE_URL, else the detected Tailscale IP."""
    url, source = koplugin.default_server_url(settings)
    return {
        "server_url": url,
        "source": source,
        "port": settings.port,
        # Also put into the zip: the plugin prefers it at home (docs/home-network.md).
        "lan_url": koplugin.home_url(settings),
    }


def _own_plugin_device(db, user: User, device_id: str) -> tuple[SyncKey, AppPassword]:
    """The caller's own device with both credentials. Nobody else's, not even for the owner
    role: the zip and the codes hand out that device's credentials."""
    kind, num = device_id[:1], device_id[1:]
    if kind not in ("k", "p") or not num.isdigit():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if kind == "k":
        key = db.get(SyncKey, int(num))
        pw = (
            db.scalar(select(AppPassword).where(AppPassword.sync_key_id == key.id)) if key else None
        )
    else:
        pw = db.get(AppPassword, int(num))
        key = db.get(SyncKey, pw.sync_key_id) if pw and pw.sync_key_id else None
    if key is None or pw is None or key.user_id != user.id or pw.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if key.revoked_at or pw.revoked_at:
        raise HTTPException(status.HTTP_409_CONFLICT, "device_revoked")
    return key, pw


class PluginZipIn(BaseModel):
    server_url: str | None = Field(default=None, max_length=200)


@router.post("/{device_id}/koplugin")
def download_koplugin(
    request: Request,
    auth: AuthCsrf,
    db: DbDep,
    settings: SettingsDep,
    device_id: str,
    body: PluginZipIn,
) -> Response:
    """lectern.koplugin.zip for one of the caller's devices, with a fresh one-time setup
    secret (valid 7 days). Any earlier unused secret of the device stops working."""
    key, _pw = _own_plugin_device(db, auth.user, device_id)
    raw = body.server_url or koplugin.default_server_url(settings)[0]
    if not raw:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "server_url_required")
    try:
        server_url = koplugin.normalize_server_url(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_server_url") from exc
    if koplugin.home_url(settings) and lan.is_home_address(server_url):
        # The zip's address is the plugin's unverified fallback; the home address is added
        # separately and used only after the plugin verified it (docs/home-network.md).
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "server_url_is_home")
    boot, secret = koplugin.mint_bootstrap(db, key, server_url)
    data = koplugin.build_zip(
        device_lua=koplugin.device_config_lua(
            server_url=server_url,
            username=auth.user.username,
            device_id=f"k{key.id}",
            label=key.label,
            expires_at=iso(boot.expires_at) or "",
            lan_url=koplugin.home_url(settings),
        ),
        secret=secret,
    )
    audit.record(
        db,
        "koplugin.download",
        actor_user_id=auth.user.id,
        target=key.label,
        ip=client_ip(request),
        detail=server_url,
    )
    db.commit()
    return Response(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{koplugin.ZIP_NAME}"'},
    )


@router.post("/{device_id}/koplugin/revoke")
def revoke_koplugin_setup(request: Request, auth: AuthCsrf, db: DbDep, device_id: str) -> dict:
    """Make the device's unused setup secret and pairing code useless. Credentials the
    plugin already holds keep working; revoke the device to stop those."""
    key, pw = _own_plugin_device(db, auth.user, device_id)
    n = koplugin.revoke_setup(db, key.id)
    audit.record(
        db,
        "koplugin.revoke",
        actor_user_id=auth.user.id,
        target=key.label,
        ip=client_ip(request),
        detail=str(n),
    )
    db.commit()
    return {"device": _device_json(key, pw, koplugin.latest_bootstrap(db, key.id))}


@router.post("/{device_id}/pairing-code", status_code=status.HTTP_201_CREATED)
def new_pairing_code(request: Request, auth: AuthCsrf, db: DbDep, device_id: str) -> dict:
    """A 6-digit code for Lectern → Pair in KOReader: 5 minutes, single use, 5 attempts.
    Showing a new code ends the previous one (one live code per person)."""
    key, _pw = _own_plugin_device(db, auth.user, device_id)
    if not request.app.state.limiters["koplugin_code"].allow(f"user:{auth.user.id}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited")
    row, code = koplugin.new_pairing_code(db, key)
    audit.record(
        db,
        "koplugin.pairing_code",
        actor_user_id=auth.user.id,
        target=key.label,
        ip=client_ip(request),
    )
    db.commit()
    return {
        "code": code,
        "expires_at": iso(row.expires_at),
        "attempts": koplugin.PAIRING_ATTEMPTS,
        "username": auth.user.username,
    }
