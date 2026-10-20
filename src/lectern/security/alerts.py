"""Sign-in and new-device alerts.

Browsers get a long-lived random ``lectern_device`` cookie (only its SHA-256 is stored, in
``known_devices``). A sign-in that presents a cookie this user has seen before is a sign-in on a
known device; anything else is a new device, which alerts the user and the owner. KOReader and
OPDS credentials alert their user the first time a device uses them.
"""

from __future__ import annotations

import re

from fastapi import Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern import notify
from lectern.config import Settings
from lectern.models import KnownDevice, User, utcnow
from lectern.security import audit
from lectern.security.sessions import client_ip
from lectern.security.tokens import new_secret, sha256_hex

DEVICE_COOKIE = "lectern_device"
DEVICE_COOKIE_TTL = 400 * 24 * 60 * 60  # browsers cap cookie lifetime at 400 days


def describe_user_agent(ua: str | None) -> str:
    """'Firefox on macOS' style label; good enough to recognise your own devices."""
    ua = ua or ""
    browser = "Browser"
    for pattern, name in (
        (r"Edg/", "Edge"),
        (r"OPR/|Opera", "Opera"),
        (r"Firefox/|FxiOS", "Firefox"),
        (r"CriOS|Chrome/", "Chrome"),
        (r"Safari/", "Safari"),
        (r"KOReader", "KOReader"),
    ):
        if re.search(pattern, ua):
            browser = name
            break
    system = ""
    for pattern, name in (
        (r"iPhone", "iPhone"),
        (r"iPad", "iPad"),
        (r"Android", "Android"),
        (r"Kindle", "Kindle"),
        (r"Mac OS X|Macintosh", "macOS"),
        (r"Windows", "Windows"),
        (r"CrOS", "ChromeOS"),
        (r"Linux", "Linux"),
    ):
        if re.search(pattern, ua):
            system = name
            break
    return f"{browser} on {system}" if system else browser


def on_sign_in(db: Session, request: Request, user: User, method: str) -> str | None:
    """Record the sign-in's device and raise alerts. Returns a new device cookie to set, or
    None when the browser is already known. Caller commits."""
    ip = client_ip(request)
    label = describe_user_agent(request.headers.get("user-agent"))
    where = f" from {ip}" if ip else ""
    token = request.cookies.get(DEVICE_COOKIE)
    known = None
    if token and len(token) <= 128:
        known = db.scalar(
            select(KnownDevice).where(
                KnownDevice.token_hash == sha256_hex(token), KnownDevice.user_id == user.id
            )
        )
    if known is not None:
        known.last_seen_at = utcnow()
        known.last_ip = ip
        notify.notify(
            db,
            user,
            "login.new",
            f"New sign-in: {known.label or label}",
            f"Signed in with {method}{where}.",
            data={"ip": ip, "device": known.label, "method": method},
        )
        return None

    new_token = new_secret()
    db.add(KnownDevice(user_id=user.id, token_hash=sha256_hex(new_token), label=label, last_ip=ip))
    audit.record(db, "device.new", actor_user_id=user.id, target=label, ip=ip, detail=method)
    notify.notify_user_and_owners(
        db,
        user,
        "device.new",
        f"Sign-in from a new device: {label}",
        f"Signed in with {method}{where}. If this was not you, change your password and sign "
        "out other sessions on the Security page.",
        owner_title=f"{user.username} signed in from a new device: {label}",
        data={"ip": ip, "device": label, "method": method, "user": user.username},
    )
    return new_token


def remember_device_quietly(db: Session, request: Request, user: User) -> str:
    """Register this browser without alerting (first-run setup, invite sign-up)."""
    token = new_secret()
    db.add(
        KnownDevice(
            user_id=user.id,
            token_hash=sha256_hex(token),
            label=describe_user_agent(request.headers.get("user-agent")),
            last_ip=client_ip(request),
        )
    )
    return token


def set_device_cookie(response: Response, token: str | None, settings: Settings) -> None:
    if not token:
        return
    response.set_cookie(
        DEVICE_COOKIE,
        token,
        max_age=DEVICE_COOKIE_TTL,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def on_credential_first_use(
    db: Session, request: Request, user: User, kind: str, label: str
) -> None:
    """A KOReader sync key or OPDS app password was used for the first time."""
    ip = client_ip(request)
    what = "progress sync" if kind == "sync_key" else "catalogue (OPDS)"
    audit.record(db, "device.first_use", actor_user_id=user.id, target=label, ip=ip, detail=kind)
    notify.notify(
        db,
        user,
        "device.connected",
        f"{label} connected",
        f"First {what} request{f' from {ip}' if ip else ''}. Revoke it on the Devices page if "
        "you did not set this up.",
        data={"key": f"{kind}:{label}", "ip": ip},
    )
