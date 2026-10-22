"""What the API does for "Make an audiobook": chapters, engines and voices, estimates,
queueing a narration, and retrying one (routes in ``lectern.routes.api.tts``)."""

from __future__ import annotations

import json
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from lectern import integrations, queue
from lectern.align.tracks import sync_tracks, work_epub, work_tracks
from lectern.config import Settings
from lectern.models import Job, JobStatus, Role, User, Work
from lectern.tts import elevenlabs as el
from lectern.tts import engines as eng
from lectern.tts import jobs as tj
from lectern.tts import script as sc

# Typical narration is ~15 characters per second at speed 1.0 (about 150 words a minute).
CHARS_PER_AUDIO_SECOND = 15.0
CAP_HEADROOM = 1.1


class ServiceError(Exception):
    def __init__(self, status: int, code: str, detail: dict | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.detail = detail or {}


class VoiceSettings(BaseModel):
    """ElevenLabs voice settings (``speed`` comes from the request's ``speed``)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    stability: float | None = Field(default=None, ge=0, le=1)
    similarity_boost: float | None = Field(default=None, ge=0, le=1)
    style: float | None = Field(default=None, ge=0, le=1)
    use_speaker_boost: bool | None = None


class Confirm(BaseModel):
    """What the person saw and agreed to before an ElevenLabs narration is queued."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    characters: int = Field(ge=1)
    credits: float = Field(ge=0)
    cap_credits: float = Field(gt=0, le=10**9)
    send_text: bool = False  # "the book's text is sent to ElevenLabs"


class AudiobookIn(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    engine: Literal["kokoro", "piper", "elevenlabs"]
    voice: str = Field(min_length=1, max_length=128)
    speed: float = Field(default=1.0, ge=eng.SPEED_MIN, le=eng.SPEED_MAX)
    model: str | None = Field(default=None, max_length=64)
    voice_settings: VoiceSettings | None = None
    chapters: list[int] | None = Field(default=None, max_length=5000)
    codec: Literal["aac", "flac"] | None = None
    confirm: Confirm | None = None


# --- the work's text ---------------------------------------------------------------------------


@lru_cache(maxsize=16)
def _book(path: str, sha256: str) -> sc.Book:
    return sc.read_book(Path(path))


def book_for(db: Session, settings: Settings, work: Work) -> tuple[sc.Book, str]:
    found = work_epub(db, settings.library_dir, work)
    if found is None:
        raise ServiceError(409, "needs_epub")
    book, path = found
    try:
        return _book(str(path), book.sha256), book.sha256
    except Exception as exc:  # noqa: BLE001 - a damaged EPUB is the caller's 422
        raise ServiceError(422, "unreadable_epub") from exc


def eligibility(db: Session, settings: Settings, work: Work, user: User) -> str | None:
    """Why this person cannot make an audiobook of this work now, or ``None``."""
    if user.role not in (Role.OWNER, Role.MEMBER):
        return "not_allowed"
    if work_epub(db, settings.library_dir, work) is None:
        return "needs_epub"
    sync_tracks(db, settings.library_dir, work)
    if work_tracks(db, work.id):
        return "has_audio"
    return None


def chapters_json(book: sc.Book) -> list[dict]:
    return [
        {
            "spine_index": c.spine_index,
            "title": c.title,
            "sentences": len(c.sentences),
            "characters": c.characters,
            "preview": c.sentences[0].text[:160],
        }  # fmt: skip
        for c in book.chapters
    ]


# --- engines -----------------------------------------------------------------------------------


def kokoro_where(db: Session, settings: Settings) -> str | None:
    """``server`` when this server runs Kokoro, ``worker`` when a capable worker is online."""
    if settings.tts_kokoro_local and eng.kokoro_installed():
        return "server"
    return "worker" if queue.capability_online(db, "tts-kokoro") else None


KOKORO_SAMPLES = Path(__file__).resolve().parents[1] / "static" / "voice-samples" / "kokoro"


@lru_cache(maxsize=1)
def _kokoro_samples() -> dict:
    try:
        voices = json.loads((KOKORO_SAMPLES / "manifest.json").read_text())["voices"]
    except (OSError, ValueError, KeyError):
        return {}
    return {v: s for v, s in voices.items() if (KOKORO_SAMPLES / s["file"]).is_file()}


def kokoro_sample(voice: str) -> dict | None:
    """The built-in clip of a Kokoro voice (scripts/kokoro_samples.py): a standard sentence at
    speed 1.0, served from /static, so it plays with no engine on this server or worker."""
    s = _kokoro_samples().get(voice)
    if not s:
        return None
    return {"url": f"/static/voice-samples/kokoro/{s['file']}", "text": s["text"],
            "duration_s": s["duration_s"]}  # fmt: skip


def catalog(db: Session, settings: Settings, user: User) -> dict:
    piper_voices = [v.json() for v in eng.piper_voices(settings)]
    piper_ok = eng.piper_installed() and bool(piper_voices)
    kokoro = kokoro_where(db, settings)
    el_status = integrations.status(db, settings, integrations.ELEVENLABS)
    el_ok = el_status["configured"] and not el_status["undecryptable"]
    engines = [
        {
            "id": eng.KOKORO,
            "name": "Kokoro",
            "available": kokoro is not None,
            "runs_on": kokoro,
            "note": (
                "Natural voices. Runs on a worker that has Kokoro (for example the Mac "
                "worker), or on this server when it is installed here."
                if kokoro is None
                else "Runs on a connected worker."
                if kokoro == "worker"
                else "Runs on this server."
            ),
            "voices": [
                {"id": v, "name": v, "language": eng.kokoro_lang(v), "sample": kokoro_sample(v)}
                for v in eng.KOKORO_VOICES
            ],
            "default_voice": "af_heart",
            "model": eng.KOKORO_MODEL,
            "queues_without_runner": True,
        },
        {
            "id": eng.PIPER,
            "name": "Piper",
            "available": piper_ok,
            "runs_on": "server" if piper_ok else None,
            "note": (
                "Runs on this server's CPU, a Raspberry Pi included."
                if piper_ok
                else "Not installed: build with LECTERN_EXTRAS=tts and add a voice (docs/tts.md)."
                if not eng.piper_installed()
                else "No Piper voice installed yet (docs/tts.md)."
            ),
            "voices": piper_voices,
            "default_voice": piper_voices[0]["id"] if piper_voices else None,
            "model": "piper",
            "queues_without_runner": False,
        },
        {
            "id": eng.ELEVENLABS,
            "name": "ElevenLabs",
            "available": el_ok and user.role == Role.OWNER,
            "runs_on": "server" if el_ok else None,
            "note": (
                "Your ElevenLabs account. The book's text is sent to ElevenLabs."
                if el_ok
                else "Add your ElevenLabs API key in Admin → Integrations."
            ),
            "owner_only": True,
            "configured": el_ok,
            "voices": None,  # account voices: GET /api/integrations/elevenlabs/voices
            "default_voice": None,
            "model": el.DEFAULT_MODEL,
            "queues_without_runner": False,
        },
    ]
    recommended = next((e["id"] for e in engines[:2] if e["available"]), None)
    return {"engines": engines, "recommended": recommended}


def client(db: Session, settings: Settings, transport=None) -> el.Client:
    try:
        key = integrations.secret(db, settings, integrations.ELEVENLABS)
    except integrations.Undecryptable as exc:
        raise ServiceError(409, "key_undecryptable") from exc
    if key is None:
        raise ServiceError(409, "elevenlabs_not_configured")
    return el.Client(key, transport)


def eleven_rate(c: el.Client, model: str) -> float:
    for m in c.models():
        if m["id"] == model:
            return float(m["credits_per_character"])
    raise ServiceError(400, "unknown_model")


# --- estimate and enqueue ------------------------------------------------------------------------


def _selected(book: sc.Book, chapters: list[int] | None) -> list[sc.Chapter]:
    chosen = sc.select(book, chapters)
    if chapters is not None and len(chosen) != len(set(chapters)):
        raise ServiceError(400, "unknown_chapter")
    if not chosen:
        raise ServiceError(400, "no_chapters")
    return chosen


def estimate(
    db: Session, settings: Settings, work: Work, req: AudiobookIn, user: User, transport=None
) -> dict:
    book, _ = book_for(db, settings, work)
    chosen = _selected(book, req.chapters)
    chars = sum(c.characters for c in chosen)
    out: dict = {
        "chapters": len(chosen),
        "sentences": sum(len(c.sentences) for c in chosen),
        "characters": chars,
        "audio_s": round(chars / CHARS_PER_AUDIO_SECOND / req.speed),
        "eta": None,
        "elevenlabs": None,
    }
    measured = tj.measured_cps(db, req.engine)
    if measured:
        out["eta"] = {
            "seconds": round(chars / measured[0]),
            "basis": f"median of {measured[1]} measured run(s)",
        }
    if req.engine == eng.ELEVENLABS:
        if user.role != Role.OWNER:
            raise ServiceError(403, "owner_only")
        c = client(db, settings, transport)
        model = req.model or el.DEFAULT_MODEL
        rate = eleven_rate(c, model)
        quota = c.subscription()
        credits = round(chars * rate, 1)
        out["elevenlabs"] = {
            "model": model,
            "credits_per_character": rate,
            "credits": credits,
            "quota": quota,
            "fits": quota["characters_limit"] == 0 or credits <= quota["remaining"],
            "suggested_cap": round(credits * CAP_HEADROOM),
            "price_per_1000_credits": settings.elevenlabs_price_per_1000_credits,
            "cost": (
                round(credits / 1000 * settings.elevenlabs_price_per_1000_credits, 2)
                if settings.elevenlabs_price_per_1000_credits
                else None
            ),
        }
    return out


def enqueue(
    db: Session, settings: Settings, work: Work, req: AudiobookIn, user: User, transport=None
) -> Job:
    reason = eligibility(db, settings, work, user)
    if reason == "not_allowed":
        raise ServiceError(403, reason)
    if reason:
        raise ServiceError(409, reason)
    if tj.active_job(db, work.id) is not None:
        raise ServiceError(409, "already_running")
    book, epub_sha = book_for(db, settings, work)
    chosen = _selected(book, req.chapters)
    codec = req.codec or settings.tts_audio_codec
    payload: dict = {"engine": req.engine, "voice": req.voice, "speed": round(req.speed, 3)}
    if req.engine == eng.PIPER:
        voice = next((v for v in eng.piper_voices(settings) if v.id == req.voice), None)
        if voice is None:
            raise ServiceError(400, "unknown_voice")
        payload.update(
            model="piper",
            lang=voice.language or work.language or "en",
            sample_rate=voice.sample_rate,
        )
    elif req.engine == eng.KOKORO:
        if req.voice not in eng.KOKORO_VOICES:
            raise ServiceError(400, "unknown_voice")
        payload.update(
            model=eng.KOKORO_MODEL,
            lang=eng.kokoro_lang(req.voice),
            sample_rate=eng.KOKORO_SAMPLE_RATE,
        )
    else:
        payload.update(_eleven_payload(db, settings, work, req, user, chosen, transport))
    data = sc.script_json(chosen)
    sha = sc.script_sha256(data)
    tj.write_script(settings.data_dir, data, sha)
    sample_rate = payload.pop("sample_rate")
    payload.update(
        audio={
            "codec": codec,
            "bitrate_kbps": settings.tts_audio_bitrate_kbps,
            "sample_rate": sample_rate,
        },
        script_sha256=sha,
        chapters=len(chosen),
        sentences=sum(len(c.sentences) for c in chosen),
        characters=sum(c.characters for c in chosen),
        epub_sha256=epub_sha,
        spine_indices=[c.spine_index for c in chosen],
    )
    return tj.new_job(db, work, payload, user.id)


def _eleven_payload(db, settings, work, req: AudiobookIn, user: User, chosen, transport) -> dict:
    if user.role != Role.OWNER:
        raise ServiceError(403, "owner_only")
    if not 0.7 <= req.speed <= 1.2:
        raise ServiceError(400, "speed_out_of_range", {"min": 0.7, "max": 1.2})
    chars = sum(c.characters for c in chosen)
    conf = req.confirm
    if conf is None or not conf.send_text:
        raise ServiceError(409, "confirmation_required", {"characters": chars})
    c = client(db, settings, transport)
    model = req.model or el.DEFAULT_MODEL
    rate = eleven_rate(c, model)
    credits = round(chars * rate, 1)
    if conf.characters != chars or abs(conf.credits - credits) > 0.5:
        # The person agreed to a different amount (the selection or the price changed).
        raise ServiceError(409, "estimate_changed", {"characters": chars, "credits": credits})
    if conf.cap_credits + 1e-6 < credits:
        raise ServiceError(400, "cap_below_estimate", {"credits": credits})
    quota = c.subscription()
    if quota["characters_limit"] and credits > quota["remaining"]:
        raise ServiceError(
            409, "quota_insufficient", {"credits": credits, "remaining": quota["remaining"]}
        )
    vs = req.voice_settings.model_dump(exclude_none=True) if req.voice_settings else {}
    if abs(req.speed - 1.0) > 1e-6:
        vs["speed"] = round(req.speed, 3)
    return {
        "model": model,
        "lang": work.language or "en",
        "sample_rate": el.SAMPLE_RATE,
        "voice_settings": vs or None,
        "credits_per_character": rate,
        "credits": credits,
        "cap_credits": round(conf.cap_credits, 1),
        "cap_group": None,  # set to the job's id by new_job
    }


def retry(db: Session, settings: Settings, work: Work, user: User) -> Job:
    """Queue the last failed or cancelled narration again, with the same settings and cap.
    What the earlier job finished (chapter files) is carried over, so it resumes."""
    if tj.active_job(db, work.id) is not None:
        raise ServiceError(409, "already_running")
    last = tj.latest_job(db, work.id)
    if last is None or last.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
        raise ServiceError(409, "nothing_to_retry")
    payload = dict(last.payload or {})
    if payload.get("engine") == eng.ELEVENLABS and user.role != Role.OWNER:
        raise ServiceError(403, "owner_only")
    reason = eligibility(db, settings, work, user)
    if reason:
        raise ServiceError(409 if reason != "not_allowed" else 403, reason)
    found = work_epub(db, settings.library_dir, work)
    if found is None or found[0].sha256 != payload.get("epub_sha256"):
        raise ServiceError(409, "text_changed")
    job = tj.new_job(db, work, payload, user.id)
    old = queue.artifacts_dir(settings.data_dir, last.id)
    if old.is_dir():
        shutil.copytree(old, queue.artifacts_dir(settings.data_dir, job.id), dirs_exist_ok=True)
    return job


def preview_text(db: Session, settings: Settings, work: Work | None, text: str | None) -> str:
    if text and text.strip():
        return " ".join(text.split())[:300]
    if work is not None:
        try:
            book, _ = book_for(db, settings, work)
        except ServiceError:
            book = None
        if book and book.chapters:
            # first real paragraph, skipping title-like lines
            for ch in book.chapters:
                lines = [s.text for s in ch.sentences if len(s.text) > 40]
                if lines:
                    return " ".join(lines[:2])[:300]
    return "This is how this voice sounds when it reads your book aloud."
