"""Offline downloads for the installed web app (docs/offline.md).

The phone keeps its own copy of a book: the EPUB and PDF, the audiobook (data-saver or full
quality), and the read-along data. These routes tell it what to fetch and later whether its
copy is still current. They never send book bytes themselves: every file comes from the
usual ``/library`` routes, so downloads count against the hourly caps and pass the same
visibility checks as reading and listening online.

- ``GET  /api/works/{id}/offline``: what a download of this work consists of, with sizes.
- ``POST /api/works/{id}/offline``: the same, recorded in the audit log as a download start.
- ``POST /api/offline/check``: for each downloaded work, ``ok``, ``changed`` (fetch it again),
  or ``gone`` (deleted, trashed, or no longer shared with you: the device deletes its copy).

Positions saved while offline are replayed with ``offline`` on the progress routes; see
:class:`OfflineReplay`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from lectern.access import visible_works
from lectern.ingest.formats import PDF
from lectern.ingest.sentences import primary_epub
from lectern.jobs import MEDIA_TYPE, find_variant
from lectern.models import (
    Alignment,
    AlignmentStatus,
    AudioTrack,
    FileKind,
    Sentence,
    Work,
    utcnow,
)
from lectern.progress import replay_wins
from lectern.routes.api.serializers import iso
from lectern.security import audit
from lectern.security.deps import Auth, AuthCsrf, DbDep, SettingsDep
from lectern.security.sessions import client_ip

router = APIRouter()


class OfflineReplay(BaseModel):
    """A position the device saved while offline and sends now.

    ``recorded_at`` and ``sent_at`` are the device's clock; ``base`` is the server time of the
    last position the device saw (wrote or accepted) for this book, ``None`` if it saw none.
    ``keep`` is set after the person chose this device's position in a conflict prompt.
    """

    recorded_at: AwareDatetime
    sent_at: AwareDatetime
    base: AwareDatetime | None = None
    keep: bool = False

    def judge(self, server_at: datetime | None) -> tuple[bool, datetime]:
        return replay_wins(server_at, self.base, self.recorded_at, self.sent_at, utcnow())


def conflict(server: dict | None, recorded: datetime) -> HTTPException:
    """``409 progress_conflict``: another device saved a newer position than the replay."""
    return HTTPException(
        status.HTTP_409_CONFLICT,
        {"detail": "progress_conflict", "server": server, "recorded_at": iso(recorded)},
    )


def _visible(db, user, work_id: int) -> Work:
    work = db.scalar(
        select(Work)
        .options(selectinload(Work.files))
        .where(Work.id == work_id, visible_works(user))
    )
    if work is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return work


def _tracks(db, work_id: int) -> list[AudioTrack]:
    return list(
        db.scalars(
            select(AudioTrack).where(AudioTrack.work_id == work_id).order_by(AudioTrack.position)
        )
    )


def revision(db, work: Work) -> str:
    """Changes whenever anything a downloaded copy holds changes: the metadata and cover, the
    book files, the audio tracks, or the alignment and sentence list."""
    al = db.scalar(select(Alignment).where(Alignment.work_id == work.id))
    sentences = db.execute(
        select(func.count(Sentence.id), func.max(Sentence.id)).where(Sentence.work_id == work.id)
    ).one()
    payload = [
        work.version,
        iso(work.updated_at),
        work.primary_file_id,
        sorted((f.id, f.sha256) for f in work.files if f.kind == FileKind.EBOOK),
        [(t.id, t.sha256) for t in _tracks(db, work.id)],
        [al.id, al.status.value, al.sentence_count, al.audio_fingerprint] if al else None,
        list(sentences),
    ]
    return hashlib.sha256(json.dumps(payload, default=str).encode()).hexdigest()[:32]


def manifest(db, settings, work: Work) -> dict:
    epub = primary_epub(db, work.id)
    pdf = next(
        (f for f in sorted(work.files, key=lambda f: f.id) if f.media_type == PDF),
        None,
    )
    files = []
    if epub is not None:
        files.append(
            {
                "key": "epub",
                "kind": "epub",
                "file_id": epub.id,
                "media_type": epub.media_type,
                "size": epub.size,
                "sha256": epub.sha256,
                "url": f"/library/works/{work.id}/book.epub?offline=1",
            }
        )
    if pdf is not None:
        files.append(
            {
                "key": f"pdf-{pdf.id}",
                "kind": "pdf",
                "file_id": pdf.id,
                "media_type": pdf.media_type,
                "size": pdf.size,
                "sha256": pdf.sha256,
                "url": f"/library/files/{pdf.id}/view?offline=1",
            }
        )
    tracks = []
    for t in _tracks(db, work.id):
        found = find_variant(settings.library_dir, work, t.sha256)
        tracks.append(
            {
                "id": t.id,
                "position": t.position,
                "full": {
                    "url": f"/library/tracks/{t.id}/stream",
                    "size": t.size,
                    "media_type": t.media_type,
                },
                "low": {
                    "url": f"/library/tracks/{t.id}/stream?variant=low",
                    "size": found[0].stat().st_size,
                    "media_type": MEDIA_TYPE[found[1]],
                }
                if found
                else None,
            }
        )
    al = db.scalar(select(Alignment).where(Alignment.work_id == work.id))
    matched = db.scalar(
        select(func.count())
        .select_from(Sentence)
        .where(Sentence.work_id == work.id, Sentence.matched.is_(True))
    )
    return {
        "work_id": work.id,
        "revision": revision(db, work),
        "files": files,
        "audio": {
            "tracks": tracks,
            "full_bytes": sum(t["full"]["size"] for t in tracks),
            "low_bytes": sum(t["low"]["size"] for t in tracks)
            if tracks and all(t["low"] for t in tracks)
            else None,
        }
        if tracks
        else None,
        # Read-along works offline when the EPUB and an alignment with matched sentences exist.
        "readalong": bool(
            epub is not None
            and tracks
            and al is not None
            and al.status == AlignmentStatus.SUCCEEDED
            and matched
        ),
    }


@router.get("/works/{work_id}/offline")
def offline_manifest(auth: Auth, db: DbDep, settings: SettingsDep, work_id: int) -> dict:
    return manifest(db, settings, _visible(db, auth.user, work_id))


class StartIn(BaseModel):
    parts: list[str] = Field(default_factory=list, max_length=8)


@router.post("/works/{work_id}/offline")
def offline_start(
    request: Request, auth: AuthCsrf, db: DbDep, settings: SettingsDep, work_id: int, body: StartIn
) -> dict:
    """The manifest, with the start of an offline download in the audit log. The files are
    counted as downloads when they are fetched."""
    out = manifest(db, settings, _visible(db, auth.user, work_id))
    audit.record(
        db,
        "download.offline",
        actor_user_id=auth.user.id,
        target=f"work:{work_id}",
        ip=client_ip(request),
        detail=",".join(p[:16] for p in body.parts)[:200] or None,
    )
    db.commit()
    return out


class CheckItem(BaseModel):
    id: int
    revision: str = Field(default="", max_length=64)


class CheckIn(BaseModel):
    works: list[CheckItem] = Field(max_length=1000)


@router.post("/offline/check")
def offline_check(auth: AuthCsrf, db: DbDep, body: CheckIn) -> dict:
    ids = [w.id for w in body.works]
    visible = {
        w.id: w
        for w in db.scalars(
            select(Work)
            .options(selectinload(Work.files))
            .where(Work.id.in_(ids), visible_works(auth.user))
        )
    }
    out = []
    for item in body.works:
        work = visible.get(item.id)
        if work is None:
            out.append({"id": item.id, "status": "gone"})
            continue
        rev = revision(db, work)
        out.append(
            {"id": item.id, "status": "ok" if rev == item.revision else "changed", "revision": rev}
        )
    return {"works": out}
