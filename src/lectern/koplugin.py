"""The KOReader plugin (``lectern.koplugin``): per-device zips, bootstrap, and pairing codes.

See docs/koreader-plugin.md. A KOReader device is a sync key plus the app password issued with
it (``routes.api.devices``). For the plugin:

Zip download
    The device's own user (nobody else, not even the owner role) downloads a zip of the plugin
    with a fresh one-time bootstrap secret and the server address the e-reader should use,
    normally the server's Tailscale IP on plain HTTP port 8080 (MagicDNS is off on e-readers).
    Only the secret's SHA-256 is stored; a new download revokes the previous unused secret.

Bootstrap
    On first start the plugin trades the secret for the device's long-lived credentials:
    the KOReader sync ``userkey`` (md5 of a new sync secret, which is what KOReader sends) and
    a new OPDS app password. The secret is single-use (one conditional ``UPDATE``), valid for
    7 days, and revocable. Exchanging it rotates both credentials, so a leaked older value of
    either stops working. Because the rotation commits before the response is sent, a device
    that never received it (Wi-Fi dropped, a slow server hit the client's timeout) may present
    the same secret again for ``BOOTSTRAP_RETRY`` after its first use: each retry rotates again,
    so only the last holder's credentials work. A new zip, a pairing code, or revoking ends that
    window at once.

Pairing code
    Re-pairing after a reset: a 6-digit code shown in the web app, valid 5 minutes, single
    use, 5 attempts, and rate limited per address and per username. It rotates the device's
    credentials just like a bootstrap.

No secret, code, or credential is ever logged or written to the audit log.
"""

from __future__ import annotations

import io
import ipaddress
import logging
import re
import secrets
import socket
import zipfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from lectern import egress
from lectern.config import Settings
from lectern.models import (
    AppPassword,
    DeviceBootstrap,
    DevicePairingCode,
    SyncKey,
    User,
    utcnow,
)
from lectern.security import audit
from lectern.security.tokens import (
    constant_time_equal,
    md5_hex,
    new_secret,
    sha256_hex,
    sync_key_hash,
)

log = logging.getLogger("lectern.koplugin")

PLUGIN_DIR = Path(__file__).parent / "koreader" / "lectern.koplugin"
PLUGIN_FOLDER = "lectern.koplugin"
ZIP_NAME = "lectern.koplugin.zip"
DEVICE_FILE = "lectern_device.lua"
BOOTSTRAP_FILE = "lectern_bootstrap.txt"

BOOTSTRAP_TTL = timedelta(days=7)
BOOTSTRAP_RETRY = timedelta(minutes=15)
BOOTSTRAP_PREFIX = "lb_"
PAIRING_TTL = timedelta(minutes=5)
PAIRING_ATTEMPTS = 5
PAIRING_DIGITS = 6

# Tailscale hands out addresses from the CGNAT range; 100.100.100.100 is its resolver, which
# tailscaled routes through the tailnet interface in kernel TUN mode.
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_PROBE = egress.TAILSCALE_PROBE  # the one UDP connect offline mode allows


# --- server address ---------------------------------------------------------------------------


def detect_tailscale_ip() -> str | None:
    """This machine's Tailscale IPv4 address, if it has one.

    A connected UDP socket sends nothing; ``getsockname`` reveals the source address the
    kernel would use to reach Tailscale's resolver, which is the tailnet address when the
    route goes through ``tailscale0`` (the sidecar shares its network namespace with Lectern).
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(TAILSCALE_PROBE)
            ip = s.getsockname()[0]
    except OSError:
        return None
    try:
        return ip if ipaddress.ip_address(ip) in TAILSCALE_NET else None
    except ValueError:
        return None


def default_server_url(settings: Settings) -> tuple[str | None, str]:
    """(address for e-readers, where it came from: ``config``, ``tailscale``, or ``none``)."""
    if settings.device_url:
        return settings.device_url, "config"
    ip = detect_tailscale_ip()
    if ip:
        return f"http://{ip}:{settings.port}", "tailscale"
    return None, "none"


def home_url(settings: Settings) -> str | None:
    """The home Wi-Fi address devices may use besides the Tailscale one, if LAN access is on
    and its address is known (docs/home-network.md)."""
    return (settings.lan_url or None) if settings.lan_enabled else None


def normalize_server_url(value: str) -> str:
    """``scheme://host[:port]`` for http(s), no credentials, path, query, or fragment."""
    value = value.strip().rstrip("/")
    if not value or len(value) > 200 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("invalid_server_url")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("invalid_server_url")
    if parts.username or parts.password or parts.path or parts.query or parts.fragment:
        raise ValueError("invalid_server_url")
    try:
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError as exc:
        raise ValueError("invalid_server_url") from exc
    return f"{parts.scheme}://{parts.netloc}"


# --- credentials ------------------------------------------------------------------------------


def issue_credentials(key: SyncKey, pw: AppPassword) -> dict:
    """Rotate both credentials of a device; returns what the plugin stores. Caller commits."""
    sync_secret, opds_secret = new_secret(24), new_secret(24)
    key.key_hash = sync_key_hash(sync_secret)
    pw.key_hash = sha256_hex(opds_secret)
    # KOReader stores and sends md5(password) as its userkey; the raw secret is never needed.
    return {"userkey": md5_hex(sync_secret), "app_password": opds_secret}


def credentials_payload(user: User, key: SyncKey, pw: AppPassword) -> dict:
    return {
        "username": user.username,
        "device_id": f"k{key.id}",
        "label": key.label,
        **issue_credentials(key, pw),
    }


def device_parts(db: Session, key: SyncKey) -> tuple[SyncKey, AppPassword] | None:
    pw = db.scalar(select(AppPassword).where(AppPassword.sync_key_id == key.id))
    return (key, pw) if pw is not None else None


def _active(db: Session, key: SyncKey, pw: AppPassword) -> User | None:
    user = db.get(User, key.user_id)
    if user is None or not user.is_active or key.revoked_at or pw.revoked_at:
        return None
    return user


def revoke_setup(db: Session, key_id: int, *, bootstraps: bool = True) -> int:
    """Revoke a device's bootstrap secrets (unused ones, and used ones still in their retry
    window) and its live pairing codes. Returns how many secrets that could still be exchanged
    were revoked. Caller commits."""
    now = utcnow()
    n = 0
    if bootstraps:
        n = db.execute(
            update(DeviceBootstrap)
            .where(
                DeviceBootstrap.sync_key_id == key_id,
                DeviceBootstrap.revoked_at.is_(None),
                or_(
                    and_(DeviceBootstrap.used_at.is_(None), DeviceBootstrap.expires_at > now),
                    DeviceBootstrap.used_at > now - BOOTSTRAP_RETRY,
                ),
            )
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        ).rowcount
    db.execute(
        update(DevicePairingCode)
        .where(
            DevicePairingCode.sync_key_id == key_id,
            DevicePairingCode.used_at.is_(None),
            DevicePairingCode.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    return n or 0


# --- bootstrap --------------------------------------------------------------------------------


def latest_bootstrap(db: Session, key_id: int) -> DeviceBootstrap | None:
    return db.scalar(
        select(DeviceBootstrap)
        .where(DeviceBootstrap.sync_key_id == key_id)
        .order_by(DeviceBootstrap.id.desc())
        .limit(1)
    )


def bootstrap_state(row: DeviceBootstrap | None) -> str | None:
    if row is None:
        return None
    if row.used_at:
        return "used"
    if row.revoked_at:
        return "revoked"
    if row.expires_at <= utcnow():
        return "expired"
    return "pending"


def mint_bootstrap(db: Session, key: SyncKey, server_url: str) -> tuple[DeviceBootstrap, str]:
    """A new one-time secret for ``key``'s device; older unused ones are revoked."""
    revoke_setup(db, key.id)
    secret = BOOTSTRAP_PREFIX + new_secret(32)
    now = utcnow()
    row = DeviceBootstrap(
        user_id=key.user_id,
        sync_key_id=key.id,
        secret_hash=sha256_hex(secret),
        server_url=server_url,
        created_at=now,
        expires_at=now + BOOTSTRAP_TTL,
    )
    db.add(row)
    db.flush()
    return row, secret


@dataclass
class Exchange:
    payload: dict | None
    reason: str  # "ok" or why it failed (audit detail, never shown to the device)
    user: User | None = None
    label: str | None = None


def exchange_bootstrap(db: Session, secret: str, ip: str | None) -> Exchange:
    """Trade a bootstrap secret for the device's credentials, once. Caller commits."""
    if not secret.startswith(BOOTSTRAP_PREFIX) or len(secret) > 128:
        return Exchange(None, "malformed")
    row = db.scalar(
        select(DeviceBootstrap).where(DeviceBootstrap.secret_hash == sha256_hex(secret))
    )
    if row is None:
        return Exchange(None, "unknown")
    key = db.get(SyncKey, row.sync_key_id)
    parts = device_parts(db, key) if key else None
    user = db.get(User, row.user_id)
    label = key.label if key else None
    now = utcnow()
    cutoff = now - BOOTSTRAP_RETRY
    state = bootstrap_state(row)
    retry = state == "used" and row.revoked_at is None and row.used_at > cutoff
    if state != "pending" and not retry:
        return Exchange(None, state or "unknown", user, label)
    if parts is None or _active(db, *parts) is None:
        return Exchange(None, "device_revoked", user, label)
    claimed = db.execute(
        update(DeviceBootstrap)
        .where(
            DeviceBootstrap.id == row.id,
            DeviceBootstrap.revoked_at.is_(None),
            or_(
                and_(DeviceBootstrap.used_at.is_(None), DeviceBootstrap.expires_at > now),
                DeviceBootstrap.used_at > cutoff,
            ),
        )
        # The retry window runs from the first use; a retry does not extend it.
        .values(used_at=func.coalesce(DeviceBootstrap.used_at, now), used_ip=ip)
        .execution_options(synchronize_session=False)
    ).rowcount
    if claimed != 1:
        return Exchange(None, "used", user, label)
    # Any pairing code shown for this device is moot now (this secret stays retryable).
    revoke_setup(db, parts[0].id, bootstraps=False)
    payload = credentials_payload(user, *parts)
    payload["server_url"] = row.server_url
    return Exchange(payload, "retry" if retry else "ok", user, label)


def hello_key(db: Session, device_id: str, *, setup: bool) -> str | None:
    """What keys the home-address proof (lectern.lan.proof) for a device: the SHA-256 of its
    app password, or with ``setup`` of its newest setup secret that can still be exchanged.
    ``None`` for an unknown, revoked, or finished device or secret."""
    if not re.fullmatch(r"k[0-9]{1,11}", device_id):
        return None
    key = db.get(SyncKey, int(device_id[1:]))
    parts = device_parts(db, key) if key else None
    if parts is None or _active(db, *parts) is None:
        return None
    if not setup:
        return parts[1].key_hash
    row = latest_bootstrap(db, key.id)
    if row is None or row.revoked_at is not None:
        return None
    now = utcnow()
    if row.used_at is None:
        return row.secret_hash if row.expires_at > now else None
    return row.secret_hash if row.used_at > now - BOOTSTRAP_RETRY else None


# --- pairing codes ----------------------------------------------------------------------------


def _code_hash(salt: str, code: str) -> str:
    return sha256_hex(f"{salt}:{code}")


def new_pairing_code(db: Session, key: SyncKey) -> tuple[DevicePairingCode, str]:
    """A fresh code for ``key``'s device. A user has at most one live code: older ones die."""
    now = utcnow()
    db.execute(
        update(DevicePairingCode)
        .where(
            DevicePairingCode.user_id == key.user_id,
            DevicePairingCode.used_at.is_(None),
            DevicePairingCode.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    code = f"{secrets.randbelow(10**PAIRING_DIGITS):0{PAIRING_DIGITS}d}"
    salt = secrets.token_hex(16)
    row = DevicePairingCode(
        user_id=key.user_id,
        sync_key_id=key.id,
        salt=salt,
        code_hash=_code_hash(salt, code),
        created_at=now,
        expires_at=now + PAIRING_TTL,
        attempts=0,
    )
    db.add(row)
    db.flush()
    return row, code


def live_pairing_code(db: Session, user_id: int) -> DevicePairingCode | None:
    return db.scalar(
        select(DevicePairingCode)
        .where(
            DevicePairingCode.user_id == user_id,
            DevicePairingCode.used_at.is_(None),
            DevicePairingCode.revoked_at.is_(None),
            DevicePairingCode.expires_at > utcnow(),
            DevicePairingCode.attempts < PAIRING_ATTEMPTS,
        )
        .order_by(DevicePairingCode.id.desc())
        .limit(1)
    )


@dataclass
class Pairing:
    payload: dict | None
    reason: str  # ok, no_code, wrong_code, burned, device_revoked
    attempts_left: int = 0
    label: str | None = None


def redeem_pairing_code(db: Session, user: User, code: str) -> Pairing:
    """Check ``code`` against ``user``'s live code; every miss costs one of its 5 attempts.
    Caller commits (also on failure: the attempt must be counted)."""
    row = live_pairing_code(db, user.id)
    if row is None:
        return Pairing(None, "no_code")
    key = db.get(SyncKey, row.sync_key_id)
    label = key.label if key else None
    if not constant_time_equal(_code_hash(row.salt, code), row.code_hash):
        now = utcnow()
        db.execute(
            update(DevicePairingCode)
            .where(DevicePairingCode.id == row.id)
            .values(attempts=DevicePairingCode.attempts + 1)
            .execution_options(synchronize_session=False)
        )
        db.refresh(row)
        left = max(0, PAIRING_ATTEMPTS - row.attempts)
        if left == 0:
            row.revoked_at = now
            return Pairing(None, "burned", 0, label)
        return Pairing(None, "wrong_code", left, label)
    claimed = db.execute(
        update(DevicePairingCode)
        .where(
            DevicePairingCode.id == row.id,
            DevicePairingCode.used_at.is_(None),
            DevicePairingCode.revoked_at.is_(None),
            DevicePairingCode.attempts < PAIRING_ATTEMPTS,
            DevicePairingCode.expires_at > utcnow(),
        )
        .values(used_at=utcnow())
        .execution_options(synchronize_session=False)
    ).rowcount
    if claimed != 1:
        return Pairing(None, "no_code", 0, label)
    parts = device_parts(db, key) if key else None
    if parts is None or _active(db, *parts) is None:
        return Pairing(None, "device_revoked", 0, label)
    revoke_setup(db, parts[0].id)  # an unused zip for this device is superseded
    return Pairing(credentials_payload(user, *parts), "ok", 0, label)


# --- the zip ----------------------------------------------------------------------------------


def _lua_string(value: str) -> str:
    """A Lua string literal that cannot break out of its quotes, whatever ``value`` holds."""
    out = []
    for byte in value.encode("utf-8"):
        ch = chr(byte)
        if ch.isascii() and (ch.isalnum() or ch in " ._:/@+-()'"):
            out.append(ch)
        else:
            out.append(f"\\{byte:03d}")
    return '"' + "".join(out) + '"'


def device_config_lua(
    *,
    server_url: str,
    username: str,
    device_id: str,
    label: str,
    expires_at: str,
    lan_url: str | None = None,
) -> str:
    return (
        "-- Generated by Lectern for one device. Not secret: the one-time setup secret is in\n"
        f"-- {BOOTSTRAP_FILE}, which the plugin deletes after its first successful start.\n"
        "return {\n"
        f"    server_url = {_lua_string(server_url)},\n"
        + (f"    lan_url = {_lua_string(lan_url)},\n" if lan_url else "")
        + f"    username = {_lua_string(username)},\n"
        f"    device_id = {_lua_string(device_id)},\n"
        f"    label = {_lua_string(label)},\n"
        f"    setup_expires_at = {_lua_string(expires_at)},\n"
        "}\n"
    )


def build_zip(*, device_lua: str, secret: str) -> bytes:
    """``lectern.koplugin/`` with the plugin's sources, the device file, and the secret."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:

        def add(name: str, data: bytes) -> None:
            info = zipfile.ZipInfo(f"{PLUGIN_FOLDER}/{name}", date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)

        for path in sorted(PLUGIN_DIR.glob("*.lua")):
            if path.name not in (DEVICE_FILE,):
                add(path.name, path.read_bytes())
        add(DEVICE_FILE, device_lua.encode())
        add(BOOTSTRAP_FILE, (secret + "\n").encode())
    return buf.getvalue()


def record(db: Session, action: str, user: User | None, label: str | None, ip, detail=None):
    audit.record(
        db,
        action,
        actor_user_id=user.id if user else None,
        target=label,
        ip=ip,
        detail=detail,
    )
