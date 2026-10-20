"""In-app notifications (security alerts and household events) for the signed-in user."""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from lectern.models import Notification, utcnow
from lectern.routes.api.serializers import iso
from lectern.security.deps import Auth, AuthCsrf, DbDep

router = APIRouter(prefix="/notifications")


def note_json(n: Notification) -> dict:
    return {
        "id": n.id,
        "kind": n.kind,
        "severity": n.severity,
        "title": n.title,
        "body": n.body,
        "data": n.data or {},
        "created_at": iso(n.created_at),
        "read_at": iso(n.read_at),
    }


@router.get("")
def list_notifications(auth: Auth, db: DbDep, limit: int = Query(default=30, ge=1, le=200)) -> dict:
    rows = db.scalars(
        select(Notification)
        .where(Notification.user_id == auth.user.id)
        .order_by(Notification.id.desc())
        .limit(limit)
    )
    unread = db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == auth.user.id, Notification.read_at.is_(None))
    )
    return {"items": [note_json(n) for n in rows], "unread": unread or 0}


class ReadIn(BaseModel):
    ids: list[int] = Field(default_factory=list, max_length=500)
    all: bool = False


@router.post("/read")
def mark_read(auth: AuthCsrf, db: DbDep, body: ReadIn) -> dict:
    stmt = update(Notification).where(
        Notification.user_id == auth.user.id, Notification.read_at.is_(None)
    )
    if not body.all:
        stmt = stmt.where(Notification.id.in_(body.ids))
    db.execute(stmt.values(read_at=utcnow()))
    db.commit()
    return {"ok": True}
