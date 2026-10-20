"""Identity, library, continue-reading, and activity endpoints."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from lectern.access import visible_works
from lectern.models import (
    UPLOAD_CHUNK_MAX_BYTES,
    AudioTrack,
    AuditLog,
    BookFile,
    Notification,
    ProgressSource,
    ReadingProgress,
    Role,
    Sentence,
    ShelfItem,
    SyncKey,
    User,
    Work,
    WorkProgress,
)
from lectern.routes.api.serializers import iso, progress_json, user_json, work_json
from lectern.security.deps import Auth, AuthCsrf, DbDep, SettingsDep

router = APIRouter()


@router.get("/me")
def me(auth: Auth, settings: SettingsDep, db: DbDep) -> dict:
    counts = {
        "works": db.scalar(select(func.count()).select_from(Work).where(visible_works(auth.user)))
        or 0,
        "users": db.scalar(select(func.count()).select_from(User)) or 0,
        "unread_notifications": db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == auth.user.id, Notification.read_at.is_(None))
        )
        or 0,
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
        "upload": {
            "max_bytes": settings.upload_max_bytes,
            "chunk_bytes": UPLOAD_CHUNK_MAX_BYTES,
            "max_active": settings.upload_max_active,
        },
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


def _audio_summary(db, work_ids: list[int]) -> dict[int, tuple[int, float]]:
    if not work_ids:
        return {}
    rows = db.execute(
        select(AudioTrack.work_id, func.count(), func.sum(AudioTrack.duration_s))
        .where(AudioTrack.work_id.in_(work_ids))
        .group_by(AudioTrack.work_id)
    ).all()
    return {w: (n, float(d or 0.0)) for w, n, d in rows}


def _positions(
    db, user_id: int, audio: dict[int, tuple[int, float]], work_ids: list[int] | None = None
) -> dict[int, dict]:
    """The caller's canonical positions (``work_progress``) summarised for lists: how far,
    from which device and source, and when."""
    stmt = select(WorkProgress).where(WorkProgress.user_id == user_id)
    if work_ids is not None:
        stmt = stmt.where(WorkProgress.work_id.in_(work_ids))
    rows = db.scalars(stmt).all()
    if not rows:
        return {}
    counts = dict(
        db.execute(
            select(Sentence.work_id, func.count())
            .where(Sentence.work_id.in_([r.work_id for r in rows]))
            .group_by(Sentence.work_id)
        ).all()
    )
    out = {}
    for r in rows:
        n = counts.get(r.work_id, 0)
        fraction = None
        if n:
            fraction = min(1.0, r.sentence_n / n)
        elif r.source == ProgressSource.AUDIO and audio.get(r.work_id, (0, 0.0))[1]:
            try:
                fraction = min(1.0, float(r.raw) / audio[r.work_id][1])
            except ValueError:
                fraction = None
        out[r.work_id] = {
            "fraction": fraction,
            "source": r.source.value,
            "device": r.device,
            "received_at": iso(r.received_at),
            "audio_s": float(r.raw)
            if r.source == ProgressSource.AUDIO and _isfloat(r.raw)
            else None,
        }
    return out


def _isfloat(v: str) -> bool:
    try:
        float(v)
        return True
    except ValueError:
        return False


@router.get("/library")
def library(
    auth: Auth,
    db: DbDep,
    q: str = Query(default="", max_length=200),
    sort: str = Query(default="title", pattern="^(title|author|added)$"),
    shelf: int | None = None,
) -> dict:
    stmt = select(Work).options(selectinload(Work.files)).where(visible_works(auth.user))
    if shelf is not None:
        stmt = stmt.where(Work.id.in_(select(ShelfItem.work_id).where(ShelfItem.shelf_id == shelf)))
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
    audio = _audio_summary(db, [w.id for w in works])
    positions = _positions(db, auth.user.id, audio)
    return {
        "works": [
            _attach_progress(
                work_json(w, audio=audio.get(w.id), position=positions.get(w.id)), progress
            )
            for w in works
        ],
        "total": len(works),
    }


@router.get("/works/{work_id}")
def work_detail(auth: Auth, db: DbDep, work_id: int) -> dict:
    work = db.scalar(
        select(Work)
        .options(selectinload(Work.files))
        .where(Work.id == work_id, visible_works(auth.user))
    )
    if work is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    audio = _audio_summary(db, [work.id])
    positions = _positions(db, auth.user.id, audio, [work.id])
    return _attach_progress(
        work_json(work, audio=audio.get(work.id), position=positions.get(work.id)),
        _progress_by_doc(db, auth.user.id),
    )


@router.get("/continue")
def continue_reading(auth: Auth, db: DbDep, limit: int = Query(default=8, ge=1, le=50)) -> dict:
    """Works this user is reading or listening to, most recent activity first.

    Merges KOReader sync progress (per document hash) with the canonical position the web
    reader and audio player keep; each item says which one is newer (``latest``).
    """
    rows = db.execute(
        select(ReadingProgress, SyncKey, BookFile)
        .join(BookFile, BookFile.partial_md5 == ReadingProgress.document)
        .outerjoin(SyncKey, SyncKey.id == ReadingProgress.sync_key_id)
        .where(ReadingProgress.user_id == auth.user.id)
        .order_by(ReadingProgress.updated_at.desc())
        .limit(limit * 2)
    ).all()
    latest: dict[int, tuple[str, object]] = {}
    ko: dict[int, tuple] = {}
    for p, key, f in rows:
        if f.work_id in ko:
            continue
        ko[f.work_id] = (p, key, f)
        latest[f.work_id] = (iso(p.updated_at) or "", "koreader")
    canon = db.scalars(
        select(WorkProgress)
        .where(WorkProgress.user_id == auth.user.id)
        .order_by(WorkProgress.received_at.desc())
        .limit(limit * 2)
    ).all()
    for r in canon:
        at = iso(r.received_at) or ""
        if r.work_id not in latest or at > latest[r.work_id][0]:
            latest[r.work_id] = (at, "canonical")
    ordered = sorted(latest.items(), key=lambda kv: kv[1][0], reverse=True)
    ids = [w for w, _ in ordered]
    visible = set(db.scalars(select(Work.id).where(Work.id.in_(ids), visible_works(auth.user))))
    ids = [w for w in ids if w in visible][:limit]
    works = {
        w.id: w
        for w in db.scalars(select(Work).options(selectinload(Work.files)).where(Work.id.in_(ids)))
    }
    audio = _audio_summary(db, ids)
    positions = _positions(db, auth.user.id, audio, ids)
    items: list[dict] = []
    for wid in ids:
        wj = work_json(works[wid], audio=audio.get(wid), position=positions.get(wid))
        if wid in ko:
            p, key, f = ko[wid]
            wj["progress"] = progress_json(p, key)
            wj["progress_file_id"] = f.id
        else:
            wj["progress"] = None
        wj["latest"] = latest[wid][1]
        items.append(wj)
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
    "upload.complete",
    "upload.fail",
    "download.blocked",
    "download.burst",
    "device.new",
    "device.first_use",
    "invite.create",
    "invite.accept",
    "invite.revoke",
    "user.role",
    "listen.start",
    "work.merge",
    "device.forget",
    "device.download_cap",
    "user.download_cap",
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
    linkable = ("book.upload", "upload.complete")
    uploaded = {e["target"] for e in events if e["action"] in linkable and e["target"]}
    if uploaded:
        ids = dict(db.execute(select(Work.title, Work.id).where(Work.title.in_(uploaded))).all())
        for e in events:
            if e["action"] in linkable and e["target"] in ids:
                e["work_id"] = ids[e["target"]]
    # Listening is audited against "work:<id>"; show the title instead.
    listened = {
        int(e["target"][5:])
        for e in events
        if e["action"] == "listen.start"
        and (e["target"] or "").startswith("work:")
        and e["target"][5:].isdigit()
    }
    if listened:
        titles = dict(db.execute(select(Work.id, Work.title).where(Work.id.in_(listened))).all())
        for e in events:
            if e["action"] == "listen.start" and (e["target"] or "")[5:].isdigit():
                wid = int(e["target"][5:])
                if wid in titles:
                    e["work_id"], e["target"] = wid, titles[wid]

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
