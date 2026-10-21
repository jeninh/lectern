"""Worker capabilities, routing, enrollment, artifacts, and the desktop worker
(docs/contracts.md, "Capabilities and routing"; docs/mac-worker.md).

Everything runs against the real app over HTTP with a worker credential. Kokoro, MLX, and
macOS are replaced by small fakes at their boundaries (a synthesizer function, the
``mlx_whisper`` module, ``pmset`` output); ffmpeg is real.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import types
from datetime import timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.conftest import ORIGIN, api_post
from tests.test_jobs import claim, ingest_fixture, lease, request_align

from lectern import queue
from lectern.align import jobs
from lectern.align.mlx import MLXTranscriber, words_from_segments, worker_transcriber
from lectern.align.transcribe import Transcriber, Word
from lectern.align.worker import RemoteWorker
from lectern.db import session_factory
from lectern.jobs import find_variant
from lectern.models import Job, JobStatus, Work, WorkerCredential, utcnow
from lectern.worker import conditions as cond
from lectern.worker import config as wcfg
from lectern.worker import menubar
from lectern.worker.runners import KokoroRunner, TranscodeRunner, render_chapter

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def patch(client, url, csrf, body):
    return client.patch(url, json=body, headers={"x-csrf-token": csrf, "origin": ORIGIN})


@pytest.fixture
def work_id(client, owner, settings, tmp_path) -> int:
    return ingest_fixture(settings, tmp_path)


@pytest.fixture
def worker_key(client, owner) -> str:
    return api_post(client, "/api/worker-credentials", owner["csrf"], {"label": "mac"}).json()[
        "secret"
    ]


@pytest.fixture
def wc(app, worker_key) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {worker_key}"})


def add_job(work_id: int, kind: str, payload: dict, **kw) -> int:
    with session_factory()() as db:
        job = Job(kind=kind, work_id=work_id, status=JobStatus.QUEUED, payload=payload, **kw)
        db.add(job)
        db.commit()
        return job.id


# --- routing ----------------------------------------------------------------------------------


def test_capability_is_derived_from_kind_and_payload(client, owner, work_id):
    align = request_align(client, owner, work_id).json()
    assert align["capability"] == "align"
    tts = add_job(work_id, "tts", {"engine": "Kokoro"})
    with session_factory()() as db:
        assert db.get(Job, tts).capability == "tts-kokoro"


def test_an_old_worker_that_sends_only_kinds_still_claims_align(client, owner, work_id, wc):
    add_job(work_id, "tts", {"engine": "kokoro"})
    job_id = request_align(client, owner, work_id).json()["id"]
    r = wc.post("/api/worker/jobs/claim", json={"kinds": ["align"], "worker": "old"})
    assert r.status_code == 200 and r.json()["job"]["id"] == job_id
    assert set(r.json()["inputs"]) == {"epub", "tracks"}  # the align input shape is unchanged


def test_claim_rejects_malformed_capabilities(wc):
    r = wc.post("/api/worker/jobs/claim", json={"capabilities": ["Align; DROP"]})
    assert r.status_code == 422


def test_workers_list_shows_presence_claimable_and_running_job(client, owner, work_id, wc):
    request_align(client, owner, work_id)
    r = claim(wc, capabilities=["align", "tts-kokoro", "tts-elevenlabs"],
              info={"platform": "darwin-arm64", "engines": {"align": "mlx-whisper-small"}})  # fmt: skip
    headers = lease(r)
    wc.post(f"/api/worker/jobs/{r.json()['job']['id']}/heartbeat",
            json={"progress": 0.25, "message": "window 2"}, headers=headers)  # fmt: skip
    cred = client.get("/api/worker-credentials").json()["credentials"][0]
    assert cred["state"] == "busy"
    assert cred["claimable"] == ["align", "tts-kokoro"]  # ElevenLabs never leaves the server
    assert cred["info"]["platform"] == "darwin-arm64"
    assert cred["jobs"][0]["progress"] == 0.25 and cred["jobs"][0]["message"] == "window 2"


# --- Pi fallback: local runners defer to an online worker, then take over -------------------


def test_local_runner_defers_to_an_online_worker_then_takes_over(client, owner, work_id, wc):
    job_id = request_align(client, owner, work_id).json()["id"]
    assert wc.post("/api/worker/status", json={"state": "ready", "capabilities": ["align"]}
                   ).status_code == 200  # fmt: skip

    def local_claim():
        with session_factory()() as db:
            got = jobs.claim(db, kinds=["align"], worker="local", lease_s=600,
                             credential_id=None, defer_s=600)  # fmt: skip
            return got[0].id if got else None

    assert local_claim() is None  # the Mac is online and has align: leave it a while
    # the Mac goes on battery: it is no longer counted, the Pi takes the job
    wc.post("/api/worker/status", json={"state": "paused", "info": {"reason": "on battery"}})
    assert local_claim() == job_id
    with session_factory()() as db:
        jobs.release(db, db.get(Job, job_id))
    # back online, but the job has waited longer than the deferral: the Pi still runs it
    wc.post("/api/worker/status", json={"state": "ready", "capabilities": ["align"]})
    with session_factory()() as db:
        db.get(Job, job_id).created_at = utcnow() - timedelta(seconds=601)
        db.commit()
    assert local_claim() == job_id


def test_an_online_worker_without_the_capability_does_not_delay_the_pi(client, owner, work_id, wc):
    job_id = request_align(client, owner, work_id).json()["id"]
    wc.post("/api/worker/status", json={"state": "ready", "capabilities": ["tts-kokoro"]})
    with session_factory()() as db:
        got = jobs.claim(db, kinds=["align"], worker="local", lease_s=600, credential_id=None,
                         defer_s=600)  # fmt: skip
        assert got is not None and got[0].id == job_id


# --- enrollment (one-command install) ---------------------------------------------------------


def test_enrollment_code_is_single_use_and_yields_a_job_only_credential(client, owner, app):
    r = api_post(client, "/api/worker-enrollments", owner["csrf"],
                 {"label": "Jen's MacBook", "capabilities": ["align", "tts-kokoro"]})  # fmt: skip
    assert r.status_code == 201
    code, command = r.json()["code"], r.json()["command"]
    assert code.startswith("lwe_") and command.endswith(f"| sh -s -- --code {code}")
    assert "/worker/install.sh" in command
    anon = TestClient(app)
    got = anon.post("/api/worker/enroll", json={"code": code, "name": "mbp"})
    assert got.status_code == 201
    secret = got.json()["secret"]
    assert secret.startswith("lw_") and got.json()["capabilities"] == ["align", "tts-kokoro"]
    assert anon.post("/api/worker/enroll", json={"code": code}).status_code == 401  # used
    bearer = {"Authorization": f"Bearer {secret}"}
    assert (
        anon.post("/api/worker/status", json={"state": "ready"}, headers=bearer).status_code == 200
    )
    for url in ("/api/library", "/api/jobs", "/api/worker-credentials", "/opds"):
        assert anon.get(url, headers=bearer).status_code in (401, 403), url
    listed = client.get("/api/worker-enrollments").json()["enrollments"][0]
    assert listed["state"] == "used" and listed["credential_id"] is not None
    # revoking the credential ends it at once
    client.delete(f"/api/worker-credentials/{listed['credential_id']}",
                  headers={"x-csrf-token": owner["csrf"], "origin": ORIGIN})  # fmt: skip
    assert (
        anon.post("/api/worker/status", json={"state": "ready"}, headers=bearer).status_code == 401
    )


def test_expired_and_revoked_enrollment_codes_do_not_work(client, owner, app):
    anon = TestClient(app)
    first = api_post(client, "/api/worker-enrollments", owner["csrf"], {"label": "a"}).json()
    second = api_post(client, "/api/worker-enrollments", owner["csrf"], {"label": "b"}).json()
    client.delete(f"/api/worker-enrollments/{first['id']}",
                  headers={"x-csrf-token": owner["csrf"], "origin": ORIGIN})  # fmt: skip
    assert anon.post("/api/worker/enroll", json={"code": first["code"]}).status_code == 401
    from lectern.models import WorkerEnrollment

    with session_factory()() as db:
        db.get(WorkerEnrollment, second["id"]).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert anon.post("/api/worker/enroll", json={"code": second["code"]}).status_code == 401


def test_install_script_names_this_server_and_parses(client, settings):
    r = client.get("/worker/install.sh")
    assert r.status_code == 200
    assert f"SERVER={settings.base_url}" in r.text or f"SERVER='{settings.base_url}'" in r.text
    assert "__LECTERN" not in r.text
    if shutil.which("sh"):
        assert subprocess.run(["sh", "-n"], input=r.text.encode()).returncode == 0


# --- artifacts, release, resume -------------------------------------------------------------


def test_artifacts_need_the_lease_and_a_matching_hash_and_survive_a_release(
    client, owner, work_id, wc
):
    import hashlib

    job_id = add_job(work_id, "transcode", {"codec": "aac", "bitrate_kbps": 32, "tracks": []})
    r = claim(wc, capabilities=["transcode"])
    headers = lease(r)
    body = b"chapter audio"
    digest = hashlib.sha256(body).hexdigest()
    url = f"/api/worker/jobs/{job_id}/artifacts"
    assert wc.put(f"{url}/a.m4a", content=body).status_code == 409  # no lease token
    bad = wc.put(f"{url}/a.m4a", content=body, headers={**headers, "X-Content-SHA256": "0" * 64})
    assert bad.status_code == 422
    assert wc.put(f"{url}/..%2Fx", content=body,
                  headers={**headers, "X-Content-SHA256": digest}).status_code in (400, 404)  # fmt: skip
    ok = wc.put(f"{url}/a.m4a", content=body, headers={**headers, "X-Content-SHA256": digest})
    assert ok.status_code == 201
    # pausing hands the job back without spending an attempt
    assert wc.post(f"/api/worker/jobs/{job_id}/release", headers=headers).json() == {
        "status": "queued"
    }
    with session_factory()() as db:
        assert db.get(Job, job_id).attempts == 0
    headers = lease(claim(wc, capabilities=["transcode"]))
    assert wc.get(url, headers=headers).json()["artifacts"] == [
        {"name": "a.m4a", "size": len(body), "sha256": digest}
    ]
    assert wc.get(f"{url}/a.m4a", headers=headers).content == body


@needs_ffmpeg
def test_a_remote_worker_transcodes_and_the_server_publishes_the_rendition(
    client, owner, work_id, app, worker_key, settings
):
    with session_factory()() as db:  # the rescan that finds the work's audio
        jobs.refresh_work(db, settings.library_dir, db.get(Work, work_id), auto_enqueue=False)
        db.commit()
    r = api_post(client, f"/api/works/{work_id}/transcode", owner["csrf"])
    assert r.status_code in (200, 202), r.text
    http = TestClient(app, headers={"Authorization": f"Bearer {worker_key}"})
    worker = RemoteWorker("http://testserver", worker_key, client=http,
                          runners=[TranscodeRunner()])  # fmt: skip
    assert worker.run_once()
    with session_factory()() as db:
        job = db.scalar(select(Job).where(Job.kind == "transcode"))
        assert job.status == JobStatus.SUCCEEDED, job.error
        assert job.result == {"done": 1, "total": 1}
        work = db.get(Work, work_id)
        track_sha = job.payload["tracks"][0]["sha256"]
        found = find_variant(settings.library_dir, work, track_sha)
        assert found is not None and found[0].stat().st_size > 0
    tracks = client.get(f"/api/works/{work_id}/tracks").json()["tracks"]
    assert tracks[0]["variants"]["low"] is not None


# --- the tts contract, with a stand-in for the TTS thread's server handler -------------------

SCRIPT = {
    "version": 1,
    "chapters": [
        {"index": 0, "title": "One", "sentences": [{"n": 0, "text": "Hi."},
                                                    {"n": 1, "text": "Hello there.", "pause_s": 0.5}]},
        {"index": 1, "title": "Two", "sentences": [{"n": 2, "text": "Bye now."}]},
    ],
}  # fmt: skip


def fake_synth(calls: list):
    """One sample per character, at 24 kHz: sentence durations are known exactly."""

    def make(voice, speed, lang):
        def synth(text):
            calls.append(text)
            return np.full(len(text) * 2400, 0.1, dtype=np.float32)  # 0.1 s per character

        return synth

    return make


@pytest.fixture
def tts_kind(settings, tmp_path):
    """A minimal ``tts`` handler like the one the TTS PR registers: serves the script and
    records the result it is given."""
    script = tmp_path / "script.json"
    script.write_text(json.dumps(SCRIPT))
    from lectern.ingest.hashing import sha256_file

    applied: list = []

    def inputs(db, settings, job):
        return {"script": queue.InputFile(script, "application/json", sha256_file(script))}

    def apply(db, settings, job, body, artifacts):
        data = json.loads(body)
        for ch in data["chapters"]:
            assert sha256_file(artifacts / ch["artifact"]) == ch["sha256"]
        job.status = JobStatus.SUCCEEDED
        job.finished_at = utcnow()
        db.commit()
        applied.append(data)
        return {"job_id": job.id, "status": "succeeded"}

    previous = queue.handler("tts")  # the real handler, once generated audiobooks exist
    queue.register(queue.KindHandler(kind="tts", capability=queue.tts_capability,
                                     inputs=inputs, apply_result=apply))  # fmt: skip
    yield applied
    if previous is None:
        queue._HANDLERS.pop("tts", None)
    else:
        queue.register(previous)


@needs_ffmpeg
def test_kokoro_runner_follows_the_tts_contract_and_resumes(
    client, owner, work_id, app, worker_key, tts_kind
):
    job_id = add_job(work_id, "tts", {"engine": "kokoro", "voice": "af_heart", "speed": 1.0,
                     "lang": "en-us", "audio": {"codec": "flac", "sample_rate": 24000}})  # fmt: skip
    http = TestClient(app, headers={"Authorization": f"Bearer {worker_key}"})
    calls: list = []
    paused = {"on": False}

    def synth_then_pause(voice, speed, lang):
        inner = fake_synth(calls)(voice, speed, lang)

        def synth(text):
            if text == "Bye now.":  # chapter 1: the Mac is unplugged before it starts
                paused["on"] = True
            return inner(text)

        return synth

    worker = RemoteWorker("http://testserver", worker_key, client=http,
                          runners=[KokoroRunner(synth_then_pause)],
                          pause=lambda: "on battery" if paused["on"] else None)  # fmt: skip
    assert worker.run_once()
    with session_factory()() as db:
        job = db.get(Job, job_id)
        assert job.status == JobStatus.QUEUED and job.attempts == 0  # handed back, not failed
    assert not tts_kind
    # plugged in again: chapter 0 is not synthesized a second time
    paused["on"] = False
    calls.clear()
    worker.runners = [KokoroRunner(fake_synth(calls))]
    assert worker.run_once()
    assert calls == ["Bye now."]
    (result,) = tts_kind
    assert result["engine"] == "kokoro" and result["codec"] == "flac"
    one, two = result["chapters"]
    assert one["artifact"] == "chapter-0000.flac" and two["artifact"] == "chapter-0001.flac"
    # 0.1 s per character, then the sentence's pause (default 0.3 s) belongs to no sentence
    assert one["sentences"] == [{"n": 0, "start_s": 0.0, "end_s": 0.3},
                                {"n": 1, "start_s": 0.6, "end_s": 1.8}]  # fmt: skip
    assert one["duration_s"] == pytest.approx(2.3)
    assert two["sentences"] == [{"n": 2, "start_s": 0.0, "end_s": 0.8}]


def test_render_chapter_timings_are_exact_and_pauses_are_silent():
    calls: list = []
    synth = fake_synth(calls)("v", 1.0, "en-us")
    samples, timings = render_chapter(SCRIPT["chapters"][0]["sentences"], synth, 24000)
    assert timings[1] == {"n": 1, "start_s": 0.6, "end_s": 1.8}
    assert not samples[int(0.3 * 24000) : int(0.6 * 24000)].any()  # the pause is silence


# --- MLX with the CPU fallback ----------------------------------------------------------------


def test_mlx_words_are_absolute_monotone_and_skip_untimed():
    seen = []
    words = words_from_segments(
        [{"end": 2.0, "words": [{"word": " a", "start": 1.0, "end": 1.2, "probability": 0.9},
                                {"word": " b", "start": 0.5, "end": 0.7},
                                {"word": " c", "start": float("nan"), "end": 1.0}]}],
        100.0, seen.append,
    )  # fmt: skip
    assert words == [Word(" a", 101.0, 101.2, 0.9), Word(" b", 101.0, 101.0, 0.0)]
    assert seen == [2.0]


class FakeCPU(Transcriber):
    def __init__(self):
        super().__init__("small")
        self.windows = 0

    def load(self):
        return object()

    def _window_words(self, samples, base_s, language, on_segment):
        self.windows += 1
        return [Word(" cpu", base_s, base_s + 0.1)], "en"


def test_mlx_falls_back_to_the_cpu_when_mlx_cannot_load(monkeypatch):
    fake = types.ModuleType("mlx_whisper")
    loader = types.ModuleType("mlx_whisper.load_models")

    def boom(_repo):
        raise RuntimeError("no Metal device")

    loader.load_model = boom
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    monkeypatch.setitem(sys.modules, "mlx_whisper.load_models", loader)
    cpu = FakeCPU()
    tr = MLXTranscriber("small", fallback=cpu)
    assert tr.label == "mlx-whisper-small"
    words, lang = tr._window_words(np.zeros(16000, dtype=np.float32), 5.0, None, lambda t: None)
    assert words == [Word(" cpu", 5.0, 5.1)] and cpu.windows == 1
    assert tr.using_fallback and tr.label == cpu.label  # the map names the engine that ran


def test_mlx_failure_mid_job_redoes_the_window_on_the_cpu(monkeypatch):
    fake = types.ModuleType("mlx_whisper")
    loader = types.ModuleType("mlx_whisper.load_models")
    loader.load_model = lambda _repo: None
    fake.transcribe = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Metal OOM"))
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    monkeypatch.setitem(sys.modules, "mlx_whisper.load_models", loader)
    cpu = FakeCPU()
    tr = MLXTranscriber("small", fallback=cpu)
    words, _ = tr._window_words(np.zeros(10, dtype=np.float32), 0.0, None, lambda t: None)
    assert words and tr.using_fallback


def test_worker_transcriber_uses_the_cpu_off_apple_silicon(monkeypatch):
    monkeypatch.setattr("lectern.align.mlx.apple_silicon", lambda: False)
    monkeypatch.setattr("lectern.align.mlx.transcription_available", lambda: True)
    tr = worker_transcriber("auto", "base")
    assert type(tr) is Transcriber and tr.model_name == "base"
    monkeypatch.setattr("lectern.align.mlx.transcription_available", lambda: False)
    assert worker_transcriber("cpu", "base") is None


def test_mlx_label_maps_back_to_the_model_for_retries():
    assert jobs._model_name("mlx-whisper-medium") == "medium"


# --- conditions, config, menu bar ----------------------------------------------------------

PMSET_BATTERY = """Now drawing from 'Battery Power'
 -InternalBattery-0 (id=4653155)\t54%; discharging; 4:10 remaining present: true"""
PMSET_AC = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=4653155)\t100%; charged; 0:00 remaining present: true"""
PORTS = """Hardware Port: Ethernet\nDevice: en5\nEthernet Address: x\n
Hardware Port: Wi-Fi\nDevice: en0\nEthernet Address: y\n"""


def test_parsers_read_macos_tool_output():
    assert cond.parse_pmset(PMSET_BATTERY) == cond.Power(on_ac=False, percent=54)
    assert cond.parse_pmset(PMSET_AC) == cond.Power(on_ac=True, percent=100)
    assert cond.parse_pmset("Now drawing from 'AC Power'\n") == cond.Power(True, None)  # Mac mini
    assert cond.parse_wifi_device(PORTS) == "en0"
    assert cond.parse_default_interface("   route to: default\n  interface: en5\n") == "en5"
    assert cond.parse_ssid("  BSSID : x\n  SSID : Home Net\n") == "Home Net"
    assert cond.parse_ssid("  SSID : <redacted>\n") is None
    scutil = "<dictionary> {\n  HTTPSEnable : 1\n  HTTPSPort : 3128\n  HTTPSProxy : proxy.school\n}"
    assert cond.parse_scutil_proxy(scutil) == "http://proxy.school:3128"
    assert cond.parse_scutil_proxy("  HTTPSEnable : 0\n") is None


@pytest.mark.parametrize(
    ("changes", "power", "net", "reason"),
    [
        ({}, cond.Power(True, 100), None, None),
        ({}, cond.Power(False, 90), None, "on battery"),
        ({"pause_on_battery": False}, cond.Power(False, 31), None, None),
        ({"pause_on_battery": False}, cond.Power(False, 29), None, "battery below 30%"),
        ({"pause_on_battery": False, "battery_min_percent": 0}, cond.Power(False, 5), None, None),
        ({}, None, None, None),  # a desktop without a battery, or not a Mac
        ({"enabled": False}, cond.Power(True, 100), None, "switched off"),
        ({"only_on_wifi": True}, cond.Power(True, 100), cond.Network(False, None), "not on Wi-Fi"),
        ({"only_on_wifi": True}, cond.Power(True, 100), cond.Network(True, "x"), None),
        ({"only_on_wifi": True, "wifi_networks": ["Home"]}, None, cond.Network(True, "School"),
         "Wi-Fi network 'School' is not allowed"),
        ({"only_on_wifi": True, "wifi_networks": ["Home"]}, None, cond.Network(True, "Home"), None),
    ],
)  # fmt: skip
def test_pause_reason(changes, power, net, reason):
    cfg = wcfg.AgentConfig.from_dict(changes)
    assert cond.pause_reason(cfg, power, net) == reason


def test_proxy_resolution(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    assert cond.resolve_proxy("none") is None
    assert cond.resolve_proxy("http://p:8080") == "http://p:8080"
    assert cond.resolve_proxy("system", "") is None
    monkeypatch.setenv("HTTPS_PROXY", "http://env:1")
    assert cond.resolve_proxy("system", "") == "http://env:1"


def test_config_and_key_are_private_and_toggles_round_trip(tmp_path):
    cfg = wcfg.update_config(tmp_path, server="https://l.example", only_on_wifi=True)
    assert wcfg.load_config(tmp_path).only_on_wifi and cfg.server == "https://l.example"
    key = wcfg.save_key("lw_secret", tmp_path)
    assert key.stat().st_mode & 0o077 == 0 and wcfg.read_key(tmp_path) == "lw_secret"
    with pytest.raises(KeyError):
        wcfg.update_config(tmp_path, nonsense=1)


def test_menu_shows_the_job_and_toggles_change_the_config():
    cfg = wcfg.AgentConfig(server="https://l.example")
    busy = {"updated_at": 1000.0, "state": "busy", "capabilities": ["align"],
            "job": {"kind": "tts", "work_id": 7, "progress": 0.426, "message": "chapter 3"}}  # fmt: skip
    items = [i for i in menubar.menu_model(busy, cfg, login=True, now=1001.0) if i]
    assert items[0].title == "Making audiobook work 7: 43 %"
    assert items[1].title == "chapter 3"
    by_key = {i.key: i for i in items}
    assert by_key["enabled"].checked and by_key["login"].checked
    assert by_key["pause_on_battery"].checked and not by_key["battery_min"].enabled
    assert menubar.title_icon(busy, cfg, now=1001.0) == "📖 43%"
    assert menubar.status_line(busy, now=1100.0) == "Not running"  # stale state file
    paused = {"updated_at": 1000.0, "state": "paused", "reason": "on battery"}
    assert menubar.status_line(paused, now=1001.0) == "Paused: on battery"
    assert menubar.toggle("enabled", cfg) == {"enabled": False}
    assert menubar.toggle("battery_min", cfg) == {"battery_min_percent": 0}


def test_agent_follows_the_menu_toggles_and_reports_state(tmp_path, monkeypatch):
    from lectern.worker.agent import Agent

    wcfg.update_config(tmp_path, server="https://l.example", name="mbp")
    wcfg.save_key("lw_x", tmp_path)
    conditions = cond.Conditions(power=lambda: cond.Power(True, 100), network=lambda: None)
    agent = Agent(tmp_path, conditions=conditions, runners=[TranscodeRunner()], client=object())
    assert agent.pause_reason() is None and agent.state()["state"] == "ready"
    wcfg.update_config(tmp_path, enabled=False)
    agent._cfg_at -= 10  # as if the reload interval had passed
    assert agent.pause_reason() == "switched off"
    state = agent.state()
    assert state["state"] == "paused" and state["capabilities"] == ["transcode"]
    assert state["power"] == {"on_ac": True, "percent": 100}


def test_enroll_writes_config_and_key(client, owner, app, tmp_path):
    from lectern.worker.agent import enroll

    code = api_post(client, "/api/worker-enrollments", owner["csrf"], {"label": "m"}).json()["code"]
    cfg = enroll("http://testserver", code, name="mbp", home=tmp_path, allow_http=True,
                 client=TestClient(app))  # fmt: skip
    assert cfg.server == "http://testserver" and wcfg.read_key(tmp_path).startswith("lw_")
    with session_factory()() as db:
        assert db.scalar(select(WorkerCredential).where(WorkerCredential.label == "m"))


def test_launchd_plist_runs_the_agent_at_low_priority(tmp_path):
    import plistlib

    from lectern.worker.agent import AGENT_LABEL, plist_for

    data = plistlib.loads(plist_for(AGENT_LABEL, ["lectern", "worker-agent"], keep_alive=True,
                                    logs=tmp_path))  # fmt: skip
    assert data["ProgramArguments"] == ["lectern", "worker-agent"] and data["RunAtLoad"]
    assert data["KeepAlive"] == {"SuccessfulExit": False} and data["Nice"] == 10


# --- migration ------------------------------------------------------------------------------


def test_0012_backfills_capabilities_and_downgrades(tmp_path):
    import sqlalchemy as sa
    from alembic import command

    from lectern.db import make_engine
    from lectern.migrate import alembic_config

    engine = make_engine(f"sqlite:///{tmp_path / 'm12.db'}")
    cfg = alembic_config(str(engine.url))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0011")
        now = "2026-10-21 00:00:00"
        for kind in ("align", "transcode"):
            conn.execute(sa.text(
                "INSERT INTO jobs (kind, status, priority, attempts, max_attempts, run_after, "
                f"created_at, updated_at) VALUES ('{kind}', 'QUEUED', 0, 0, 3, '{now}', "
                f"'{now}', '{now}')"))  # fmt: skip
        command.upgrade(cfg, "head")
        rows = conn.execute(sa.text("SELECT kind, capability FROM jobs ORDER BY id")).all()
        assert [tuple(r) for r in rows] == [("align", "align"), ("transcode", "transcode")]
        command.downgrade(cfg, "0011")
        assert "capability" not in {c["name"] for c in sa.inspect(conn).get_columns("jobs")}
        assert "worker_enrollments" not in sa.inspect(conn).get_table_names()
