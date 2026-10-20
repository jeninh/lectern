"""User notifications: in-app first, with a registry for other delivery channels.

Every alert is stored as a ``notifications`` row and shown in the web app (the bell in the top
bar, and the Security page). That is the only channel Lectern ships, because e-mail or webhooks
need configuration (an SMTP relay, a URL, a secret) that a household server does not have by
default, and an alert channel that silently fails is worse than none.

Extension point: register a :class:`Sink` at startup and it receives every notification after
the row is written, inside the same transaction (it must not commit). A sink should hand the
work to a queue or thread and return quickly; delivery failures are logged, never raised, so a
broken webhook cannot block a sign-in. For example, a webhook sink enabled by an environment
variable::

    class WebhookSink:
        def __init__(self, url: str) -> None: ...
        def deliver(self, db, note, user) -> None:
            queue.put({"user": user.username, "kind": note.kind, "title": note.title})

    notify.register_sink(WebhookSink(os.environ["LECTERN_ALERT_WEBHOOK_URL"]))
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern.models import Notification, Role, User, utcnow

log = logging.getLogger("lectern.notify")


class Sink(Protocol):
    def deliver(self, db: Session, note: Notification, user: User) -> None: ...


_SINKS: list[Sink] = []


def register_sink(sink: Sink) -> None:
    _SINKS.append(sink)


def unregister_sink(sink: Sink) -> None:
    if sink in _SINKS:
        _SINKS.remove(sink)


def notify(
    db: Session,
    user: User,
    kind: str,
    title: str,
    body: str = "",
    *,
    severity: str = "info",
    data: dict | None = None,
) -> Notification:
    """Store an alert for ``user`` and pass it to every registered sink. Caller commits."""
    note = Notification(
        user_id=user.id, kind=kind, severity=severity, title=title[:200], body=body, data=data
    )
    db.add(note)
    db.flush()
    for sink in list(_SINKS):
        try:
            sink.deliver(db, note, user)
        except Exception:  # noqa: BLE001 - a broken channel must not break the request
            log.exception("notification sink %r failed", sink)
    return note


def owners(db: Session) -> list[User]:
    return list(db.scalars(select(User).where(User.role == Role.OWNER, User.is_active.is_(True))))


def recently_sent(db: Session, user_id: int, kind: str, key: str, within: timedelta) -> bool:
    """True if ``user_id`` already got a ``kind`` alert with this dedupe ``key`` lately."""
    since = utcnow() - within
    rows = db.scalars(
        select(Notification.data).where(
            Notification.user_id == user_id,
            Notification.kind == kind,
            Notification.created_at >= since,
        )
    ).all()
    return any((d or {}).get("key") == key for d in rows)


def notify_user_and_owners(
    db: Session,
    user: User,
    kind: str,
    title: str,
    body: str = "",
    *,
    owner_title: str | None = None,
    severity: str = "warning",
    data: dict | None = None,
    dedupe: timedelta | None = None,
) -> None:
    """Alert ``user`` and every owner (once each). With ``dedupe``, skip recipients who already
    received this ``kind`` with the same ``data["key"]`` within that window."""
    recipients = [user] + [o for o in owners(db) if o.id != user.id]
    key = (data or {}).get("key", "")
    for r in recipients:
        if dedupe and key and recently_sent(db, r.id, kind, key, dedupe):
            continue
        t = title if r.id == user.id else (owner_title or title)
        notify(db, r, kind, t, body, severity=severity, data=data)
