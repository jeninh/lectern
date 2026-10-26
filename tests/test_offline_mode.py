"""Offline mode: one choke point refuses every outbound connection, the features that would
contact the internet say so without trying, and models can be fetched ahead of time."""

from __future__ import annotations

import asyncio
import http.server
import os
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.request

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from tests.conftest import ORIGIN, OWNER_PASSWORD, api_post, login, make_user, put_json
from tests.test_lookup import FakeUpstream
from tests.test_tts import KEY, FakeEleven

from lectern import cli, egress, koplugin, lookup, offline_mode
from lectern.align.transcribe import Transcriber, TranscriptionUnavailable
from lectern.config import Settings
from lectern.main import create_app
from lectern.tts import elevenlabs as el
from lectern.tts import engines as eng


@pytest.fixture
def loopback_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"local")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://localhost:{srv.server_address[1]}/"
    srv.shutdown()


def test_every_outbound_path_is_refused_before_it_starts(loopback_server):
    egress.enforce(True)
    # An unroutable address would hang until the timeout if the connect were attempted.
    started = time.monotonic()
    with pytest.raises(egress.OfflineError, match="offline mode"):
        socket.create_connection(("192.0.2.1", 443), timeout=5)
    assert time.monotonic() - started < 1
    for lookup_fn in (
        lambda: socket.getaddrinfo("openlibrary.org", 443),
        lambda: socket.gethostbyname("huggingface.co"),
        lambda: socket.gethostbyaddr("8.8.8.8"),
        lambda: socket.create_connection(("api.elevenlabs.io", 443), timeout=5),
    ):
        with pytest.raises(egress.OfflineError):
            lookup_fn()
    with pytest.raises(httpx.ConnectError, match="offline mode"):
        httpx.get("https://huggingface.co/api/models", timeout=5)
    # The real transports of both features report it as offline, not as a flaky network.
    with pytest.raises(lookup.LookupError_) as exc:
        lookup.http_get("https://openlibrary.org/search.json?isbn=9780441478125")
    assert exc.value.code == "offline"
    with pytest.raises(el.ElevenLabsError) as eexc:
        el.https_transport("GET", "/v1/voices", {}, None)
    assert eexc.value.code == "offline" and not eexc.value.retryable
    # The reverse proxy, health check, and local services keep working.
    assert urllib.request.urlopen(loopback_server, timeout=5).read() == b"local"  # noqa: S310
    # Finding Lectern's own Tailscale address: a UDP connect sends nothing; sending does.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(koplugin.TAILSCALE_PROBE)
        with pytest.raises(egress.OfflineError):
            s.sendto(b"x", ("100.100.100.100", 53))
        with pytest.raises(egress.OfflineError):
            s.sendmsg([b"x"], [], 0, ("192.0.2.1", 53))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s, pytest.raises(egress.OfflineError):
        s.connect(("100.101.102.103", 53))  # any other tailnet peer could forward onward


def test_the_server_runs_on_an_event_loop_the_guard_sees(monkeypatch):
    """uvloop connects and resolves in libuv, where no audit event fires."""
    seen: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: seen.update(kw))
    cli.serve()
    loop = uvicorn.Config(
        "lectern.main:app_factory", loop=seen.get("loop", "auto")
    ).get_loop_factory()()
    egress.enforce(True)
    try:
        connect = loop.create_connection(asyncio.Protocol, "192.0.2.1", 80)
        with pytest.raises(egress.OfflineError):
            loop.run_until_complete(asyncio.wait_for(connect, 5))
        with pytest.raises(egress.OfflineError):
            loop.run_until_complete(loop.getaddrinfo("openlibrary.org", 443))
    finally:
        loop.close()


def test_alignment_loads_only_downloaded_models_while_offline(monkeypatch):
    calls: list[dict] = []

    class WhisperModel:
        def __init__(self, name, **kw):
            calls.append(kw)
            if kw.get("local_files_only"):
                raise FileNotFoundError("model.bin")

    monkeypatch.setitem(
        sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel)
    )
    Transcriber("small").load()
    assert "local_files_only" not in calls[-1]
    egress.enforce(True)
    with pytest.raises(TranscriptionUnavailable, match="offline mode.*prepare-models"):
        Transcriber("small").load()
    assert calls[-1]["local_files_only"] is True


def test_child_processes_inherit_it_and_turning_off_restores_the_environment(monkeypatch):
    monkeypatch.delenv(egress.CHILD_ENV, raising=False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")  # the operator's own setting
    egress.enforce(True)
    assert os.environ[egress.CHILD_ENV] == "1"
    probe = (
        "from lectern import egress; egress.enforce_from_env(); import socket; "
        "socket.create_connection(('192.0.2.1', 80), timeout=5)"
    )
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    assert done.returncode != 0 and "OfflineError" in done.stderr, done.stderr
    egress.enforce(False)
    assert egress.CHILD_ENV not in os.environ
    assert os.environ["HF_HUB_OFFLINE"] == "1"  # not ours to remove


def test_owner_switch_blocks_lookup_and_elevenlabs_without_a_request(client, owner):
    csrf = owner["csrf"]
    fetch = FakeUpstream()
    client.app.state.lookup_fetch = fetch
    eleven = FakeEleven()
    client.app.state.elevenlabs_transport = eleven
    assert put_json(client, "/api/library-settings", csrf, {"metadata_lookup_enabled": True}).status_code == 200  # fmt: skip
    assert put_json(client, "/api/integrations/elevenlabs", csrf, {"api_key": KEY}).status_code == 200  # fmt: skip
    before = len(eleven.requests)

    view = client.get("/api/admin/offline-mode").json()
    assert (view["on"], view["forced"]) == (False, False)
    rows = {f["id"]: f for f in view["features"]}
    assert rows["metadata_lookup"]["on"] and rows["elevenlabs"]["on"]
    assert not rows["metadata_lookup"]["blocked"]

    r = put_json(client, "/api/admin/offline-mode", csrf, {"enabled": True})
    assert r.status_code == 200, r.text
    rows = {f["id"]: f for f in r.json()["features"]}
    assert r.json()["on"] and rows["metadata_lookup"]["blocked"] and rows["elevenlabs"]["blocked"]
    assert client.get("/api/me").json()["offline_mode"] is True

    r = api_post(client, "/api/metadata/lookup", csrf, {"isbn": "9780441478125"})
    assert r.status_code == 409 and r.json()["detail"] == "lookup_offline"
    r = client.get("/api/integrations/elevenlabs/voices")
    assert r.status_code == 409 and r.json()["detail"] == "elevenlabs_offline"
    st = api_post(client, "/api/integrations/elevenlabs/test", csrf).json()
    assert st["offline"] and st["last_test_ok"] is False and st["last_test_detail"] == "offline"
    assert fetch.urls == [] and len(eleven.requests) == before  # nothing left the server

    r = put_json(client, "/api/admin/offline-mode", csrf, {"enabled": False})
    assert r.json()["on"] is False
    r = api_post(client, "/api/metadata/lookup", csrf, {"isbn": "9780441478125"})
    assert r.status_code == 200 and fetch.urls


def test_only_the_owner_changes_it(client, owner):
    make_user(client, owner["csrf"], "mem", "member")
    mcsrf = login(client, "mem", "mem-correct horse")
    assert put_json(client, "/api/admin/offline-mode", mcsrf, {"enabled": True}).status_code == 403
    assert client.get("/api/admin/offline-mode").status_code == 403
    assert egress.offline() is False


def test_the_environment_forces_it_and_nothing_downloads(tmp_path, settings, setup_token):
    forced = Settings(**{**settings.model_dump(), "offline": True})
    app = create_app(forced, start_watcher=False, start_background=False)
    marker = tmp_path / "ran"
    app.state.prepare_command = [sys.executable, "-c", f"open({str(marker)!r}, 'w')"]
    with TestClient(app) as c:
        assert egress.offline()
        c.post("/setup", data={"token": setup_token.token, "username": "owner",
                               "password": OWNER_PASSWORD, "password_confirm": OWNER_PASSWORD},
               headers={"origin": ORIGIN}, follow_redirects=False)  # fmt: skip
        csrf = c.get("/api/me").json()["csrf_token"]
        view = c.get("/api/admin/offline-mode").json()
        assert view["on"] and view["forced"]
        r = put_json(c, "/api/admin/offline-mode", csrf, {"enabled": False})
        assert r.status_code == 409 and r.json()["detail"] == "offline_forced"
        r = api_post(c, "/api/admin/offline-mode/prepare", csrf)
        assert r.status_code == 409 and r.json()["detail"] == "offline"
    assert not marker.exists()
    assert offline_mode.prepare(forced, say=lambda _m: None) is False


def test_download_button_runs_in_the_background(client, owner):
    client.app.state.prepare_command = [
        sys.executable, "-c", "print('alignment model small: ready')",
    ]  # fmt: skip
    r = api_post(client, "/api/admin/offline-mode/prepare", owner["csrf"])
    assert r.status_code == 202, r.text
    for _ in range(100):
        prep = client.get("/api/admin/offline-mode").json()["prepare"]
        if not prep["running"]:
            break
        time.sleep(0.05)
    assert prep["ok"] is True and prep["log"] == ["alignment model small: ready"]


def test_whisper_models_are_found_by_name_without_the_network(settings):
    root = offline_mode.models_dir(settings)
    for repo in ("Systran--faster-whisper-large-v3-turbo", "Systran--faster-distil-whisper-large-v3",
                 "Systran--faster-whisper-small"):  # fmt: skip
        snap = root / f"models--{repo}" / "snapshots" / "abc"
        snap.mkdir(parents=True)
        (snap / "model.bin").write_bytes(b"")
    (root / "models--Systran--faster-whisper-base" / "snapshots" / "abc").mkdir(parents=True)
    ready = {m for m in ("small", "base", "large-v3", "large-v3-turbo", "distil-large-v3")
             if offline_mode.whisper_ready(settings, m)}  # fmt: skip
    # base has no model.bin (an interrupted download); large-v3 is not large-v3-turbo
    assert ready == {"small", "large-v3-turbo", "distil-large-v3"}


def test_piper_voice_download_is_all_or_nothing(settings):
    urls: list[str] = []

    def fetch(url, dest):
        urls.append(url)
        dest.write_bytes(b"{}" if url.endswith(".json") else b"onnx")

    offline_mode._download_piper(settings, "en_US-lessac-medium", fetch)
    assert urls[0] == (
        "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/"
        "en_US-lessac-medium.onnx"
    )
    assert [v.id for v in eng.piper_voices(settings)] == ["en_US-lessac-medium"]

    def broken(url, dest):
        if url.endswith(".json"):
            raise OSError("connection reset")
        dest.write_bytes(b"onnx")

    with pytest.raises(OSError):
        offline_mode._download_piper(settings, "de_DE-thorsten-high", broken)
    voices_dir = eng.piper_voices_dir(settings)
    assert sorted(p.name for p in voices_dir.iterdir()) == [
        "en_US-lessac-medium.onnx", "en_US-lessac-medium.onnx.json",
    ]  # fmt: skip
    with pytest.raises(ValueError):
        offline_mode.piper_voice_urls("../../etc/passwd")
