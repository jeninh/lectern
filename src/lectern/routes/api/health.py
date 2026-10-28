"""Owner-only Health page (docs/health.md). ``/healthz`` stays liveness-only."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from lectern import egress, health, push_alerts
from lectern.models import Role
from lectern.security import audit
from lectern.security.deps import (
    AuthContext,
    DbDep,
    SettingsDep,
    require_role,
    require_role_read,
)
from lectern.security.sessions import client_ip

router = APIRouter(prefix="/admin/health")
OwnerRead = Annotated[AuthContext, Depends(require_role_read(Role.OWNER))]
OwnerWrite = Annotated[AuthContext, Depends(require_role(Role.OWNER))]


def _view(db, settings, max_age_s: float) -> dict:
    stored = health.refresh(db, settings, max_age_s=max_age_s)
    return {
        "level": stored["level"],
        "checked_at": stored["checked_at"],
        "checks": stored["checks"],
        "since": {k: v.get("since") for k, v in (stored.get("state") or {}).items()},
        "workers": health.workers(db),
        "offline": egress.offline(),
    }


@router.get("")
def health_page(auth: OwnerRead, db: DbDep, settings: SettingsDep) -> dict:
    """Every check, as the maintenance thread last stored them (evaluated here only when that
    result is older than ``health.STALE_S``), so polling is cheap."""
    return _view(db, settings, health.STALE_S)


@router.post("/check")
def check_now(auth: OwnerWrite, db: DbDep, settings: SettingsDep) -> dict:
    return _view(db, settings, 0)


@router.get("/summary")
def health_summary(auth: OwnerRead, db: DbDep, settings: SettingsDep) -> dict:
    """The overall level for the top bar, from the last refresh (the first one runs here)."""
    s = health.summary(db)
    if s["level"] is None:
        stored = health.refresh(db, settings)
        s = {"level": stored["level"], "checked_at": stored["checked_at"]}
    return s


@router.post("/push-test")
def push_test(request: Request, auth: OwnerWrite, db: DbDep, settings: SettingsDep) -> dict:
    """Send one sample push alert now. Nothing is attempted while offline mode is on."""
    target, _problems = push_alerts.load(settings)
    if target is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "not_configured")
    if egress.offline():
        raise HTTPException(status.HTTP_409_CONFLICT, "offline")
    audit.record(db, "health.push_test", actor_user_id=auth.user.id, ip=client_ip(request))
    db.commit()
    return {**push_alerts.send_test(settings), "push": push_alerts.describe(settings)}
