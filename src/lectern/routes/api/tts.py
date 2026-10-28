"""Generated audiobooks ("Make an audiobook") and the ElevenLabs integration (docs/tts.md).

- ``GET  /api/works/{id}/audiobook``           eligibility, chapters, engines, the latest job
- ``POST /api/works/{id}/audiobook/estimate``  characters, length, ETA; ElevenLabs credits+quota
- ``POST /api/works/{id}/audiobook``           queue a narration (ElevenLabs needs ``confirm``)
- ``POST /api/works/{id}/audiobook/retry``     queue the last failed one again (resumes)
- ``GET|PUT|DELETE /api/integrations/elevenlabs``, ``POST …/test``, ``GET …/voices``,
  ``GET …/models``, ``POST …/preview``         owner only

Job management (list, cancel) is the shared ``/api/jobs`` (``routes/api/align.py``).
ElevenLabs is contacted only by the integration routes and by a confirmed narration.
"""

from __future__ import annotations

import base64
import io
import wave
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from lectern import egress, integrations
from lectern.models import Role, WorkerCredential
from lectern.routes.api.align import server_capabilities
from lectern.routes.api.readalong import visible_work
from lectern.security import audit
from lectern.security.deps import (
    AuthContext,
    DbDep,
    OwnerOnly,
    SettingsDep,
    require_role,
    require_role_read,
)
from lectern.security.sessions import client_ip
from lectern.tts import elevenlabs as el
from lectern.tts import jobs as tj
from lectern.tts import service as svc

router = APIRouter()

Manager = Annotated[AuthContext, Depends(require_role(Role.OWNER, Role.MEMBER))]
Viewer = Annotated[AuthContext, Depends(require_role_read(Role.OWNER, Role.MEMBER))]
OwnerRead = Annotated[AuthContext, Depends(require_role_read(Role.OWNER))]


def transport(request: Request):
    """Tests replace the ElevenLabs transport here; production uses the fixed-origin one."""
    return getattr(request.app.state, "elevenlabs_transport", None)


def _raise(exc: svc.ServiceError):
    raise HTTPException(exc.status, {"detail": exc.code, **exc.detail}) from exc


def _el(exc: el.ElevenLabsError):
    code = 502 if exc.retryable else (400 if exc.code == "rejected" else 409)
    raise HTTPException(code, {"detail": f"elevenlabs_{exc.code}", "message": exc.message}) from exc


def _workers(db) -> dict[int, str]:
    from sqlalchemy import select

    return dict(db.execute(select(WorkerCredential.id, WorkerCredential.label)).all())


# --- per work ----------------------------------------------------------------------------------


@router.get("/works/{work_id}/audiobook")
def audiobook_state(auth: Viewer, db: DbDep, settings: SettingsDep, work_id: int) -> dict:
    work = visible_work(db, auth.user, work_id)
    reason = svc.eligibility(db, settings, work, auth.user)
    db.commit()
    chapters = None
    if reason in (None, "has_audio"):
        try:
            book, _ = svc.book_for(db, settings, work)
            chapters = svc.chapters_json(book)
        except svc.ServiceError as exc:
            reason = reason or exc.code
    job = tj.latest_job(db, work.id)
    edition = tj.generated(db, work.id)
    return {
        "work_id": work.id,
        "eligible": reason is None,
        "reason": reason,
        "chapters": chapters,
        **svc.catalog(db, settings, auth.user),
        "job": tj.job_json(
            db,
            job,
            _workers(db),
            server_caps=server_capabilities(db, settings),
            for_owner=auth.user.role == Role.OWNER,
        )  # fmt: skip
        if job
        else None,
        "edition": (
            {
                "engine": edition.engine,
                "model": edition.model,
                "voice": edition.voice,
                "speed": edition.speed,
                "chapters": edition.chapters,
                "duration_s": edition.duration_s,
                "created_at": tj._iso(edition.created_at),
            }  # fmt: skip
            if edition
            else None
        ),
        "disclosure": (
            "ElevenLabs narration sends the text of the chosen chapters to ElevenLabs "
            "(api.elevenlabs.io) with your API key and uses your account's credits. Piper and "
            "Kokoro run on this server or on your own worker; no text leaves them."
        ),
    }


@router.post("/works/{work_id}/audiobook/estimate")
def audiobook_estimate(
    request: Request,
    auth: Manager,
    db: DbDep,
    settings: SettingsDep,
    work_id: int,
    body: svc.AudiobookIn,
) -> dict:
    work = visible_work(db, auth.user, work_id)
    try:
        return svc.estimate(db, settings, work, body, auth.user, transport(request))
    except svc.ServiceError as exc:
        _raise(exc)
    except el.ElevenLabsError as exc:
        _el(exc)


@router.post("/works/{work_id}/audiobook", status_code=status.HTTP_202_ACCEPTED)
def audiobook_create(
    request: Request,
    auth: Manager,
    db: DbDep,
    settings: SettingsDep,
    work_id: int,
    body: svc.AudiobookIn,
) -> dict:
    work = visible_work(db, auth.user, work_id)
    try:
        job = svc.enqueue(db, settings, work, body, auth.user, transport(request))
    except svc.ServiceError as exc:
        db.rollback()
        _raise(exc)
    except el.ElevenLabsError as exc:
        db.rollback()
        _el(exc)
    p = job.payload or {}
    detail = f"{p['engine']}/{p['voice']} {p['chapters']} chapters, {p['characters']} chars"
    if p["engine"] == "elevenlabs":
        detail += f", estimate {p['credits']:.0f} credits, cap {p['cap_credits']:.0f}"
    audit.record(db, "tts.request", actor_user_id=auth.user.id, target=f"work:{work.id}",
                 ip=client_ip(request), detail=detail)  # fmt: skip
    db.commit()
    _wake(request)
    return tj.job_json(db, job, _workers(db))


@router.post("/works/{work_id}/audiobook/retry", status_code=status.HTTP_202_ACCEPTED)
def audiobook_retry(
    request: Request, auth: Manager, db: DbDep, settings: SettingsDep, work_id: int
) -> dict:
    work = visible_work(db, auth.user, work_id)
    try:
        job = svc.retry(db, settings, work, auth.user)
    except svc.ServiceError as exc:
        db.rollback()
        _raise(exc)
    audit.record(db, "tts.retry", actor_user_id=auth.user.id, target=f"work:{work.id}",
                 ip=client_ip(request), detail=f"job {job.id}")  # fmt: skip
    db.commit()
    _wake(request)
    return tj.job_json(db, job, _workers(db))


class LocalPreviewIn(BaseModel):
    engine: str = Field(pattern="^(piper|kokoro)$")
    voice: str = Field(min_length=1, max_length=128)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    text: str | None = Field(default=None, max_length=300)


@router.post("/works/{work_id}/audiobook/preview")
def audiobook_preview(
    request: Request,
    auth: Manager,
    db: DbDep,
    settings: SettingsDep,
    work_id: int,
    body: LocalPreviewIn,
) -> Response:
    """A few seconds of a Piper (or server-side Kokoro) voice reading this book, as WAV.
    Synthesised here, on this server; nothing leaves it."""
    from lectern.tts import runner

    work = visible_work(db, auth.user, work_id)
    if body.engine not in runner.local_engines(db, settings):
        raise HTTPException(409, {"detail": "engine_not_on_server"})
    text = svc.preview_text(db, settings, work, body.text)
    try:
        synth = runner.default_synth(
            body.engine, {"voice": body.voice, "speed": body.speed}, settings
        )
        try:
            pcm = synth.sentence(text)
        finally:
            if hasattr(synth, "close"):
                synth.close()  # the engine's child process (lectern.tts.child)
    except Exception as exc:  # noqa: BLE001 - a voice that does not load is the caller's 422
        raise HTTPException(422, {"detail": "preview_failed", "message": str(exc)[:200]}) from exc
    return _wav(pcm, synth.sample_rate, text)


def _wav(pcm: bytes, rate: int, text: str) -> Response:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return Response(buf.getvalue(), media_type="audio/wav", headers={
        "Cache-Control": "no-store", "X-Preview-Characters": str(len(text)),
        "X-Preview-Text": base64.b64encode(text.encode()).decode(),
    })  # fmt: skip


def _wake(request: Request) -> None:
    runner = getattr(request.app.state, "tts", None)
    if runner is not None and hasattr(runner, "wake"):
        runner.wake()


# --- ElevenLabs integration (owner only) --------------------------------------------------------


class KeyIn(BaseModel):
    api_key: str = Field(min_length=8, max_length=256)


class PreviewIn(BaseModel):
    voice: str = Field(min_length=1, max_length=128)
    model: str | None = Field(default=None, max_length=64)
    speed: float = Field(default=1.0, ge=0.7, le=1.2)
    voice_settings: svc.VoiceSettings | None = None
    work_id: int | None = Field(default=None, ge=1)
    text: str | None = Field(default=None, max_length=300)


@router.get("/integrations/elevenlabs")
def elevenlabs_status(auth: OwnerRead, db: DbDep, settings: SettingsDep) -> dict:
    out = integrations.status(db, settings, integrations.ELEVENLABS)
    out["endpoint"] = el.ORIGIN
    out["offline"] = egress.offline()
    out["usage"] = _usage(db)
    return out


@router.put("/integrations/elevenlabs")
def elevenlabs_save(
    request: Request, auth: OwnerOnly, db: DbDep, settings: SettingsDep, body: KeyIn
) -> dict:
    """Save (encrypted) and test the key in one step. The key is never returned."""
    key = body.api_key.strip()
    ok, detail = _test(el.Client(key, transport(request)))
    if not ok and detail == "key_rejected":
        raise HTTPException(
            400, {"detail": "elevenlabs_key_rejected", "message": "ElevenLabs rejected that key"}
        )
    row = integrations.store(db, settings, integrations.ELEVENLABS, key, auth.user.id)
    _record_test(row, ok, detail)
    audit.record(db, "integration.elevenlabs.set", actor_user_id=auth.user.id,
                 target="integration:elevenlabs", ip=client_ip(request), detail=f"…{row.key_hint}")  # fmt: skip
    db.commit()
    return elevenlabs_status(auth, db, settings)


@router.post("/integrations/elevenlabs/test")
def elevenlabs_test(request: Request, auth: OwnerOnly, db: DbDep, settings: SettingsDep) -> dict:
    try:
        c = svc.client(db, settings, transport(request))
    except svc.ServiceError as exc:
        _raise(exc)
    ok, detail = _test(c)
    _record_test(integrations.active(db, integrations.ELEVENLABS), ok, detail)
    audit.record(db, "integration.elevenlabs.test", actor_user_id=auth.user.id,
                 target="integration:elevenlabs", ip=client_ip(request),
                 detail="ok" if ok else detail)  # fmt: skip
    db.commit()
    return elevenlabs_status(auth, db, settings)


@router.delete("/integrations/elevenlabs")
def elevenlabs_revoke(request: Request, auth: OwnerOnly, db: DbDep, settings: SettingsDep) -> dict:
    """Forget the key now. Queued ElevenLabs narrations fail at their next request."""
    if integrations.revoke(db, integrations.ELEVENLABS):
        audit.record(db, "integration.elevenlabs.revoke", actor_user_id=auth.user.id,
                     target="integration:elevenlabs", ip=client_ip(request))  # fmt: skip
        db.commit()
    return elevenlabs_status(auth, db, settings)


@router.get("/integrations/elevenlabs/voices")
def elevenlabs_voices(request: Request, auth: OwnerRead, db: DbDep, settings: SettingsDep) -> dict:
    try:
        return {"voices": svc.client(db, settings, transport(request)).voices()}
    except svc.ServiceError as exc:
        _raise(exc)
    except el.ElevenLabsError as exc:
        _el(exc)


@router.get("/integrations/elevenlabs/models")
def elevenlabs_models(request: Request, auth: OwnerRead, db: DbDep, settings: SettingsDep) -> dict:
    try:
        return {
            "models": svc.client(db, settings, transport(request)).models(),
            "default": el.DEFAULT_MODEL,
        }
    except svc.ServiceError as exc:
        _raise(exc)
    except el.ElevenLabsError as exc:
        _el(exc)


@router.post("/integrations/elevenlabs/preview")
def elevenlabs_preview(
    request: Request, auth: OwnerOnly, db: DbDep, settings: SettingsDep, body: PreviewIn
) -> Response:
    """A few seconds of the voice reading this book (or the given text): ≤ 300 characters,
    billed to the account, recorded as usage and audited. Returns a WAV."""
    work = visible_work(db, auth.user, body.work_id) if body.work_id else None
    text = svc.preview_text(db, settings, work, body.text)
    try:
        c = svc.client(db, settings, transport(request))
        model = body.model or el.DEFAULT_MODEL
        vs = body.voice_settings.model_dump(exclude_none=True) if body.voice_settings else {}
        if abs(body.speed - 1.0) > 1e-6:
            vs["speed"] = body.speed
        speech = c.speech(body.voice, text, model_id=model, voice_settings=vs or None)
    except svc.ServiceError as exc:
        _raise(exc)
    except el.ElevenLabsError as exc:
        _el(exc)
    credits = len(text) * el.credits_per_character(model)
    tj.record_usage(db, provider=integrations.ELEVENLABS, kind="preview", characters=len(text),
                    credits=credits, work_id=work.id if work else None, user_id=auth.user.id)  # fmt: skip
    audit.record(db, "integration.elevenlabs.preview", actor_user_id=auth.user.id,
                 target=f"work:{work.id}" if work else "integration:elevenlabs",
                 ip=client_ip(request), detail=f"{body.voice} {len(text)} chars")  # fmt: skip
    db.commit()
    return _wav(speech.pcm, el.SAMPLE_RATE, text)


def _test(c: el.Client) -> tuple[bool, str]:
    try:
        q = c.subscription()
    except el.ElevenLabsError as exc:
        return False, exc.code
    return (
        True,
        f"{q.get('tier') or 'plan'}: {q['remaining']} of {q['characters_limit']} credits left",
    )


def _record_test(row, ok: bool, detail: str) -> None:
    if row is None:
        return
    from lectern.models import utcnow

    row.last_tested_at = utcnow()
    row.last_test_ok = ok
    row.last_test_detail = detail[:256]


def _usage(db) -> dict:
    from datetime import timedelta

    from sqlalchemy import func, select

    from lectern.models import IntegrationUsage, utcnow

    since = utcnow() - timedelta(days=30)
    chars, credits = db.execute(
        select(
            func.coalesce(func.sum(IntegrationUsage.characters), 0),
            func.coalesce(func.sum(IntegrationUsage.credits), 0.0),
        ).where(  # fmt: skip
            IntegrationUsage.provider == integrations.ELEVENLABS,
            IntegrationUsage.created_at >= since,
        )
    ).one()
    return {"days": 30, "characters": int(chars), "credits": round(float(credits), 1)}
