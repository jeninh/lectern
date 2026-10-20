"""Identity, library, continue-reading, and activity endpoints."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from lectern.models import AuditLog, BookFile, ReadingProgress, Role, SyncKey, User, Work
from lectern.routes.api.serializers import iso, progress_json, user_json, work_json
from lectern.security.deps import Auth, AuthCsrf, DbDep, SettingsDep

router = APIRouter()


@router.get("/me")
def me(auth: Auth, settings: SettingsDep, db: DbDep) -> dict:
    counts = {
        "works": db.scalar(select(func.count()).select_from(Work)) or 0,
        "users": db.scalar(select(func.count()).select_from(User)) or 0,
    }
    return {
        "user": user_json(auth.user),
        "csrf_token": auth.session.csrf_token,
        "base_url": settings.base_url,
        "passkeys_possible": settings.is_https,
        "opds_url": f"{settings.base_url}/opds",
        "sync_url": f"{settings.base_url}/",
        "inbox_path": str(settings.inbox_path),
        "counts": counts,
        "preferences": _preferences(auth.user),
    }


def _preferences(user: User) -> dict:
    return {"show_technical_details": user.show_technical_details}


class PreferencesIn(BaseModel):
    show_technical_details: bool | None = None


@router.post("/me/preferences")
def update_preferences(auth: AuthCsrf, db: DbDep, body: PreferencesIn) -> dict:
    """Per-user display preferences. Only fields present in the body change."""
    if body.show_technical_details is not None:
        auth.user.show_technical_details = body.show_technical_details
    db.commit()
    return {"preferences": _preferences(auth.user)}


def _progress_by_doc(db, user_id: int) -> dict[str, tuple[ReadingProgress, SyncKey | None]]:
    rows = db.execute(
        select(ReadingProgress, SyncKey)
        .outerjoin(SyncKey, SyncKey.id == ReadingProgress.sync_key_id)
        .where(ReadingProgress.user_id == user_id)
    ).all()
    return {p.document: (p, k) for p, k in rows}


def _attach_progress(work: dict, progress: dict) -> dict:
    best = None
    for f in work["files"]:
        hit = progress.get(f["partial_md5"] or "")
        if hit:
            pj = progress_json(*hit)
            f["progress"] = pj
            if best is None or (pj["updated_at"] or "") > (best["updated_at"] or ""):
                best = pj
        else:
            f["progress"] = None
    work["progress"] = best
    return work


@router.get("/library")
def library(
    auth: Auth,
    db: DbDep,
    q: str = Query(default="", max_length=200),
    sort: str = Query(default="title", pattern="^(title|author|added)$"),
) -> dict:
    stmt = select(Work).options(selectinload(Work.files))
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Work.title.ilike(like), Work.author.ilike(like)))
    order = {
        "title": (Work.sort_title,),
        "author": (Work.sort_author, Work.sort_title),
        "added": (Work.created_at.desc(), Work.id.desc()),
    }[sort]
    works = db.scalars(stmt.order_by(*order).limit(2000)).all()
    progress = _progress_by_doc(db, auth.user.id)
    return {"works": [_attach_progress(work_json(w), progress) for w in works], "total": len(works)}


@router.get("/works/{work_id}")
def work_detail(auth: Auth, db: DbDep, work_id: int) -> dict:
    work = db.scalar(select(Work).options(selectinload(Work.files)).where(Work.id == work_id))
    if work is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return _attach_progress(work_json(work), _progress_by_doc(db, auth.user.id))


@router.get("/continue")
def continue_reading(auth: Auth, db: DbDep, limit: int = Query(default=8, ge=1, le=50)) -> dict:
    """Works with sync progress for this user, most recently synced first."""
    rows = db.execute(
        select(ReadingProgress, SyncKey, BookFile)
        .join(BookFile, BookFile.partial_md5 == ReadingProgress.document)
        .outerjoin(SyncKey, SyncKey.id == ReadingProgress.sync_key_id)
        .where(ReadingProgress.user_id == auth.user.id)
        .order_by(ReadingProgress.updated_at.desc())
        .limit(limit * 2)
    ).all()
    seen: set[int] = set()
    items: list[dict] = []
    for p, key, f in rows:
        if f.work_id in seen:
            continue
        seen.add(f.work_id)
        work = db.scalar(select(Work).options(selectinload(Work.files)).where(Work.id == f.work_id))
        wj = work_json(work)
        wj["progress"] = progress_json(p, key)
        wj["progress_file_id"] = f.id
        items.append(wj)
        if len(items) >= limit:
            break
    return {"items": items}


_ACTIVITY_ACTIONS = {
    "login.success",
    "login.failed",
    "login.locked_out",
    "logout",
    "download",
    "sync_key.create",
    "sync_key.revoke",
    "app_password.create",
    "app_password.revoke",
    "passkey.register",
    "passkey.delete",
    "totp.enable",
    "totp.disable",
    "password.change",
    "session.revoke",
    "user.create",
    "user.disable",
    "user.enable",
    "device.create",
    "device.revoke",
    "book.upload",
    "setup.owner_created",
}


@router.get("/activity")
def activity(
    auth: Auth,
    db: DbDep,
    since: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """Recent audit events plus sync events. Owners see everyone; others see their own."""
    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=UTC)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad since") from exc

    users = {u.id: u.username for u in db.scalars(select(User)).all()}
    audit_stmt = select(AuditLog).where(AuditLog.action.in_(_ACTIVITY_ACTIONS))
    if auth.user.role != Role.OWNER:
        audit_stmt = audit_stmt.where(
            or_(AuditLog.actor_user_id == auth.user.id, AuditLog.target == auth.user.username)
        )
    if since_dt:
        audit_stmt = audit_stmt.where(AuditLog.at > since_dt)
    events = [
        {
            "id": f"a{r.id}",
            "kind": "audit",
            "action": r.action,
            "at": iso(r.at),
            "actor": users.get(r.actor_user_id),
            "target": r.target,
            "detail": r.detail,
            "ip": r.ip,
        }
        for r in db.scalars(audit_stmt.order_by(AuditLog.id.desc()).limit(limit)).all()
    ]
    uploaded = {e["target"] for e in events if e["action"] == "book.upload" and e["target"]}
    if uploaded:
        ids = dict(db.execute(select(Work.title, Work.id).where(Work.title.in_(uploaded))).all())
        for e in events:
            if e["action"] == "book.upload" and e["target"] in ids:
                e["work_id"] = ids[e["target"]]

    sync_stmt = (
        select(ReadingProgress, SyncKey, BookFile, Work)
        .join(BookFile, BookFile.partial_md5 == ReadingProgress.document)
        .join(Work, Work.id == BookFile.work_id)
        .outerjoin(SyncKey, SyncKey.id == ReadingProgress.sync_key_id)
    )
    if auth.user.role != Role.OWNER:
        sync_stmt = sync_stmt.where(ReadingProgress.user_id == auth.user.id)
    if since_dt:
        sync_stmt = sync_stmt.where(ReadingProgress.updated_at > since_dt)
    for p, key, _f, work in db.execute(
        sync_stmt.order_by(ReadingProgress.updated_at.desc()).limit(limit)
    ).all():
        events.append(
            {
                "id": f"s{p.id}",
                "kind": "sync",
                "action": "sync.progress",
                "at": iso(p.updated_at),
                "actor": users.get(p.user_id),
                "target": work.title,
                "work_id": work.id,
                "device": p.device or (key.label if key else None),
                "sync_key_label": key.label if key else None,
                "percentage": p.percentage,
            }
        )
    events.sort(key=lambda e: e["at"] or "", reverse=True)
    now = iso(datetime.now(UTC))
    return {"events": events[:limit], "now": now}
