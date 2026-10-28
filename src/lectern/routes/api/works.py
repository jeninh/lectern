"""Per-work routes: audio tracks, sentences, canonical progress, the sleep-timer rewind point,
playback auditing, data-saver renditions, the Listening list, and ingest match review."""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
from collections import OrderedDict
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from lectern.access import can_edit_library, can_see_work, visible_works
from lectern.align import jobs as align_jobs
from lectern.editing import Journal
from lectern.fsops import LIBRARY_LOCK
from lectern.ingest.merge import merge_work
from lectern.jobs import (
    active_job,
    delete_renditions,
    enqueue_transcode,
    find_variant,
    generation_off,
    rendition_sizes,
    transcoder_unavailable,
)
from lectern.models import (
    Alignment,
    AudioTrack,
    AuditLog,
    JobStatus,
    MatchSuggestion,
    ProgressSource,
    Role,
    Sentence,
    Work,
    utcnow,
)
from lectern.progress import ProgressError, progress_json, put_progress
from lectern.progress import get_progress as stored_progress
from lectern.routes.api.core import _audio_summary, _positions
from lectern.routes.api.offline import OfflineReplay, conflict
from lectern.routes.api.serializers import iso, work_json
from lectern.security import audit
from lectern.security.deps import Auth, AuthContext, AuthCsrf, DbDep, SettingsDep, require_role
from lectern.security.sessions import client_ip
from lectern.sentence_map import audio_fingerprint
from lectern.workmeta import write_book_json

router = APIRouter()
Manager = Annotated[AuthContext, Depends(require_role(Role.OWNER, Role.MEMBER))]


def _visible_work(db, user, work_id: int) -> Work:
    work = db.get(Work, work_id)
    if work is None or not can_see_work(db, user, work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return work


def _alignment_json(db, work_id: int, user) -> dict | None:
    """Status, coverage, and the matcher's diagnosis (``lectern.align.jobs.alignment_json``);
    the error text only for owners and members."""
    al = db.scalar(select(Alignment).where(Alignment.work_id == work_id))
    return align_jobs.alignment_json(db, al, detailed=can_edit_library(user))


def _tracks(db, work_id: int) -> list[AudioTrack]:
    return list(
        db.scalars(
            select(AudioTrack).where(AudioTrack.work_id == work_id).order_by(AudioTrack.position)
        )
    )


@router.get("/works/{work_id}/tracks")
def tracks(auth: Auth, db: DbDep, settings: SettingsDep, work_id: int) -> dict:
    work = _visible_work(db, auth.user, work_id)
    rows = _tracks(db, work_id)
    job = active_job(db, work_id)
    out = []
    for t in rows:
        found = find_variant(settings.library_dir, work, t.sha256)
        out.append(
            {
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
                "download_url": f"/library/tracks/{t.id}/download",
                "variants": {
                    "low": {
                        "codec": found[1],
                        "bitrate_kbps": settings.audio_low_bitrate_kbps,
                        "size": found[0].stat().st_size,
                        "stream_url": f"/library/tracks/{t.id}/stream?variant=low",
                    }
                    if found
                    else None
                },
            }
        )
    # Why copies cannot be made names server internals: owners and members only.
    editor = can_edit_library(auth.user)
    unavailable = transcoder_unavailable(db, settings) if rows and editor else None
    have, original, low = rendition_sizes(settings, work, rows)
    return {
        "work_id": work_id,
        "audio_fingerprint": audio_fingerprint(t.sha256 for t in rows),
        "total_duration_s": round(sum(t.duration_s for t in rows), 3),
        "alignment": _alignment_json(db, work_id, auth.user),
        "tracks": out,
        "transcode": {
            "status": job.status.value,
            "progress": (job.result or {}).get("done"),
            "total": len((job.payload or {}).get("tracks", [])),
            # a queued job with nothing to run it says so instead of waiting silently
            "message": unavailable["message"]
            if unavailable and job.status == JobStatus.QUEUED
            else None,
        }
        if job
        else None,
        # Whether copies may be made, what could make them, and what they save.
        "data_saver": {
            "off": generation_off(db, work) if editor else None,
            "unavailable": unavailable,
            "tracks_ready": have,
            "original_bytes": original,
            "low_bytes": low,
        },
    }


@router.get("/works/{work_id}/sentences")
def sentences(
    auth: Auth,
    db: DbDep,
    work_id: int,
    from_: Annotated[int | None, Query(alias="from", ge=0)] = None,
    to: Annotated[int | None, Query(ge=0)] = None,
    t: Annotated[float | None, Query(ge=0)] = None,
    around: Annotated[int, Query(ge=0, le=50)] = 0,
    spine: Annotated[int | None, Query(ge=0)] = None,
    char: Annotated[int | None, Query(ge=0)] = None,
) -> dict:
    _visible_work(db, auth.user, work_id)
    modes = [from_ is not None, t is not None, spine is not None]
    if sum(modes) != 1 or (spine is not None and char is None):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "exactly one query mode")
    base = select(Sentence).where(Sentence.work_id == work_id)
    count = db.scalar(select(func.count()).select_from(Sentence).where(Sentence.work_id == work_id))
    if from_ is not None:
        end = to if to is not None else from_ + 500
        end = min(end, from_ + 2000)
        rows = db.scalars(base.where(Sentence.n >= from_, Sentence.n < end).order_by(Sentence.n))
    elif t is not None:
        hit = db.scalar(
            base.where(Sentence.matched.is_(True), Sentence.audio_start_s <= t)
            .order_by(Sentence.audio_start_s.desc())
            .limit(1)
        )
        if hit is None:
            hit = db.scalar(base.where(Sentence.matched.is_(True)).order_by(Sentence.n).limit(1))
        rows = (
            db.scalars(
                base.where(Sentence.n >= hit.n - around, Sentence.n <= hit.n + around).order_by(
                    Sentence.n
                )
            )
            if hit
            else []
        )
    else:
        hit = db.scalar(
            base.where(
                (Sentence.spine_index < spine)
                | ((Sentence.spine_index == spine) & (Sentence.char_start <= char))
            )
            .order_by(Sentence.spine_index.desc(), Sentence.char_start.desc())
            .limit(1)
        )
        start = (
            hit.n
            if hit is not None and hit.spine_index == spine and char < hit.char_end
            else (hit.n + 1 if hit is not None else 0)
        )
        rows = db.scalars(
            base.where(Sentence.n >= start, Sentence.n <= start + 100).order_by(Sentence.n)
        )
    return {
        "work_id": work_id,
        "count": count or 0,
        "alignment": _alignment_json(db, work_id, auth.user),
        "sentences": [
            {
                "n": s.n,
                "spine_index": s.spine_index,
                "char_start": s.char_start,
                "char_end": s.char_end,
                "audio_start_s": s.audio_start_s,
                "audio_end_s": s.audio_end_s,
                "matched": s.matched,
            }
            for s in rows
        ],
    }


@router.get("/works/{work_id}/progress")
def get_progress(auth: Auth, db: DbDep, work_id: int) -> dict:
    _visible_work(db, auth.user, work_id)
    return progress_json(db, auth.user.id, work_id)


class ProgressIn(BaseModel):
    source: Literal["web", "audio", "koreader"]
    device: str = Field(default="", max_length=128)
    sentence_n: int | None = Field(default=None, ge=0)
    offset: int = Field(default=0, ge=0)
    audio_s: float | None = Field(default=None, ge=0)
    spine_index: int | None = Field(default=None, ge=0)
    char: int | None = Field(default=None, ge=0)
    # Web reader only: its own precise locator (an EPUB CFI), kept as ``raw``.
    raw: str | None = Field(default=None, max_length=1024)
    # The listening speed now in use for this work (player, read-along listen mode); kept per
    # user and work, and returned as ``speed``. Omitted: the stored speed stays.
    speed: float | None = Field(default=None, ge=0.5, le=4)
    # A position saved while offline, sent later from the device's queue (docs/offline.md).
    offline: OfflineReplay | None = None

    @model_validator(mode="after")
    def _one(self) -> ProgressIn:
        given = [
            self.sentence_n is not None,
            self.audio_s is not None,
            self.spine_index is not None,
        ]
        if sum(given) != 1 or (self.spine_index is not None and self.char is None):
            raise ValueError("exactly one of sentence_n, audio_s, spine_index+char")
        return self


@router.put("/works/{work_id}/progress")
def set_progress(auth: AuthCsrf, db: DbDep, work_id: int, body: ProgressIn) -> dict:
    _visible_work(db, auth.user, work_id)
    if body.source == "koreader":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "koreader_source_reserved")
    if body.offline is not None:
        row = stored_progress(db, auth.user.id, work_id)
        wins, recorded = body.offline.judge(row.received_at if row else None)
        if not wins:
            raise conflict(progress_json(db, auth.user.id, work_id), recorded)
    try:
        put_progress(
            db,
            auth.user.id,
            work_id,
            ProgressSource(body.source),
            body.device,
            sentence=(body.sentence_n, body.offset) if body.sentence_n is not None else None,
            audio_s=body.audio_s,
            text=(body.spine_index, body.char) if body.spine_index is not None else None,
            raw=body.raw if body.source == "web" else None,
            speed=body.speed,
        )
    except ProgressError as exc:
        raise HTTPException(exc.status, exc.code) from None
    db.commit()
    return progress_json(db, auth.user.id, work_id)


_SILENCE_END = re.compile(r"silence_end: (-?\d+(?:\.\d+)?)")
REWIND_WINDOW_S = 25.0
# (track sha256, position to 0.1 s) -> rewind point; ffmpeg runs once per pair.
_rewind_cache: OrderedDict[tuple[str, float], float | None] = OrderedDict()
_rewind_lock = threading.Lock()
REWIND_CACHE_SIZE = 512


def _cached_silence_rewind(request: Request, user_id: int, settings, track, path, local_t: float):
    """:func:`_silence_rewind` behind a per-track cache and a per-user rate limit (ffmpeg
    decodes up to 25 s of audio). The route is a plain ``def``, so this runs in the threadpool."""
    key = (track.sha256, round(local_t, 1))
    with _rewind_lock:
        if key in _rewind_cache:
            _rewind_cache.move_to_end(key)
            return _rewind_cache[key]
    if not request.app.state.limiters["rewind"].allow(f"user:{user_id}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")
    found = _silence_rewind(settings, path, key[1])
    with _rewind_lock:
        _rewind_cache[key] = found
        while len(_rewind_cache) > REWIND_CACHE_SIZE:
            _rewind_cache.popitem(last=False)
    return found


def _silence_rewind(settings, path, local_t: float) -> float | None:
    """Start of the sentence playing at ``local_t`` (seconds into one file): the end of the
    last pause of at least 0.4 s before it, found with ffmpeg's silencedetect."""
    if shutil.which(settings.ffmpeg_path) is None:
        return None
    ws = max(0.0, local_t - REWIND_WINDOW_S)
    try:
        res = subprocess.run(  # noqa: S603 - fixed argv
            [
                settings.ffmpeg_path,
                "-nostdin",
                "-hide_banner",
                "-ss",
                f"{ws:.3f}",
                "-t",
                f"{local_t - ws:.3f}",
                "-i",
                str(path),
                "-af",
                "silencedetect=noise=-35dB:d=0.4",
                "-f",
                "null",
                "-",
            ],  # fmt: skip
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    ends = [ws + float(m) for m in _SILENCE_END.findall(res.stderr.decode(errors="replace"))]
    ends = [e for e in ends if e <= local_t - 0.3]
    return max(ends) if ends else None


@router.get("/works/{work_id}/rewind-point")
def rewind_point(
    request: Request,
    auth: Auth,
    db: DbDep,
    settings: SettingsDep,
    work_id: int,
    t: Annotated[float, Query(ge=0)],
) -> dict:
    """Where the sleep timer should leave the listener: the start of the sentence heard last.

    Exact once the work is aligned (the matched sentence spoken at ``t``); before that, the
    end of the last pause in the audio; failing both, 5 seconds back.
    """
    work = _visible_work(db, auth.user, work_id)
    hit = db.scalar(
        select(Sentence)
        .where(Sentence.work_id == work_id, Sentence.matched.is_(True), Sentence.audio_start_s <= t)
        .order_by(Sentence.audio_start_s.desc())
        .limit(1)
    )
    if hit is not None and t <= (hit.audio_end_s or t) + 1.0:
        return {"t": hit.audio_start_s, "method": "alignment", "sentence_n": hit.n}
    track = db.scalar(
        select(AudioTrack)
        .where(AudioTrack.work_id == work_id, AudioTrack.offset_s <= t)
        .order_by(AudioTrack.offset_s.desc())
        .limit(1)
    )
    if track is not None:
        local = t - track.offset_s
        found = _cached_silence_rewind(
            request,
            auth.user.id,
            settings,
            track,
            settings.library_dir / work.dir_path / track.rel_path,
            local,
        )
        if found is not None:
            return {"t": round(track.offset_s + found, 3), "method": "silence"}
    return {"t": max(0.0, t - 5.0), "method": "fallback"}


@router.post("/works/{work_id}/listen")
def listen_started(request: Request, auth: AuthCsrf, db: DbDep, work_id: int) -> dict:
    """Playback of a work began: audited once per browser session (streams are not)."""
    _visible_work(db, auth.user, work_id)
    target, detail = f"work:{work_id}", f"session:{auth.session.id}"
    seen = db.scalar(
        select(AuditLog.id).where(
            AuditLog.action == "listen.start",
            AuditLog.actor_user_id == auth.user.id,
            AuditLog.target == target,
            AuditLog.detail == detail,
        )
    )
    if seen is not None:
        return {"recorded": False}
    audit.record(
        db,
        "listen.start",
        actor_user_id=auth.user.id,
        target=target,
        ip=client_ip(request),
        detail=detail,
    )
    db.commit()
    return {"recorded": True}


@router.post("/works/{work_id}/transcode")
def transcode(
    request: Request, auth: Manager, db: DbDep, settings: SettingsDep, work_id: int
) -> dict:
    """Queue data-saver renditions for tracks that lack one (owner and member)."""
    work = _visible_work(db, auth.user, work_id)
    if not _tracks(db, work_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "no_audio")
    if off := generation_off(db, work):
        raise HTTPException(status.HTTP_409_CONFLICT, off)
    # Neither ffmpeg here nor a transcoding worker online: say so rather than queue forever.
    if transcoder_unavailable(db, settings) is not None and active_job(db, work_id) is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "no_transcoder")
    job = enqueue_transcode(db, settings, work, auth.user.id)
    db.commit()
    runner = getattr(request.app.state, "jobs", None)
    if runner is not None:
        runner.wake.set()
    if job is None:
        return {"status": "ready"}
    return {"status": job.status.value, "job_id": job.id}


@router.delete("/works/{work_id}/transcode")
def delete_transcode(auth: Manager, db: DbDep, settings: SettingsDep, work_id: int) -> dict:
    """Delete the work's data-saver copies and stop any being made (owner and member).
    Originals are untouched; ``POST`` makes them again."""
    work = _visible_work(db, auth.user, work_id)
    freed = delete_renditions(db, settings, work)
    db.commit()
    return {"deleted_bytes": freed}


@router.get("/listening")
def listening(auth: Auth, db: DbDep) -> dict:
    """Works with audio, the ones in progress first (most recent), then by title."""
    ids = list(
        db.scalars(
            select(Work.id)
            .where(Work.id.in_(select(AudioTrack.work_id)), visible_works(auth.user))
            .order_by(Work.sort_title)
        )
    )
    works = {
        w.id: w
        for w in db.scalars(select(Work).options(selectinload(Work.files)).where(Work.id.in_(ids)))
    }
    audio = _audio_summary(db, ids)
    positions = _positions(db, auth.user.id, audio, ids)
    items = [work_json(works[i], audio=audio.get(i), position=positions.get(i)) for i in ids]
    items.sort(key=lambda w: (w["position"] or {}).get("received_at") or "", reverse=True)
    return {"items": items}


# --- ingest match review ------------------------------------------------------------------


def _suggestion_json(db, s: MatchSuggestion) -> dict:
    audio = _audio_summary(db, [s.work_id, s.candidate_work_id])
    new, cand = db.get(Work, s.work_id), db.get(Work, s.candidate_work_id)
    return {
        "id": s.id,
        "origin": s.origin,
        "score": s.score,
        "reason": s.reason,
        "created_at": iso(s.created_at),
        "work": work_json(new, audio=audio.get(new.id)),
        "candidate": work_json(cand, audio=audio.get(cand.id)),
    }


@router.get("/suggestions")
def suggestions(auth: Manager, db: DbDep) -> dict:
    live = select(Work.id).where(Work.trashed_at.is_(None))
    rows = db.scalars(
        select(MatchSuggestion)
        .where(
            MatchSuggestion.resolved_at.is_(None),
            MatchSuggestion.work_id.in_(live),
            MatchSuggestion.candidate_work_id.in_(live),
        )
        .order_by(MatchSuggestion.created_at.desc())
        .limit(100)
    ).all()
    return {"suggestions": [_suggestion_json(db, s) for s in rows]}


@router.post("/suggestions/scan")
def scan_duplicates(auth: Manager, db: DbDep) -> dict:
    """Look for likely duplicates among the works already in the library (local only; the
    hourly sweep runs the same scan). New pairs join the review list above."""
    from lectern import duplicates

    found = duplicates.scan(db)
    db.commit()
    return {"found": found}


def _open_suggestion(db, suggestion_id: int) -> MatchSuggestion:
    s = db.get(MatchSuggestion, suggestion_id)
    if (
        s is None
        or s.resolved_at is not None
        or any(db.get(Work, w).trashed_at is not None for w in (s.work_id, s.candidate_work_id))
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return s


def _merge_commit(
    request, auth, db, settings, source: Work, target: Work, label: str, *, keep_owner_data=False
) -> None:
    """Merge under the caller's library lock: file moves are journalled and put back if the
    commit fails; the emptied source folder and its history go only after it succeeds.
    ``keep_owner_data`` (library duplicates): the source's metadata, positions, and reading
    states fill what the target lacks first (``lectern.duplicates.carry_over``)."""
    from lectern import duplicates

    journal = Journal()
    try:
        restore = duplicates.carry_over(db, source, target) if keep_owner_data else None
        finish = merge_work(db, settings, source, target, journal)
        if restore:
            restore()
        target.version = (target.version or 1) + 1
        write_book_json(db, settings.library_dir, target)
        audit.record(
            db, "work.merge", actor_user_id=auth.user.id, target=target.title[:256],
            ip=client_ip(request), detail=label[:500],
        )  # fmt: skip
        db.commit()
    except Exception:
        db.rollback()
        journal.rollback()
        raise
    finish()


@router.post("/suggestions/{suggestion_id}/merge")
def merge_suggestion(
    request: Request, auth: Manager, db: DbDep, settings: SettingsDep, suggestion_id: int
) -> dict:
    """Confirm: the new work is the candidate. Its files move over and it is deleted."""
    with LIBRARY_LOCK:
        return _merge(request, auth, db, settings, suggestion_id)


def _merge(request, auth, db, settings, suggestion_id: int) -> dict:
    s = _open_suggestion(db, suggestion_id)
    source, target = db.get(Work, s.work_id), db.get(Work, s.candidate_work_id)
    label = f"{source.title} -> {target.title}"
    _merge_commit(
        request, auth, db, settings, source, target, label, keep_owner_data=s.origin == "library"
    )
    return {"work_id": target.id}


@router.post("/suggestions/{suggestion_id}/dismiss")
def dismiss_suggestion(auth: Manager, db: DbDep, suggestion_id: int) -> dict:
    """Keep both works separate."""
    s = _open_suggestion(db, suggestion_id)
    s.resolved_at, s.resolution = utcnow(), "dismissed"
    db.commit()
    return {"ok": True}


class MergeIn(BaseModel):
    target_work_id: int


@router.post("/works/{work_id}/merge")
def merge_into(
    request: Request, auth: Manager, db: DbDep, settings: SettingsDep, work_id: int, body: MergeIn
) -> dict:
    """Manually merge this work into another (e.g. an audiobook imported under another title)."""
    with LIBRARY_LOCK:
        return _merge_into(request, auth, db, settings, work_id, body)


def _merge_into(request, auth, db, settings, work_id: int, body: MergeIn) -> dict:
    source = _visible_work(db, auth.user, work_id)
    target = _visible_work(db, auth.user, body.target_work_id)
    if source.id == target.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "same_work")
    label = f"{source.title} -> {target.title}"
    _merge_commit(request, auth, db, settings, source, target, label)
    return {"work_id": target.id}
