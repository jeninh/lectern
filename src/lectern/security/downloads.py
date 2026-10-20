"""Hourly download caps and download-burst alerts.

Every whole-file transfer (EPUB download from the web or OPDS, an original audio track, the
web reader's copy of an EPUB) goes through :func:`admit`. It counts the user's and the device's
transfers in the last hour, refuses with ``429 download_cap`` once either cap is reached, and
raises a burst alert when many downloads happen in a short window.

Streaming and read-along text are metered with :func:`admit_once`: a track (or a work's spine
text) counts as one download per credential per hour, so a player's many range requests and a
reader's page turns are free once admitted. Streams count only after ``stream_meter_bytes``
were served for that track in the hour, so probing metadata or a few seconds of playback is
never charged.

Counting and recording happen under one process-wide lock, and the event row is inserted
*before* counting: on SQLite the insert takes the write lock, so two concurrent requests (in
this process or another) cannot both see room under the cap.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lectern import notify
from lectern.config import Settings
from lectern.models import AppPassword, DownloadEvent, User, utcnow
from lectern.security import audit

HOUR = timedelta(hours=1)
_lock = threading.Lock()


@dataclass
class Device:
    """Who is downloading: ``session:<id>`` or ``app_password:<id>`` plus a display label."""

    credential: str
    label: str
    cap_override: int | None = None

    @classmethod
    def session(cls, session_id: int, label: str = "Web browser") -> Device:
        return cls(f"session:{session_id}", label)

    @classmethod
    def app_password(cls, pw: AppPassword) -> Device:
        return cls(f"app_password:{pw.id}", pw.label, pw.download_cap_per_hour)


def user_cap(user: User, settings: Settings) -> int:
    return (
        user.download_cap_per_hour
        if user.download_cap_per_hour is not None
        else settings.download_cap_per_hour
    )


def device_cap(device: Device, settings: Settings) -> int:
    return (
        device.cap_override
        if device.cap_override is not None
        else settings.download_device_cap_per_hour
    )


def _count(db: Session, *where) -> int:
    return (
        db.scalar(
            select(func.count()).select_from(DownloadEvent).where(~DownloadEvent.blocked, *where)
        )
        or 0
    )


def usage(db: Session, user: User, settings: Settings) -> dict:
    """Downloads in the last hour against the user's cap, for the Security page."""
    since = utcnow() - HOUR
    return {
        "used": _count(db, DownloadEvent.user_id == user.id, DownloadEvent.at >= since),
        "cap": user_cap(user, settings),
        "device_cap": settings.download_device_cap_per_hour,
        "burst_count": settings.download_burst_count,
        "burst_window_s": settings.download_burst_window_seconds,
    }


def admit(
    db: Session,
    settings: Settings,
    user: User,
    device: Device,
    target: str,
    ip: str | None,
) -> None:
    """Record a download or raise 429. Commits either way."""
    with _lock:
        _admit(db, settings, user, device, target, ip)


def _admit(
    db: Session, settings: Settings, user: User, device: Device, target: str, ip: str | None
) -> None:
    now = utcnow()
    since = now - HOUR
    ucap, dcap = user_cap(user, settings), device_cap(device, settings)
    # Insert first, then count including it: the insert holds the write lock until commit.
    event = DownloadEvent(
        user_id=user.id, credential=device.credential, target=target, ip=ip, at=now
    )
    db.add(event)
    db.flush()
    n_user = _count(db, DownloadEvent.user_id == user.id, DownloadEvent.at >= since)
    n_dev = _count(db, DownloadEvent.credential == device.credential, DownloadEvent.at >= since)
    scope = None
    if ucap and n_user > ucap:
        scope, limit, where = "user", ucap, (DownloadEvent.user_id == user.id)
    elif dcap and n_dev > dcap:
        scope, limit, where = "device", dcap, (DownloadEvent.credential == device.credential)
    if scope is not None:
        event.blocked = True
        db.flush()
        oldest = db.scalar(
            select(func.min(DownloadEvent.at)).where(
                ~DownloadEvent.blocked, DownloadEvent.at >= since, where
            )
        )
        retry = max(1, int(((oldest or now) + HOUR - now).total_seconds()) + 1)
        who = "account" if scope == "user" else f"device {device.label}"
        audit.record(
            db,
            "download.blocked",
            actor_user_id=user.id,
            target=target,
            ip=ip,
            detail=f"{scope} cap {limit}/h reached ({device.label})",
        )
        notify.notify_user_and_owners(
            db,
            user,
            "download.cap",
            f"Download limit reached on your {who}",
            f"{limit} downloads in the last hour. More are blocked for about {retry // 60 + 1} "
            "min. The owner can change the limit in Admin.",
            owner_title=f"{user.username} hit the download limit ({who})",
            data={"key": f"{scope}:{device.credential}", "limit": limit, "scope": scope},
            dedupe=HOUR,
        )
        db.commit()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            {
                "detail": "download_cap",
                "message": f"Download limit of {limit} per hour reached for this {scope}.",
                "scope": scope,
                "limit": limit,
                "retry_after": retry,
            },
            headers={"Retry-After": str(retry)},
        )

    window = timedelta(seconds=settings.download_burst_window_seconds)
    if settings.download_burst_count:
        recent = _count(db, DownloadEvent.user_id == user.id, DownloadEvent.at >= now - window)
        if recent >= settings.download_burst_count and not notify.recently_sent(
            db, user.id, "download.burst", "burst", HOUR
        ):
            audit.record(
                db,
                "download.burst",
                actor_user_id=user.id,
                target=device.label,
                ip=ip,
                detail=f"{recent} downloads in {window.seconds // 60} min",
            )
            notify.notify_user_and_owners(
                db,
                user,
                "download.burst",
                f"{recent} downloads in {window.seconds // 60} minutes",
                f"Latest from {device.label}{f' at {ip}' if ip else ''}. If this was not you, "
                "revoke the device and change your password.",
                owner_title=f"{user.username}: {recent} downloads in {window.seconds // 60} min",
                data={"key": "burst", "count": recent, "device": device.label},
                dedupe=HOUR,
            )
    db.commit()


def counted_recently(db: Session, credential: str, target: str) -> bool:
    return bool(
        _count(
            db,
            DownloadEvent.credential == credential,
            DownloadEvent.target == target,
            DownloadEvent.at >= utcnow() - HOUR,
        )
    )


def admit_once(
    db: Session,
    settings: Settings,
    user: User,
    device: Device,
    target: str,
    ip: str | None,
    detail: str,
) -> bool:
    """Count ``target`` as one download per credential per hour (or raise 429). Returns True
    when this call counted it; then a ``download`` audit row with ``detail`` is written too."""
    with _lock:
        if counted_recently(db, device.credential, target):
            return False
        _admit(db, settings, user, device, target, ip)
        audit.record(db, "download", actor_user_id=user.id, target=target, ip=ip, detail=detail)
        db.commit()
        return True


# Bytes served per (credential, target) in the current hour. In memory on purpose: losing it on
# restart only means a stream is charged a little later; the admitted state is in the database.
_streamed: dict[tuple[str, str], tuple[float, int]] = {}


def stream_bytes(credential: str, target: str, n: int) -> int:
    """Add ``n`` bytes to this hour's tally for (credential, target); return the new total."""
    now = time.monotonic()
    with _lock:
        for k in [k for k, (t0, _) in _streamed.items() if now - t0 > 3600]:
            del _streamed[k]
        t0, total = _streamed.get((credential, target), (now, 0))
        total += n
        _streamed[(credential, target)] = (t0, total)
        return total


def meter_stream(
    db: Session,
    settings: Settings,
    user: User,
    device: Device,
    target: str,
    ip: str | None,
    nbytes: int,
) -> None:
    """Charge a stream once it has served ``stream_meter_bytes`` in the hour (or raise 429)."""
    if stream_bytes(device.credential, target, nbytes) >= settings.stream_meter_bytes:
        admit_once(db, settings, user, device, target, ip, "stream")
