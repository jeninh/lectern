"""Hourly download caps and download-burst alerts.

Every whole-file transfer (EPUB download from the web or OPDS, an original audio track, the
web reader's copy of an EPUB) goes through :func:`admit`. It counts the user's and the device's
transfers in the last hour, refuses with ``429 download_cap`` once either cap is reached, and
raises a burst alert when many downloads happen in a short window.

Streaming and read-along text are metered with :func:`admit_once`: an audiobook (all tracks
of one work) or a work's chapter text counts as one download per credential per hour, so a
player's many range requests, chapter skips, and a reader's page turns are free once admitted.
Streams count only after ``stream_meter_bytes`` were actually sent for that work in the hour
(see :class:`StreamMeter`), so probing, seeking, and open-ended ``bytes=N-`` requests that the
browser abandons after one block are never charged for bytes they did not receive. A credential
that was refused for a target gets ``429`` again without new audit rows or notifications.

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
_lock = threading.RLock()


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


def _over(db: Session, settings: Settings, user: User, device: Device, since, extra: int):
    """(scope, limit, where) of the first cap that ``extra`` more downloads would exceed."""
    ucap, dcap = user_cap(user, settings), device_cap(device, settings)
    n_user = _count(db, DownloadEvent.user_id == user.id, DownloadEvent.at >= since)
    n_dev = _count(db, DownloadEvent.credential == device.credential, DownloadEvent.at >= since)
    if ucap and n_user + extra > ucap:
        return "user", ucap, (DownloadEvent.user_id == user.id)
    if dcap and n_dev + extra > dcap:
        return "device", dcap, (DownloadEvent.credential == device.credential)
    return None


def _retry_after(db: Session, where, now) -> int:
    oldest = db.scalar(
        select(func.min(DownloadEvent.at)).where(
            ~DownloadEvent.blocked, DownloadEvent.at >= now - HOUR, where
        )
    )
    return max(1, int(((oldest or now) + HOUR - now).total_seconds()) + 1)


def _too_many(scope: str, limit: int, retry: int) -> HTTPException:
    return HTTPException(
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


def _admit(
    db: Session, settings: Settings, user: User, device: Device, target: str, ip: str | None
) -> None:
    now = utcnow()
    since = now - HOUR
    # Insert first, then count including it: the insert holds the write lock until commit.
    event = DownloadEvent(
        user_id=user.id, credential=device.credential, target=target, ip=ip, at=now
    )
    db.add(event)
    db.flush()
    over = _over(db, settings, user, device, since, 0)
    if over is not None:
        scope, limit, where = over
        event.blocked = True
        db.flush()
        retry = _retry_after(db, where, now)
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
        raise _too_many(scope, limit, retry)

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


def _recent(db: Session, credential: str, target: str, blocked: bool) -> bool:
    return bool(
        db.scalar(
            select(func.count())
            .select_from(DownloadEvent)
            .where(
                DownloadEvent.blocked.is_(blocked),
                DownloadEvent.credential == credential,
                DownloadEvent.target == target,
                DownloadEvent.at >= utcnow() - HOUR,
            )
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
    when this call counted it; then a ``download`` audit row with ``detail`` is written too.

    A credential already refused for ``target`` this hour is refused again without writing
    anything while it is still over a cap, so repeated range requests cannot flood the audit
    log or the notifications."""
    with _lock:
        if _recent(db, device.credential, target, blocked=False):
            return False
        if _recent(db, device.credential, target, blocked=True):
            now = utcnow()
            over = _over(db, settings, user, device, now - HOUR, 1)
            if over is not None:
                scope, limit, where = over
                raise _too_many(scope, limit, _retry_after(db, where, now))
        _admit(db, settings, user, device, target, ip)
        audit.record(db, "download", actor_user_id=user.id, target=target, ip=ip, detail=detail)
        db.commit()
        return True


# Per (credential, target): [hour start (monotonic), bytes sent, admitted]. In memory on
# purpose: losing it on restart only means a stream is charged a little later; the admitted
# state that matters for caps is in the database.
_streamed: dict[tuple[str, str], list] = {}


def _entry(key: tuple[str, str]) -> list:
    now = time.monotonic()
    for k in [k for k, (t0, _, _) in _streamed.items() if now - t0 > 3600]:
        del _streamed[k]
    return _streamed.setdefault(key, [now, 0, False])


class StreamMeter:
    """Charges one work's audio to a credential once ``stream_meter_bytes`` were actually sent.

    :meth:`gate` runs before a response starts: if this credential already crossed the threshold
    for the work without being admitted (it was refused), it re-checks the caps and raises 429.
    :meth:`sent` is called by the response's file iterator after each block went out, from the
    threadpool, with its own database session (the request's may be closed by then). Returning
    False stops the stream: the headers are gone, so a refusal mid-response ends the body early
    and the next request gets the 429.
    """

    def __init__(self, settings: Settings, user_id: int, device: Device, target: str, ip):
        self.settings, self.user_id, self.device = settings, user_id, device
        self.target, self.ip = target, ip
        self.key = (device.credential, target)

    def gate(self, db: Session, user: User) -> None:
        with _lock:
            _t0, total, admitted = _entry(self.key)
            if admitted or total < self.settings.stream_meter_bytes:
                return
            self._charge(db, user)

    def _charge(self, db: Session, user: User) -> None:
        # Caller holds _lock (re-entrant), so concurrent ranges cannot both charge.
        admit_once(db, self.settings, user, self.device, self.target, self.ip, "stream")
        _entry(self.key)[2] = True

    def sent(self, n: int) -> bool:
        from lectern.db import session_factory

        with _lock:
            entry = _entry(self.key)
            entry[1] += n
            if entry[2] or entry[1] < self.settings.stream_meter_bytes:
                return True
            try:
                with session_factory()() as db:
                    user = db.get(User, self.user_id)
                    if user is None:
                        return False
                    self._charge(db, user)
            except HTTPException:
                return False
            return True
