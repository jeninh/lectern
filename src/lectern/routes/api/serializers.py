from __future__ import annotations

from datetime import datetime

from lectern.models import (
    AppPassword,
    BookFile,
    Passkey,
    ReadingProgress,
    Session,
    SyncKey,
    User,
    Work,
)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def user_json(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "display_name": u.display_name,
        "role": u.role.value,
        "totp_enabled": u.totp_enabled,
        "is_active": u.is_active,
        "created_at": iso(u.created_at),
        "last_login_at": iso(u.last_login_at),
        "download_cap_per_hour": u.download_cap_per_hour,
    }


def file_json(f: BookFile) -> dict:
    return {
        "id": f.id,
        "kind": f.kind.value,
        "rel_path": f.rel_path,
        "media_type": f.media_type,
        "size": f.size,
        "sha256": f.sha256,
        "partial_md5": f.partial_md5,
        "download_url": f"/library/files/{f.id}/download",
        "added_at": iso(f.added_at),
    }


def work_json(
    w: Work,
    files: list[BookFile] | None = None,
    audio: tuple[int, float] | None = None,
    position: dict | None = None,
) -> dict:
    """``audio`` is (track count, total seconds); ``position`` the caller's canonical
    position summary (web reader or audio player), see routes.api.core."""
    return {
        "id": w.id,
        "title": w.title,
        "author": w.author,
        "sort_title": w.sort_title,
        "sort_author": w.sort_author,
        "language": w.language,
        "isbn": w.isbn,
        "description": w.description,
        "publisher": w.publisher,
        "published": w.published,
        "dir_path": w.dir_path,
        "has_cover": w.has_cover,
        "cover_url": f"/library/works/{w.id}/cover.jpg" if w.has_cover else None,
        "cover_tint": w.cover_tint,
        "created_at": iso(w.created_at),
        "updated_at": iso(w.updated_at),
        "files": [file_json(f) for f in (files if files is not None else w.files)],
        "audio": {"tracks": audio[0], "duration_s": round(audio[1], 3)} if audio else None,
        "position": position,
    }


def progress_json(p: ReadingProgress, key: SyncKey | None = None) -> dict:
    return {
        "document": p.document,
        "progress": p.progress,
        "percentage": p.percentage,
        "device": p.device,
        "device_id": p.device_id,
        "timestamp": p.timestamp,
        "updated_at": iso(p.updated_at),
        "sync_key_id": p.sync_key_id,
        "sync_key_label": key.label if key else None,
    }


def sync_key_json(k: SyncKey) -> dict:
    return {
        "id": k.id,
        "label": k.label,
        "created_at": iso(k.created_at),
        "last_used_at": iso(k.last_used_at),
        "revoked_at": iso(k.revoked_at),
    }


def app_password_json(k: AppPassword) -> dict:
    return {
        "id": k.id,
        "label": k.label,
        "created_at": iso(k.created_at),
        "last_used_at": iso(k.last_used_at),
        "revoked_at": iso(k.revoked_at),
        "download_cap_per_hour": k.download_cap_per_hour,
    }


def passkey_json(p: Passkey) -> dict:
    return {
        "id": p.id,
        "label": p.label,
        "backed_up": p.backed_up,
        "transports": p.transports,
        "created_at": iso(p.created_at),
        "last_used_at": iso(p.last_used_at),
    }


def session_json(s: Session, current_id: int) -> dict:
    return {
        "id": s.id,
        "created_at": iso(s.created_at),
        "expires_at": iso(s.expires_at),
        "ip": s.ip,
        "user_agent": s.user_agent,
        "current": s.id == current_id,
    }
