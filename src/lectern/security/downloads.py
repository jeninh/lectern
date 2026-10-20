"""Hourly download caps and download-burst alerts.

Every whole-file transfer (EPUB download from the web or OPDS, an original audio track, the
web reader's copy of an EPUB) goes through :func:`admit`. It counts the user's and the device's
transfers in the last hour, refuses with ``429 download_cap`` once either cap is reached, and
raises a burst alert when many downloads happen in a short window. Streaming audio for playback
is not a download and is not counted (a player issues many range requests).
"""

from __future__ import annotations

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
    now = utcnow()
    since = now - HOUR
    ucap, dcap = user_cap(user, settings), device_cap(device, settings)
    n_user = _count(db, DownloadEvent.user_id == user.id, DownloadEvent.at >= since)
    n_dev = _count(db, DownloadEvent.credential == device.credential, DownloadEvent.at >= since)
    scope = None
    if ucap and n_user >= ucap:
        scope, limit, where = "user", ucap, (DownloadEvent.user_id == user.id)
    elif dcap and n_dev >= dcap:
        scope, limit, where = "device", dcap, (DownloadEvent.credential == device.credential)
    if scope is not None:
        oldest = db.scalar(
            select(func.min(DownloadEvent.at)).where(
                ~DownloadEvent.blocked, DownloadEvent.at >= since, where
            )
        )
        retry = max(1, int(((oldest or now) + HOUR - now).total_seconds()) + 1)
        db.add(
            DownloadEvent(
                user_id=user.id, credential=device.credential, target=target, ip=ip, blocked=True
            )
        )
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

    db.add(DownloadEvent(user_id=user.id, credential=device.credential, target=target, ip=ip))
    db.flush()
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
