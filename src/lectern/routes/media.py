"""Interim implementation of the milestone 2 media contract: track list and range streaming.

The read-along player (milestone 4) needs ``GET /api/works/{id}/tracks`` and
``GET /library/tracks/{id}/stream`` exactly as docs/contracts.md defines them. Milestone 2
owns these routes (audio ingest, low-bitrate variants, OPDS Basic auth on the stream, the
per-session playback audit); until it lands this module serves the same shapes from the
``audio_tracks`` table with session auth only. When milestone 2 merges, delete this module
and its ``include_router`` in :mod:`lectern.main`; nothing else depends on it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import select

from lectern.models import AudioTrack
from lectern.routes.api.readalong import alignment_json, current_alignment, visible_work
from lectern.security.deps import Auth, DbDep, SettingsDep
from lectern.sentence_map import audio_fingerprint

router = APIRouter()


def track_json(t: AudioTrack) -> dict:
    return {
        "id": t.id,
        "position": t.position,
        "media_type": t.media_type,
        "codec": t.codec,
        "duration_s": t.duration_s,
        "offset_s": t.offset_s,
        "size": t.size,
        "sha256": t.sha256,
        "chapters": t.chapters or [],
        "stream_url": f"/library/tracks/{t.id}/stream",
    }


@router.get("/api/works/{work_id}/tracks")
def tracks(auth: Auth, db: DbDep, work_id: int) -> dict:
    visible_work(db, auth.user, work_id)
    rows = db.scalars(
        select(AudioTrack).where(AudioTrack.work_id == work_id).order_by(AudioTrack.position)
    ).all()
    return {
        "work_id": work_id,
        "audio_fingerprint": audio_fingerprint(t.sha256 for t in rows) if rows else None,
        "total_duration_s": sum(t.duration_s for t in rows),
        "alignment": alignment_json(current_alignment(db, work_id)),
        "tracks": [track_json(t) for t in rows],
    }


@router.get("/library/tracks/{track_id}/stream", include_in_schema=False)
def stream(request: Request, auth: Auth, db: DbDep, settings: SettingsDep, track_id: int):
    track = db.get(AudioTrack, track_id)
    if track is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    work = visible_work(db, auth.user, track.work_id)
    root = settings.library_dir.resolve()
    path = (root / work.dir_path / track.rel_path).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File missing")
    etag = f'"{track.sha256}"'
    headers = {"etag": etag, "cache-control": "private, max-age=3600"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    # Starlette answers Range / If-Range itself (206, 416 with Content-Range: bytes */size).
    return FileResponse(path, media_type=track.media_type, headers=headers)
