from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from lectern.models import AuditLog

log = logging.getLogger("lectern.audit")


def record(
    db: Session,
    action: str,
    *,
    actor_user_id: int | None = None,
    target: str | None = None,
    ip: str | None = None,
    detail: str | None = None,
) -> None:
    """Append an audit row. Caller commits. Also mirrored to the log stream."""
    db.add(
        AuditLog(action=action, actor_user_id=actor_user_id, target=target, ip=ip, detail=detail)
    )
    log.info(
        "audit action=%s actor=%s target=%s ip=%s %s",
        action,
        actor_user_id,
        target,
        ip,
        detail or "",
    )
