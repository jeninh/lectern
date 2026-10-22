"""Generated audiobooks (docs/tts.md): script, synthesis-native timings, the edition, the
ElevenLabs integration (encrypted key, confirmation, spending cap, retry/resume, outbound
origin), and the queue rules (cancel, retry, local-runner claim)."""

from __future__ import annotations

import base64
import json
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.conftest import (
    api_delete,
    api_post,
    drain_inbox,
    login,
    make_user,
    needs_ffmpeg,
    put_json,
)

from lectern import queue
from lectern.db import session_factory
from lectern.models import (
    Alignment,
    AudioTrack,
    AuditLog,
    GeneratedAudiobook,
    Integration,
    IntegrationUsage,
    Job,
    JobStatus,
    Sentence,
)
from lectern.tts import elevenlabs as el
from lectern.tts import jobs as tj
from lectern.tts import script as sc
from lectern.tts.runner import TTSRunner

CHAPTERS = [
    ("Chapter One", ["The first chapter begins here.", "It has a second sentence!"],
     ["A new paragraph follows."]),
    ("Chapter Two", ["Only one sentence in this chapter."], []),
    ("Chapter Three", ["Third chapter, first line.", "And the last line of the book."], []),
]  # fmt: skip
SR = 16000
SAMPLES_PER_CHAR = 100  # the fake engine: 100 samples per character of the sentence


def make_book(path: Path) -> Path:
    """A three-chapter EPUB 3 with a nav TOC; each chapter is an <h1> plus paragraphs."""
    opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">urn:uuid:tts-book</dc:identifier>
    <dc:title>Narrated Book</dc:title><dc:creator>Ada Writer</dc:creator><dc:language>en</dc:language></metadata>
  <manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/>
    <item id="c3" href="c3.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="c1"/><itemref idref="c2"/><itemref idref="c3"/></spine>
</package>"""
    nav = "".join(
        f"<li><a href='c{i + 1}.xhtml'>{t}</a></li>" for i, (t, _, _) in enumerate(CHAPTERS)
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>
<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""",
        )
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/nav.xhtml", "<html xmlns='http://www.w3.org/1999/xhtml' xmlns:epub="
                    f"'http://www.idpf.org/2007/ops'><body><nav epub:type='toc'><ol>{nav}</ol></nav></body></html>")  # fmt: skip
        for i, (title, para1, para2) in enumerate(CHAPTERS):
            body = f"<h1>{title}</h1><p>{' '.join(para1)}</p>" + (
                f"<p>{' '.join(para2)}</p>" if para2 else ""
            )
            zf.writestr(f"OEBPS/c{i + 1}.xhtml",
                        f"<html xmlns='http://www.w3.org/1999/xhtml'><body>{body}</body></html>")  # fmt: skip
    return path


class FakeSynth:
    sample_rate = SR

    def __init__(self) -> None:
        self.calls: list[str] = []

    def sentence(self, text: str) -> bytes:
        self.calls.append(text)
        return b"\x10\x00" * (len(text) * SAMPLES_PER_CHAR)


def _work(settings, owner, client, tmp_path) -> int:
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    make_book(settings.inbox_path / "book.epub")
    drain_inbox(settings)
    r = client.get("/api/library")
    return r.json()["works"][0]["id"] if "works" in r.json() else r.json()["items"][0]["id"]


def _runner(settings, engines, synth=None, transport=None) -> TTSRunner:
    return TTSRunner(session_factory(), settings, synth_factory=lambda e, p, s: synth,
                     engines=lambda db: set(engines), eleven_transport=transport,
                     eleven_sleep=lambda _s: None)  # fmt: skip


def _piper_voice(settings) -> None:
    """The catalog and enqueue look Piper voices up on disk; the fake synth replaces the model."""
    d = settings.data_dir / "tts" / "piper"
    d.mkdir(parents=True, exist_ok=True)
    (d / "en_US-test-low.onnx").write_bytes(b"not a model")
    (d / "en_US-test-low.onnx.json").write_text(
        json.dumps({"audio": {"sample_rate": SR, "quality": "low"}, "language": {"code": "en_US"}})
    )


# --- script ------------------------------------------------------------------------------------


def test_script_chapters_titles_and_pauses(tmp_path):
    book = sc.read_book(make_book(tmp_path / "b.epub"))
    assert [c.title for c in book.chapters] == ["Chapter One", "Chapter Two", "Chapter Three"]
    one = book.chapters[0]
    texts = [s.text for s in one.sentences]
    assert texts == ["Chapter One", *CHAPTERS[0][1], *CHAPTERS[0][2]]
    pauses = [s.pause_s for s in one.sentences]
    # heading, sentence inside a paragraph, end of paragraph, end of chapter
    assert pauses == [
        sc.PAUSE_HEADING_S,
        sc.PAUSE_SENTENCE_S,
        sc.PAUSE_PARAGRAPH_S,
        sc.PAUSE_PARAGRAPH_S,
    ]
    # n is the global sentence number of the work's sentence list
    assert [s.n for c in book.chapters for s in c.sentences] == list(range(len(book.sentences)))
    data = sc.script_json(sc.select(book, [2, 0]))
    doc = json.loads(data)
    assert [c["title"] for c in doc["chapters"]] == ["Chapter One", "Chapter Three"]
    assert [c["index"] for c in doc["chapters"]] == [0, 1]
    assert data == sc.script_json(sc.select(book, [0, 2]))  # canonical: same hash


# --- Piper (fake engine) end to end -----------------------------------------------------------


@needs_ffmpeg
def test_piper_audiobook_installs_chaptered_edition_with_exact_timings(
    client, owner, settings, tmp_path
):
    work_id = _work(settings, owner, client, tmp_path)
    _piper_voice(settings)
    state = client.get(f"/api/works/{work_id}/audiobook").json()
    assert state["eligible"] and len(state["chapters"]) == 3
    r = api_post(client, f"/api/works/{work_id}/audiobook", owner["csrf"],
                 {"engine": "piper", "voice": "en_US-test-low", "chapters": [0, 2]})  # fmt: skip
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["capability"] == "tts-piper" and job["chapters"] == 2
    # a second request while one is active is refused
    again = api_post(client, f"/api/works/{work_id}/audiobook", owner["csrf"],
                     {"engine": "piper", "voice": "en_US-test-low"})  # fmt: skip
    assert again.status_code == 409 and again.json()["detail"] == "already_running"

    synth = FakeSynth()
    assert _runner(settings, {"piper"}, synth).run_once()

    with session_factory()() as db:
        j = db.get(Job, job["id"])
        assert j.status == JobStatus.SUCCEEDED, j.error
        tracks = db.scalars(select(AudioTrack).where(AudioTrack.work_id == work_id)
                            .order_by(AudioTrack.position)).all()  # fmt: skip
        assert [t.title for t in tracks] == ["Chapter One", "Chapter Three"]
        assert all(
            t.rel_path.startswith("audio/D01-T00") and t.rel_path.endswith(".m4a") for t in tracks
        )
        sentences = db.scalars(
            select(Sentence).where(Sentence.work_id == work_id).order_by(Sentence.n)
        ).all()
        alignment = db.scalar(select(Alignment).where(Alignment.work_id == work_id))
        assert alignment.status.value == "succeeded" and alignment.model.startswith("tts-piper")
        assert (
            db.scalar(
                select(GeneratedAudiobook).where(GeneratedAudiobook.work_id == work_id)
            ).chapters
            == 2
        )

    book = sc.read_book(next((settings.library_dir).rglob("*.epub")))
    # Expected times, computed from the fake engine's sample counts, not from the code.
    expected: dict[int, tuple[float, float]] = {}
    for ch, track in zip([book.chapters[0], book.chapters[2]], tracks, strict=True):
        pos = 0
        for s in ch.sentences:
            n = len(s.text) * SAMPLES_PER_CHAR
            expected[s.n] = (track.offset_s + pos / SR, track.offset_s + (pos + n) / SR)
            pos += n + round(s.pause_s * SR)
        # AAC keeps the duration (within a frame)
        assert abs(track.duration_s - pos / SR) < 0.1
    middle = {s.n for s in book.chapters[1].sentences}
    for s in sentences:
        if s.n in middle:
            assert not s.matched and s.audio_start_s is None  # not narrated: no time at all
        else:
            assert s.matched
            assert s.audio_start_s == pytest.approx(expected[s.n][0], abs=0.002)
            assert s.audio_end_s == pytest.approx(expected[s.n][1], abs=0.002)
    # second track starts after the first: offsets laid end to end
    assert tracks[1].offset_s == pytest.approx(tracks[0].duration_s, abs=0.01)
    # read-along works without any alignment job
    rd = client.get(f"/api/works/{work_id}/read-aloud").json()
    assert rd["readalong"] and rd["alignment"]["source"] == "synthesis"
    assert "low_coverage" not in json.dumps(rd["alignment"])
    with session_factory()() as db:
        assert not db.scalars(select(Job).where(Job.kind == "align")).all()
    # the work now has audio: no second generated edition on top of it
    assert client.get(f"/api/works/{work_id}/audiobook").json()["reason"] == "has_audio"


@needs_ffmpeg
def test_retry_resumes_finished_chapters(client, owner, settings, tmp_path):
    work_id = _work(settings, owner, client, tmp_path)
    _piper_voice(settings)
    job_id = api_post(client, f"/api/works/{work_id}/audiobook", owner["csrf"],
                      {"engine": "piper", "voice": "en_US-test-low"}).json()["id"]  # fmt: skip

    class Flaky(FakeSynth):
        def sentence(self, text):
            if text.startswith("Third"):
                raise RuntimeError("engine crashed")
            return super().sentence(text)

    first = Flaky()
    _runner(settings, {"piper"}, first).run_once()
    with session_factory()() as db:
        j = db.get(Job, job_id)
        assert j.status == JobStatus.QUEUED and "engine crashed" in j.error  # retried with backoff
        db.query(Job).filter(Job.id == job_id).update(
            {"max_attempts": 1}
        )  # pretend it was the last
        j.status = JobStatus.FAILED
        db.commit()
    r = api_post(client, f"/api/works/{work_id}/audiobook/retry", owner["csrf"])
    assert r.status_code == 202, r.text
    second = FakeSynth()
    assert _runner(settings, {"piper"}, second).run_once()
    with session_factory()() as db:
        assert db.get(Job, r.json()["id"]).status == JobStatus.SUCCEEDED
    # chapters one and two were finished by the first job and not narrated again
    assert all(not t.startswith(("Chapter One", "The first", "Only one")) for t in second.calls)
    assert second.calls[0] == "Chapter Three"


def test_an_engine_that_exits_does_not_kill_the_servers_runner(client, owner, settings, tmp_path):
    """Kokoro in the server (LECTERN_TTS_KOKORO_LOCAL=1): spaCy's model download calling
    sys.exit() used to end the runner thread silently, lease held."""
    work_id = _work(settings, owner, client, tmp_path)
    _piper_voice(settings)
    job_id = api_post(client, f"/api/works/{work_id}/audiobook", owner["csrf"],
                      {"engine": "piper", "voice": "en_US-test-low"}).json()["id"]  # fmt: skip

    def exits(engine, payload, settings):
        raise SystemExit(2)

    runner = TTSRunner(session_factory(), settings, synth_factory=exits,
                       engines=lambda db: {"piper"})  # fmt: skip
    assert runner.run_once()
    with session_factory()() as db:
        j = db.get(Job, job_id)
        assert j.status == JobStatus.QUEUED and j.crashes == 1
        assert j.error == "the engine called sys.exit(2)"
        j.run_after = j.created_at  # the backoff passes
        db.commit()
    assert runner.run_once()
    with session_factory()() as db:
        j = db.get(Job, job_id)
        assert j.status == JobStatus.FAILED and j.attempts == 2
        assert j.error.startswith("crashed the worker 2 times; last error: the engine called")
    state = client.get(f"/api/works/{work_id}/audiobook").json()
    assert state["job"]["error"].startswith("crashed the worker 2 times")


def test_cancel_keeps_alignment_and_stops_runner(client, owner, settings, tmp_path):
    work_id = _work(settings, owner, client, tmp_path)
    _piper_voice(settings)
    job_id = api_post(client, f"/api/works/{work_id}/audiobook", owner["csrf"],
                      {"engine": "piper", "voice": "en_US-test-low"}).json()["id"]  # fmt: skip
    r = api_post(client, f"/api/jobs/{job_id}/cancel", owner["csrf"])
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert r.json()["kind"] == "tts" and r.json()["engine"] == "piper"
    listed = client.get("/api/jobs?kind=tts").json()["jobs"]
    assert [j["id"] for j in listed] == [job_id]
    assert not _runner(settings, {"piper"}, FakeSynth()).run_once()


def test_local_claim_routes_by_engine_and_defers(client, owner, settings, tmp_path):
    work_id = _work(settings, owner, client, tmp_path)
    r = api_post(client, f"/api/works/{work_id}/audiobook", owner["csrf"],
                 {"engine": "kokoro", "voice": "af_heart"})  # fmt: skip
    assert r.status_code == 202 and r.json()["capability"] == "tts-kokoro"
    with session_factory()() as db:
        assert tj.claim_local(db, {"piper"}) is None  # wrong engine
        assert (
            tj.claim_local(db, {"kokoro"}, defer={"kokoro"}, defer_s=600) is None
        )  # worker online
        claimed = tj.claim_local(db, {"kokoro"})
        assert claimed is not None and claimed[0].status == JobStatus.LEASED
        assert tj.claim_local(db, {"kokoro"}) is None  # one lease


def test_readers_cannot_make_audiobooks(client, owner, settings, tmp_path):
    work_id = _work(settings, owner, client, tmp_path)
    make_user(client, owner["csrf"], "rita", "reader")
    client.cookies.clear()
    csrf = login(client, "rita", "rita-correct horse")
    assert client.get(f"/api/works/{work_id}/audiobook").status_code in (403, 404)
    r = api_post(
        client, f"/api/works/{work_id}/audiobook", csrf, {"engine": "kokoro", "voice": "af_heart"}
    )
    assert r.status_code in (403, 404)


# --- ElevenLabs ----------------------------------------------------------------------------------

KEY = "sk_test_0123456789abcdefWXYZ"


class FakeEleven:
    """api.elevenlabs.io stand-in: 0.01 s (240 samples) per character, per-character times."""

    def __init__(self, limit=100_000, fail_first: int = 0) -> None:
        self.requests: list[tuple[str, str, dict]] = []
        self.limit = limit
        self.fail_first = fail_first

    def __call__(self, method, path, headers, body):
        assert path.startswith("/v1/")
        self.requests.append((method, path, json.loads(body) if body else {}))
        if headers.get("xi-api-key") != KEY:
            return 401, {}, b'{"detail": {"status": "invalid_api_key"}}'
        if path == "/v1/user/subscription":
            return 200, {}, json.dumps({"tier": "creator", "character_count": 0,
                                        "character_limit": self.limit}).encode()  # fmt: skip
        if path == "/v1/models":
            return 200, {}, json.dumps([{"model_id": "eleven_multilingual_v2", "name": "Multilingual v2",
                                         "can_do_text_to_speech": True,
                                         "model_rates": {"character_cost_multiplier": 1.0}}]).encode()  # fmt: skip
        if path == "/v1/voices":
            return 200, {}, json.dumps({"voices": [{"voice_id": "v1", "name": "Rachel"}]}).encode()
        if "/with-timestamps" in path:
            if self.fail_first:
                self.fail_first -= 1
                return 429, {"Retry-After": "1"}, b'{"detail": "slow down"}'
            text = json.loads(body)["text"]
            pcm = b"\x01\x00" * (240 * len(text))
            return 200, {"request-id": "r"}, json.dumps({
                "audio_base64": base64.b64encode(pcm).decode(),
                "alignment": {"characters": list(text),
                              "character_start_times_seconds": [i * 0.01 for i in range(len(text))],
                              "character_end_times_seconds": [(i + 1) * 0.01 for i in range(len(text))]},
            }).encode()  # fmt: skip
        return 404, {}, b"{}"

    def speech_texts(self):
        return [b["text"] for m, p, b in self.requests if "/with-timestamps" in p]


def _eleven(client, owner, fake):
    client.app.state.elevenlabs_transport = fake
    r = put_json(client, "/api/integrations/elevenlabs", owner["csrf"], {"api_key": KEY})
    assert r.status_code == 200, r.text
    return r.json()


def test_key_encrypted_never_redisplayed_tested_and_revoked(client, owner, settings):
    fake = FakeEleven()
    client.app.state.elevenlabs_transport = fake
    bad = put_json(
        client, "/api/integrations/elevenlabs", owner["csrf"], {"api_key": "sk_wrong_key_123"}
    )
    assert bad.status_code == 400 and bad.json()["detail"] == "elevenlabs_key_rejected"
    saved = _eleven(client, owner, fake)
    assert saved["configured"] and saved["key_hint"] == "…WXYZ" and saved["last_test_ok"]
    assert saved["endpoint"] == "https://api.elevenlabs.io"
    for r in (
        client.get("/api/integrations/elevenlabs"),
        api_post(client, "/api/integrations/elevenlabs/test", owner["csrf"]),
    ):
        assert r.status_code == 200 and KEY not in r.text and KEY[:-4] not in r.text
    with session_factory()() as db:
        row = db.scalar(select(Integration))
        assert KEY.encode() not in row.ciphertext
        raw = (settings.data_dir / "lectern.db").read_bytes()
        assert KEY.encode() not in raw
    r = api_delete(client, "/api/integrations/elevenlabs", owner["csrf"])
    assert r.json()["configured"] is False and r.json()["revoked_at"]
    with session_factory()() as db:
        assert db.scalar(select(Integration)).ciphertext is None
        actions = [a.action for a in db.scalars(select(AuditLog))]
    assert {
        "integration.elevenlabs.set",
        "integration.elevenlabs.test",
        "integration.elevenlabs.revoke",
    } <= set(actions)
    assert client.get("/api/integrations/elevenlabs/voices").status_code == 409


def test_member_cannot_touch_the_integration(client, owner):
    make_user(client, owner["csrf"], "mo", "member")
    client.cookies.clear()
    csrf = login(client, "mo", "mo-correct horse")
    assert client.get("/api/integrations/elevenlabs").status_code == 403
    assert (
        put_json(client, "/api/integrations/elevenlabs", csrf, {"api_key": KEY}).status_code == 403
    )


def test_no_outbound_request_without_an_explicit_action(client, owner, settings, tmp_path):
    fake = FakeEleven()
    _eleven(client, owner, fake)
    fake.requests.clear()
    work_id = _work(settings, owner, client, tmp_path)
    client.get(f"/api/works/{work_id}/audiobook")
    client.get("/api/integrations/elevenlabs")
    client.get("/api/jobs")
    assert fake.requests == []
    with pytest.raises(el.ElevenLabsError):
        el.https_transport("GET", "//evil.example/v1/x", {}, None)
    with pytest.raises(el.ElevenLabsError):
        el.https_transport("GET", "@evil.example/", {}, None)


@needs_ffmpeg
def test_elevenlabs_needs_confirmation_and_stops_at_the_cap_then_resumes_without_rebilling(
    client, owner, settings, tmp_path
):
    fake = FakeEleven(fail_first=1)
    _eleven(client, owner, fake)
    work_id = _work(settings, owner, client, tmp_path)
    req = {"engine": "elevenlabs", "voice": "v1"}
    est = api_post(client, f"/api/works/{work_id}/audiobook/estimate", owner["csrf"], req).json()
    chars = est["characters"]
    book = sc.read_book(next(settings.library_dir.rglob("*.epub")))
    assert chars == sum(c.characters for c in book.chapters)
    assert (
        est["elevenlabs"]["credits"] == chars and est["elevenlabs"]["quota"]["remaining"] == 100_000
    )

    r = api_post(client, f"/api/works/{work_id}/audiobook", owner["csrf"], req)
    assert r.status_code == 409 and r.json()["detail"] == "confirmation_required"
    stale = {
        **req,
        "confirm": {
            "characters": chars - 5,
            "credits": chars - 5,
            "cap_credits": chars,
            "send_text": True,
        },
    }
    assert (
        api_post(client, f"/api/works/{work_id}/audiobook", owner["csrf"], stale).json()["detail"]
        == "estimate_changed"
    )
    low = {
        **req,
        "confirm": {
            "characters": chars,
            "credits": chars,
            "cap_credits": chars - 1,
            "send_text": True,
        },
    }
    assert (
        api_post(client, f"/api/works/{work_id}/audiobook", owner["csrf"], low).json()["detail"]
        == "cap_below_estimate"
    )

    # Confirmed, but the cap only covers the first chapter: it must stop before chapter two.
    first = book.chapters[0].characters
    capped = {
        **req,
        "confirm": {"characters": chars, "credits": chars, "cap_credits": chars, "send_text": True},
    }
    r = api_post(client, f"/api/works/{work_id}/audiobook", owner["csrf"], capped)
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    assert r.json()["capability"] == "tts-elevenlabs"
    with session_factory()() as db:  # simulate the owner lowering the cap mid-way
        j = db.get(Job, job_id)
        j.payload = {**j.payload, "cap_credits": first + 1}
        db.commit()
    _runner(settings, {"elevenlabs"}, transport=fake).run_once()
    with session_factory()() as db:
        j = db.get(Job, job_id)
        assert j.status == JobStatus.FAILED and "cap" in j.error
        spent = tj.spent_credits(db, job_id)
    assert spent <= first + 1
    billed_once = list(fake.speech_texts())
    assert len(billed_once) == 1 + 1  # one 429 retried, then chapter one's single chunk
    assert billed_once[0] == billed_once[1]

    # Raise the cap and Retry: chapter one is not requested (or billed) again.
    with session_factory()() as db:
        j = db.get(Job, job_id)
        j.payload = {**j.payload, "cap_credits": chars}
        db.commit()
    r = api_post(client, f"/api/works/{work_id}/audiobook/retry", owner["csrf"])
    assert r.status_code == 202, r.text
    fake.requests.clear()
    _runner(settings, {"elevenlabs"}, transport=fake).run_once()
    with session_factory()() as db:
        new = db.get(Job, r.json()["id"])
        assert new.status == JobStatus.SUCCEEDED, new.error
        assert new.payload["cap_group"] == job_id  # one cap across the retry
        total = tj.spent_credits(db, job_id)
        usage = db.scalars(select(IntegrationUsage)).all()
        sentences = db.scalars(
            select(Sentence).where(Sentence.work_id == work_id).order_by(Sentence.n)
        ).all()
    assert all(book.chapters[0].sentences[0].text not in t for t in fake.speech_texts())
    assert total == pytest.approx(chars)  # every character billed exactly once
    assert sum(u.characters for u in usage) == chars
    # Times come from the service's character timestamps (0.01 s per character in a chunk).
    ch2 = book.chapters[1]
    s0 = next(s for s in sentences if s.n == ch2.sentences[0].n)
    track_offset = s0.audio_start_s
    assert s0.matched and s0.audio_end_s - track_offset == pytest.approx(
        len(ch2.sentences[0].text) * 0.01, abs=0.002
    )
    with session_factory()() as db:
        assert "integration.elevenlabs.use" in [a.action for a in db.scalars(select(AuditLog))]


def _confirmed_eleven_job(client, owner, settings, work_id) -> tuple[int, int]:
    req = {"engine": "elevenlabs", "voice": "v1"}
    est = api_post(client, f"/api/works/{work_id}/audiobook/estimate", owner["csrf"], req).json()
    chars = est["characters"]
    confirm = {"characters": chars, "credits": chars, "cap_credits": chars, "send_text": True}
    r = api_post(
        client, f"/api/works/{work_id}/audiobook", owner["csrf"], {**req, "confirm": confirm}
    )
    assert r.status_code == 202, r.text
    return r.json()["id"], chars


def test_elevenlabs_lost_response_is_never_resent_automatically(client, owner, settings, tmp_path):
    class LostFirst(FakeEleven):
        lost = 1

        def __call__(self, method, path, headers, body):
            out = super().__call__(method, path, headers, body)
            if "/with-timestamps" in path and self.lost:
                self.lost -= 1
                raise el.ElevenLabsError("network", "connection reset", retryable=True)
            return out  # (processed and billed by the service, response lost)

    fake = LostFirst()
    _eleven(client, owner, fake)
    work_id = _work(settings, owner, client, tmp_path)
    job_id, chars = _confirmed_eleven_job(client, owner, settings, work_id)
    _runner(settings, {"elevenlabs"}, transport=fake).run_once()
    assert len(fake.speech_texts()) == 1  # not retried by the client
    first_chunk = fake.speech_texts()[0]
    with session_factory()() as db:
        j = db.get(Job, job_id)
        assert j.status == JobStatus.FAILED and "may have charged" in j.error
        (row,) = db.scalars(select(IntegrationUsage)).all()
        assert row.state == "uncertain" and tj.spent_credits(db, job_id) == len(first_chunk)
        # an automatic re-run of the same job (a crash, a reclaim) does not send it again
        j.status, j.finished_at, j.attempts = JobStatus.QUEUED, None, 0
        db.commit()
    fake.requests.clear()
    _runner(settings, {"elevenlabs"}, transport=fake).run_once()
    assert fake.speech_texts() == []
    with session_factory()() as db:
        assert db.get(Job, job_id).status == JobStatus.FAILED
        j = db.get(Job, job_id)  # the owner raises the cap for the possible double charge
        j.payload = {**j.payload, "cap_credits": chars + len(first_chunk)}
        db.commit()
    # the owner's Retry is the explicit decision to send it again, within the cap
    r = api_post(client, f"/api/works/{work_id}/audiobook/retry", owner["csrf"])
    assert r.status_code == 202, r.text
    _runner(settings, {"elevenlabs"}, transport=fake).run_once()
    with session_factory()() as db:
        assert db.get(Job, r.json()["id"]).status == JobStatus.SUCCEEDED
        assert tj.spent_credits(db, job_id) == pytest.approx(chars + len(first_chunk))
    assert fake.speech_texts()[0] == first_chunk


def test_removing_the_key_stops_a_running_narration(client, owner, settings, tmp_path):
    from lectern import integrations

    class RevokeAfterFirst(FakeEleven):
        def __call__(self, method, path, headers, body):
            out = super().__call__(method, path, headers, body)
            if "/with-timestamps" in path:
                with session_factory()() as db:  # the owner removes the key meanwhile
                    integrations.revoke(db, integrations.ELEVENLABS)
                    db.commit()
            return out

    fake = RevokeAfterFirst()
    _eleven(client, owner, fake)
    work_id = _work(settings, owner, client, tmp_path)
    job_id, _chars = _confirmed_eleven_job(client, owner, settings, work_id)
    _runner(settings, {"elevenlabs"}, transport=fake).run_once()
    assert len(fake.speech_texts()) == 1  # the first chunk only
    with session_factory()() as db:
        j = db.get(Job, job_id)
        assert j.status == JobStatus.FAILED and "removed" in j.error
        assert [u.state for u in db.scalars(select(IntegrationUsage))] == ["billed"]


def test_member_cannot_use_elevenlabs(client, owner, settings, tmp_path):
    _eleven(client, owner, FakeEleven())
    work_id = _work(settings, owner, client, tmp_path)
    make_user(client, owner["csrf"], "mo", "member")
    client.cookies.clear()
    csrf = login(client, "mo", "mo-correct horse")
    body = {"engine": "elevenlabs", "voice": "v1",
            "confirm": {"characters": 1, "credits": 1, "cap_credits": 1, "send_text": True}}  # fmt: skip
    r = api_post(client, f"/api/works/{work_id}/audiobook", csrf, body)
    assert r.status_code == 403 and r.json()["detail"] == "owner_only"


def test_preview_is_billed_and_audited(client, owner, settings, tmp_path):
    fake = FakeEleven()
    _eleven(client, owner, fake)
    r = api_post(
        client,
        "/api/integrations/elevenlabs/preview",
        owner["csrf"],
        {"voice": "v1", "text": "Hello there."},
    )
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert r.content[:4] == b"RIFF"
    with session_factory()() as db:
        u = db.scalar(select(IntegrationUsage).where(IntegrationUsage.kind == "preview"))
        assert u.characters == len("Hello there.")


@needs_ffmpeg
def test_result_is_validated_pinned_and_fenced_by_its_lease(client, owner, settings, tmp_path):
    from lectern.align.jobs import LeaseLost, ResultError
    from lectern.tts import audio as tts_audio

    work_id = _work(settings, owner, client, tmp_path)
    job_id = api_post(client, f"/api/works/{work_id}/audiobook", owner["csrf"],
                      {"engine": "kokoro", "voice": "af_heart", "chapters": [1]}).json()["id"]  # fmt: skip
    art = queue.artifacts_dir(settings.data_dir, job_id)
    art.mkdir(parents=True)
    pcm = tmp_path / "five.pcm"
    pcm.write_bytes(b"\x10\x00" * SR * 5)  # 5 s
    good = tts_audio.encode("ffmpeg", pcm, art / "chapter-0000.m4a", sample_rate=SR,
                            codec="aac", bitrate_kbps=32, tags={})  # fmt: skip
    (art / "chapter-0099.m4a").write_bytes(b"not audio at all")
    import hashlib

    junk = hashlib.sha256(b"not audio at all").hexdigest()
    with session_factory()() as db:
        job, _token = tj.claim_local(db, {"kokoro"})
        script = tj.load_job_script(settings.data_dir, job.payload)
        ns = [x["n"] for x in script["chapters"][0]["sentences"]]
        every = [{"n": m, "start_s": i * 0.5, "end_s": i * 0.5 + 0.4} for i, m in enumerate(ns)]

        def body(**over):
            ch = {"index": 0, "title": "x", "artifact": "chapter-0000.m4a", "sha256": good,
                  "duration_s": 5.0, "sentences": every}  # fmt: skip
            ch.update(over)
            return json.dumps({"version": 1, "engine": "kokoro", "voice": "af_heart", "codec": "aac",
                               "chapters": [ch]}).encode()  # fmt: skip

        bad = {
            "missing_artifact": body(sha256="0" * 64),
            "invalid_result": body(sentences=every[:-1]),
            "bad_audio": body(artifact="chapter-0099.m4a", sha256=junk),  # hash right, not audio
        }
        for code, b in {**bad, "too_long": body(sentences=[*every[:-1], {**every[-1], "end_s": 9}]),
                        "duration": body(duration_s=9.0)}.items():  # fmt: skip
            with pytest.raises(ResultError) as exc:
                tj.apply_result(db, settings, job, b, art)
            assert exc.value.status == 422, code
            if code in bad:
                assert exc.value.code == code

        # The lease is reclaimed and claimed again while this result is in flight: the stale
        # result must publish nothing, even though the job is active again.
        held = job.lease_token_hash
        with session_factory()() as other:
            other.query(Job).filter(Job.id == job_id).update({"lease_token_hash": "f" * 64})
            other.commit()
        with pytest.raises(LeaseLost):
            tj.apply_result(db, settings, job, body(), art)
        db.rollback()
        assert not db.scalars(select(AudioTrack).where(AudioTrack.work_id == work_id)).all()
        assert not list((settings.library_dir / ".lectern-staging").glob("*/*"))

        # The holder of the lease publishes the bytes it uploaded.
        db.query(Job).filter(Job.id == job_id).update({"lease_token_hash": held})
        db.commit()
        job = db.get(Job, job_id)
        assert tj.apply_result(db, settings, job, body(), art)["status"] == "succeeded"
        (track,) = db.scalars(select(AudioTrack).where(AudioTrack.work_id == work_id)).all()
        assert track.sha256 == good and abs(track.duration_s - 5.0) < 0.1


def test_elevenlabs_client_retries_with_backoff_and_does_not_retry_bad_keys():
    sleeps: list[float] = []
    fake = FakeEleven(fail_first=2)
    c = el.Client(KEY, fake, sleep=sleeps.append)
    sp = c.speech("v1", "Hi.", model_id="eleven_multilingual_v2")
    assert len(sp.char_start_s) == 3 and sleeps == [1.0, 1.0]  # honours Retry-After
    bad = el.Client("sk_nope_nope", fake, sleep=sleeps.append)
    with pytest.raises(el.ElevenLabsError) as exc:
        bad.subscription()
    assert exc.value.code == "key_rejected" and len(sleeps) == 2


def test_chunks_keep_sentences_whole_and_break_at_paragraphs():
    s = [{"n": i, "text": "x" * 900, "pause_s": 0.3} for i in range(5)]
    groups = el.chunks(s, max_chars=2000)
    assert [len(g) for g in groups] == [2, 2, 1]
    s[0]["pause_s"] = 0.7  # end of a short paragraph, not yet half full: keeps going
    s[1]["pause_s"] = 0.7  # half full: break
    assert [len(g) for g in el.chunks(s, max_chars=2000)][0] == 2


def test_origin_is_fixed():
    assert el.ORIGIN == "https://api.elevenlabs.io"


def test_toc_entry_pointing_mid_file_does_not_name_the_chapter():
    # Gutenberg: the licence's TOC entry points at the end of the file the story is in.
    book = sc.read_book(Path(__file__).parent / "fixtures" / "magi" / "the-gift-of-the-magi.epub")
    story = next(
        c for c in book.chapters if "One dollar and eighty-seven cents." in c.sentences[0].text
    )
    assert "LICENSE" not in story.title.upper()


@needs_ffmpeg
def test_mac_worker_kokoro_job_through_worker_routes(client, owner, settings, tmp_path, app):
    """The Mac worker's real client (RemoteWorker + KokoroRunner) against the real ``tts``
    handler: it claims ``tts-kokoro``, downloads the script, uploads chapters, and its result
    becomes the chaptered edition with the synthesis timings."""
    np = pytest.importorskip("numpy")
    from fastapi.testclient import TestClient

    from lectern.align.worker import RemoteWorker
    from lectern.worker.runners import KokoroRunner

    work_id = _work(settings, owner, client, tmp_path)
    r = api_post(client, f"/api/works/{work_id}/audiobook", owner["csrf"],
                 {"engine": "kokoro", "voice": "af_heart", "chapters": [0, 1]})  # fmt: skip
    assert r.status_code == 202, r.text
    key = api_post(client, "/api/worker-credentials", owner["csrf"], {"label": "mac"}).json()
    http = TestClient(app, headers={"Authorization": f"Bearer {key['secret']}"})
    texts: list[str] = []

    def synth_factory(voice, speed, lang):
        assert (voice, lang) == ("af_heart", "en-us")

        def synth(text):
            texts.append(text)
            return np.full(len(text) * 2400, 0.1, dtype=np.float32)  # 0.1 s per character

        return synth

    worker = RemoteWorker("http://testserver", key["secret"], client=http,
                          runners=[KokoroRunner(synth_factory)])  # fmt: skip
    assert worker.run_once()

    with session_factory()() as db:
        job = db.get(Job, r.json()["id"])
        assert job.status == JobStatus.SUCCEEDED, job.error
        assert job.leased_by_id is not None  # the worker ran it, not the server
        tracks = db.scalars(select(AudioTrack).where(AudioTrack.work_id == work_id)
                            .order_by(AudioTrack.position)).all()  # fmt: skip
        sentences = {
            s.n: s for s in db.scalars(select(Sentence).where(Sentence.work_id == work_id))
        }
    assert [t.title for t in tracks] == ["Chapter One", "Chapter Two"]
    book = sc.read_book(next(settings.library_dir.rglob("*.epub")))
    assert texts == [s.text for c in book.chapters[:2] for s in c.sentences]
    # expected from the fake engine (0.1 s per character plus each sentence's pause)
    for ch, track in zip(book.chapters[:2], tracks, strict=True):
        pos = 0.0
        for s in ch.sentences:
            end = pos + len(s.text) * 0.1
            got = sentences[s.n]
            assert got.matched
            assert got.audio_start_s == pytest.approx(track.offset_s + pos, abs=0.002)
            assert got.audio_end_s == pytest.approx(track.offset_s + end, abs=0.002)
            pos = end + s.pause_s
    assert not any(sentences[s.n].matched for s in book.chapters[2].sentences)
    rd = client.get(f"/api/works/{work_id}/read-aloud").json()
    assert rd["readalong"] and rd["alignment"]["source"] == "synthesis"
