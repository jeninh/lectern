"""KOReader plugin (docs/koreader-plugin.md): per-device zip, one-time setup secret, pairing
codes, the plugin's status/queue route, and the plugin itself run against a live server.

Expected values come from the request and the household built here, not from the code under
test: 7-day single-use revocable secrets; 6-digit codes valid 5 minutes, single use, 5 tries.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from tests.conftest import (
    ORIGIN,
    OWNER_PASSWORD,
    api_post,
    login,
    make_epub,
    make_user,
)

from lectern import koplugin
from lectern.db import session_factory
from lectern.ingest.watcher import InboxWatcher
from lectern.models import AuditLog, DeviceBootstrap, DevicePairingCode, Work, utcnow

SERVER = "http://100.101.102.103:8080"
PLUGIN_FILES = {"_meta.lua", "main.lua", "lectern_core.lua", "lectern_http.lua"}


def basic(username: str, secret: str) -> dict:
    return {"authorization": "Basic " + base64.b64encode(f"{username}:{secret}".encode()).decode()}


def ko(username: str, userkey: str) -> dict:
    return {"x-auth-user": username, "x-auth-key": userkey}


def add_device(http: TestClient, csrf: str, label: str) -> dict:
    r = api_post(http, "/api/devices", csrf, {"label": label})
    assert r.status_code == 201, r.text
    return r.json()


def get_zip(http: TestClient, csrf: str, device_id: str, server_url: str | None = SERVER):
    body = {} if server_url is None else {"server_url": server_url}
    return api_post(http, f"/api/devices/{device_id}/koplugin", csrf, body)


def unzip(resp) -> dict[str, bytes]:
    assert resp.status_code == 200, resp.text
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def secret_of(files: dict[str, bytes]) -> str:
    return files["lectern.koplugin/lectern_bootstrap.txt"].decode().strip()


def bootstrap(http, secret: str):
    return http.post("/koplugin/bootstrap", json={"secret": secret})


def audit_rows() -> list[AuditLog]:
    with session_factory()() as db:
        return list(db.scalars(select(AuditLog).order_by(AuditLog.id)))


@pytest.fixture
def kindle(client, owner):
    dev = add_device(client, owner["csrf"], "Kindle PW5")
    return {"id": dev["device"]["id"], "created": dev}


# --- the zip ---------------------------------------------------------------------------------


def test_zip_carries_the_plugin_server_address_and_a_hashed_one_time_secret(client, owner, kindle):
    r = get_zip(client, owner["csrf"], kindle["id"])
    assert r.headers["content-type"] == "application/zip"
    assert 'filename="lectern.koplugin.zip"' in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    files = unzip(r)
    names = {n.split("/", 1)[1] for n in files}
    assert all(n.startswith("lectern.koplugin/") for n in files)  # copy the folder as-is
    assert names == PLUGIN_FILES | {"lectern_device.lua", "lectern_bootstrap.txt"}
    device = files["lectern.koplugin/lectern_device.lua"].decode()
    assert f'server_url = "{SERVER}"' in device and 'username = "owner"' in device
    secret = secret_of(files)
    assert secret.startswith("lb_") and len(secret) > 40
    assert secret not in device
    with session_factory()() as db:
        row = db.scalar(select(DeviceBootstrap))
        assert row.secret_hash == hashlib.sha256(secret.encode()).hexdigest()
        assert row.server_url == SERVER
        assert timedelta(days=7) - (row.expires_at - row.created_at) < timedelta(seconds=5)
    devices = client.get("/api/devices").json()["devices"]
    assert devices[0]["plugin"]["setup_state"] == "pending"
    assert devices[0]["koplugin"] is True
    assert "lb_" not in json.dumps(devices)


def test_only_the_devices_own_user_can_download_its_zip(client, owner, app, kindle):
    mia = make_user(client, owner["csrf"], "mia", "member")
    mia_http = TestClient(app)
    mia_csrf = login(mia_http, "mia", mia["password"])
    mias = add_device(mia_http, mia_csrf, "Mia's Kobo")["device"]["id"]
    # Not the owner role either: the zip hands out that device's credentials.
    assert get_zip(client, owner["csrf"], mias).status_code == 404
    assert get_zip(mia_http, mia_csrf, kindle["id"]).status_code == 404
    assert api_post(client, f"/api/devices/{mias}/pairing-code", owner["csrf"]).status_code == 404
    # Without a session or CSRF token nothing is minted.
    anon = TestClient(app)
    assert anon.post(f"/api/devices/{kindle['id']}/koplugin", json={}).status_code == 401
    no_csrf = client.post(
        f"/api/devices/{kindle['id']}/koplugin", json={}, headers={"origin": ORIGIN}
    )
    assert no_csrf.status_code == 403
    assert get_zip(mia_http, mia_csrf, mias).status_code == 200
    with session_factory()() as db:
        assert len(db.scalars(select(DeviceBootstrap)).all()) == 1


def test_a_device_without_both_credentials_has_no_plugin(client, owner):
    r = api_post(client, "/api/devices/sync-keys", owner["csrf"], {"label": "Sync only"})
    key_id = r.json()["sync_key"]["id"]
    assert get_zip(client, owner["csrf"], f"k{key_id}").status_code == 404


def test_server_address_validation_and_default(client, owner, kindle, settings, monkeypatch):
    for bad in ("ftp://100.1.2.3", "http://user:pw@100.1.2.3:8080", "http://100.1.2.3/opds", "x"):
        r = get_zip(client, owner["csrf"], kindle["id"], bad)
        assert r.status_code == 422 and r.json()["detail"] == "invalid_server_url", bad
    monkeypatch.setattr(koplugin, "detect_tailscale_ip", lambda: None)
    r = get_zip(client, owner["csrf"], kindle["id"], None)
    assert r.status_code == 422 and r.json()["detail"] == "server_url_required"
    assert client.get("/api/devices/koplugin").json()["source"] == "none"
    monkeypatch.setattr(koplugin, "detect_tailscale_ip", lambda: "100.88.1.2")
    assert client.get("/api/devices/koplugin").json() == {
        "server_url": "http://100.88.1.2:8080",
        "source": "tailscale",
        "port": 8080,
    }
    files = unzip(get_zip(client, owner["csrf"], kindle["id"], None))
    assert (
        'server_url = "http://100.88.1.2:8080"'
        in files["lectern.koplugin/lectern_device.lua"].decode()
    )
    settings.device_url = "http://100.9.9.9:8080"
    assert client.get("/api/devices/koplugin").json()["source"] == "config"


def test_device_file_cannot_be_escaped_by_a_label(client, owner):
    label = 'Evil"\n os.execute("rm -rf /") --'
    dev = add_device(client, owner["csrf"], label)["device"]["id"]
    text = unzip(get_zip(client, owner["csrf"], dev))["lectern.koplugin/lectern_device.lua"]
    assert b"\n os.execute" not in text and b'Evil"' not in text
    if shutil.which("luajit"):
        out = subprocess.run(
            ["luajit", "-e", f"local t = loadstring({json.dumps(text.decode())})() print(t.label)"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert out.stdout.rstrip("\n") == label


# --- bootstrap -------------------------------------------------------------------------------


def test_bootstrap_is_single_use_and_rotates_both_credentials(client, owner, kindle):
    created = kindle["created"]
    old_sync, old_opds = created["sync_secret"], created["opds_secret"]
    md5 = hashlib.md5(old_sync.encode(), usedforsecurity=False).hexdigest()
    assert client.get("/users/auth", headers=ko("owner", md5)).status_code == 200
    secret = secret_of(unzip(get_zip(client, owner["csrf"], kindle["id"])))

    r = bootstrap(client, secret)
    assert r.status_code == 200, r.text
    creds = r.json()
    assert set(creds) == {"username", "device_id", "label", "userkey", "app_password", "server_url"}
    assert creds["username"] == "owner" and creds["label"] == "Kindle PW5"
    assert creds["server_url"] == SERVER and creds["device_id"] == kindle["id"]
    # KOReader sends its userkey as is; the new OPDS password works over Basic.
    assert re.fullmatch(r"[0-9a-f]{32}", creds["userkey"])
    assert client.get("/users/auth", headers=ko("owner", creds["userkey"])).status_code == 200
    assert client.get("/opds", headers=basic("owner", creds["app_password"])).status_code == 200
    # The previous credentials of this device stop working.
    assert client.get("/users/auth", headers=ko("owner", md5)).status_code == 401
    assert client.get("/opds", headers=basic("owner", old_opds)).status_code == 401

    again = bootstrap(client, secret)
    assert again.status_code == 410 and again.json()["detail"] == "setup_secret_invalid"
    assert client.get("/api/devices").json()["devices"][0]["plugin"]["setup_state"] == "used"
    actions = [(a.action, a.detail) for a in audit_rows() if a.action.startswith("koplugin.")]
    assert actions == [
        ("koplugin.download", SERVER),
        ("koplugin.bootstrap", None),
        ("koplugin.bootstrap_failed", "used"),
    ]


def test_bootstrap_expires_after_seven_days(client, owner, kindle):
    secret = secret_of(unzip(get_zip(client, owner["csrf"], kindle["id"])))
    with session_factory()() as db:
        db.execute(update(DeviceBootstrap).values(expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    assert bootstrap(client, secret).status_code == 410
    assert client.get("/api/devices").json()["devices"][0]["plugin"]["setup_state"] == "expired"
    assert audit_rows()[-1].detail == "expired"


def test_bootstrap_is_revocable_three_ways(client, owner, kindle):
    csrf, dev = owner["csrf"], kindle["id"]
    first = secret_of(unzip(get_zip(client, csrf, dev)))
    second = secret_of(unzip(get_zip(client, csrf, dev)))  # a new zip revokes the old secret
    assert bootstrap(client, first).status_code == 410
    r = api_post(client, f"/api/devices/{dev}/koplugin/revoke", csrf)
    assert r.status_code == 200 and r.json()["device"]["plugin"]["setup_state"] == "revoked"
    assert bootstrap(client, second).status_code == 410
    third = secret_of(unzip(get_zip(client, csrf, dev)))
    assert api_post(client, f"/api/devices/{dev}/revoke", csrf).status_code == 200
    assert bootstrap(client, third).status_code == 410
    assert get_zip(client, csrf, dev).status_code == 409  # a revoked device gets no new zip
    assert [a.detail for a in audit_rows() if a.action == "koplugin.bootstrap_failed"] == [
        "revoked",
        "revoked",
        "revoked",
    ]


def test_bootstrap_failures_are_rate_limited_per_address(client, owner, app):
    app.state.limiters["koplugin"].limit = 3
    codes = [bootstrap(client, f"lb_wrong{i}").status_code for i in range(5)]
    assert codes == [410, 410, 410, 429, 429]


# --- pairing codes ---------------------------------------------------------------------------


def new_code(http, csrf, dev) -> dict:
    r = api_post(http, f"/api/devices/{dev}/pairing-code", csrf)
    assert r.status_code == 201, r.text
    return r.json()


def pair(http, code: str, username: str = "owner"):
    return http.post("/koplugin/pair", json={"username": username, "code": code})


def wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


def test_pairing_code_is_six_digits_five_minutes_and_single_use(client, owner, kindle):
    c = new_code(client, owner["csrf"], kindle["id"])
    assert re.fullmatch(r"\d{6}", c["code"]) and c["attempts"] == 5
    with session_factory()() as db:
        row = db.scalar(select(DevicePairingCode))
        assert c["code"] not in (row.code_hash, row.salt)
        assert abs((row.expires_at - row.created_at) - timedelta(minutes=5)) < timedelta(seconds=2)
    r = pair(client, c["code"])
    assert r.status_code == 200, r.text
    assert client.get("/users/auth", headers=ko("owner", r.json()["userkey"])).status_code == 200
    assert pair(client, c["code"]).status_code == 401  # single use
    expired = new_code(client, owner["csrf"], kindle["id"])
    with session_factory()() as db:
        db.execute(update(DevicePairingCode).values(expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    assert pair(client, expired["code"]).status_code == 401


def test_fifth_try_still_works_and_five_wrong_tries_burn_the_code(client, owner, kindle, app):
    app.state.limiters["koplugin"].limit = 1000  # rate limits are tested on their own
    app.state.limiters["koplugin_pair_user"].limit = 1000
    csrf, dev = owner["csrf"], kindle["id"]
    c = new_code(client, csrf, dev)
    for _ in range(4):
        assert pair(client, wrong(c["code"])).status_code == 401
    assert pair(client, c["code"]).status_code == 200  # the fifth attempt
    c = new_code(client, csrf, dev)
    for _ in range(5):
        assert pair(client, wrong(c["code"])).status_code == 401
    assert pair(client, c["code"]).status_code == 401  # burned
    # Malformed codes count as tries too.
    c = new_code(client, csrf, dev)
    for junk in ("12", "abcdef", "1234567", "0", "99 9"):
        pair(client, junk)
    assert pair(client, c["code"]).status_code == 401


def test_a_new_code_ends_the_previous_one(client, owner, kindle):
    first = new_code(client, owner["csrf"], kindle["id"])
    second = new_code(client, owner["csrf"], kindle["id"])
    if first["code"] != second["code"]:
        assert pair(client, first["code"]).status_code == 401
    assert pair(client, second["code"]).status_code == 200


def test_pairing_is_rate_limited_per_address_and_per_username(client, owner, kindle, app):
    app.state.limiters["koplugin"].limit = 3
    codes = [pair(client, "000000", "nobody").status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]
    app.state.limiters["koplugin"].limit = 1000
    app.state.limiters["koplugin_pair_user"].limit = 2
    c = new_code(client, owner["csrf"], kindle["id"])
    assert [pair(client, wrong(c["code"])).status_code for _ in range(2)] == [401, 401]
    # Even the right code waits once the username's budget is spent.
    assert pair(client, c["code"]).status_code == 429
    app.state.limiters["koplugin_code"].limit = 1
    assert (
        api_post(client, f"/api/devices/{kindle['id']}/pairing-code", owner["csrf"]).status_code
        == 429
    )


def test_unknown_or_disabled_users_look_like_wrong_codes(client, owner, kindle):
    assert pair(client, "123456", "nobody").json() == {"detail": "invalid_code"}


# --- what the plugin reads -------------------------------------------------------------------


def test_status_lists_this_devices_queue_only(client, owner, settings):
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    make_epub(settings.inbox_path / "a.epub", title="Alpha", authors=["Ann"])
    w = InboxWatcher(settings.inbox_path, settings.library_dir, session_factory(), 0.01)
    w.scan_once()
    w.scan_once()
    with session_factory()() as db:
        work_id = db.scalar(select(Work.id))
    kindle = add_device(client, owner["csrf"], "Kindle")
    kobo = add_device(client, owner["csrf"], "Kobo")
    pw_id = kindle["device"]["app_password"]["id"]
    r = api_post(
        client, f"/api/device-queues/{pw_id}/items", owner["csrf"], {"work_ids": [work_id]}
    )
    assert r.status_code == 200
    st = client.get("/koplugin/status", headers=basic("owner", kindle["opds_secret"])).json()
    assert st["device"]["label"] == "Kindle" and st["queue"]["pending"] == 1
    item = st["queue"]["items"][0]
    assert (item["title"], item["author"]) == ("Alpha", "Ann")
    assert item["files"][0]["media_type"] == "application/epub+zip"
    other = client.get("/koplugin/status", headers=basic("owner", kobo["opds_secret"])).json()
    assert other["queue"] == {"pending": 0, "items": []}
    assert client.get("/koplugin/status").status_code == 401


def test_no_secret_reaches_the_log_or_the_audit_log(client, owner, kindle, caplog):
    caplog.set_level(logging.DEBUG)
    secret = secret_of(unzip(get_zip(client, owner["csrf"], kindle["id"])))
    creds = bootstrap(client, secret).json()
    c = new_code(client, owner["csrf"], kindle["id"])
    pair(client, wrong(c["code"]))
    paired = pair(client, c["code"]).json()
    bootstrap(client, secret)
    stored = " ".join(f"{a.action} {a.target} {a.detail}" for a in audit_rows())
    for value in (
        secret,
        creds["userkey"],
        creds["app_password"],
        c["code"],
        paired["userkey"],
        paired["app_password"],
    ):
        assert value not in caplog.text
        assert value not in stored
    assert {"koplugin.pairing_code", "koplugin.paired", "koplugin.pair_failed"} <= {
        a.action for a in audit_rows()
    }


def test_0011_downgrades_to_0010_and_upgrades(tmp_path):
    import sqlalchemy as sa
    from alembic import command

    from lectern.db import make_engine
    from lectern.migrate import alembic_config, upgrade_to_head

    engine = make_engine(f"sqlite:///{tmp_path / 'm11.db'}")
    upgrade_to_head(engine)
    cfg = alembic_config(str(engine.url))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0010")
    tables = sa.inspect(engine).get_table_names()
    assert "device_bootstraps" not in tables and "device_pairing_codes" not in tables
    upgrade_to_head(engine)
    assert "device_bootstraps" in sa.inspect(engine).get_table_names()


# --- the plugin itself, against a live server ------------------------------------------------

LUAJIT = shutil.which("luajit")
HARNESS = Path(__file__).parent / "koplugin" / "harness.lua"


def _lua_ready() -> bool:
    if not LUAJIT:
        return False
    probe = 'require("socket.http") require("cjson") require("mime")'
    return subprocess.run([LUAJIT, "-e", probe], capture_output=True).returncode == 0


needs_lua = pytest.mark.skipif(
    not _lua_ready() and not os.environ.get("LUAJIT_REQUIRED"),
    reason="luajit with lua-socket and lua-cjson not installed",
)


@pytest.fixture
def live(app, setup_token):
    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    http = httpx.Client(base_url=base, headers={"origin": ORIGIN})
    r = http.post(
        "/setup",
        data={
            "token": setup_token.token,
            "username": "owner",
            "password": OWNER_PASSWORD,
            "password_confirm": OWNER_PASSWORD,
        },
    )
    assert r.status_code == 303, r.text
    http.headers["x-csrf-token"] = http.get("/api/me").json()["csrf_token"]
    yield {"base": base, "http": http}
    http.close()
    server.should_exit = True
    thread.join(timeout=10)


LOGGED: list[str] = []


def lua(plugin: Path, settings_dir: Path, *args: str) -> dict:
    r = subprocess.run(
        [LUAJIT, str(HARNESS), str(plugin), str(settings_dir), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    LOGGED.extend(out["log"])
    return out


@needs_lua
def test_plugin_end_to_end(live, settings, tmp_path):
    http, base = live["http"], live["base"]
    LOGGED.clear()
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    make_epub(settings.inbox_path / "a.epub", title="Alpha", authors=["Ann"])
    w = InboxWatcher(settings.inbox_path, settings.library_dir, session_factory(), 0.01)
    w.scan_once()
    w.scan_once()
    with session_factory()() as db:
        work_id = db.scalar(select(Work.id))
    dev = http.post("/api/devices", json={"label": "Kindle PW5"}).json()
    dev_id, pw_id = dev["device"]["id"], dev["device"]["app_password"]["id"]
    http.post(f"/api/device-queues/{pw_id}/items", json={"work_ids": [work_id]})
    z = http.post(f"/api/devices/{dev_id}/koplugin", json={"server_url": base})
    root = tmp_path / "koreader"
    (root / "plugins").mkdir(parents=True)
    (root / "settings").mkdir()
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        zf.extractall(root / "plugins")  # "copy the folder to koreader/plugins"
    plugin, sdir = root / "plugins" / "lectern.koplugin", root / "settings"
    secret = (plugin / "lectern_bootstrap.txt").read_text().strip()

    # Wi-Fi off at first start: nothing happens, nothing is shown, the secret stays.
    out = lua(plugin, sdir, "setup", "offline")
    assert out["pending"] and not out["paired"] and out["secret_on_disk"]
    assert out["messages"] == 0
    assert out["status_line"] == "Setting up: waiting for the network"

    out = lua(plugin, sdir, "setup")
    assert out["paired"] and not out["secret_on_disk"]
    assert (plugin / "lectern_bootstrap.txt").exists() is False
    s = out["settings"]
    k = out["kosync"]
    assert k["custom_server"] == base + "/" and k["username"] == "owner"
    assert k["userkey"] == s["userkey"] and k["auto_sync"] is True
    assert k["pages_before_update"] == 10 and k["sync_forward"] == 2  # silent
    opds = out["opds"]
    assert opds[0]["url"] == base + "/opds" and opds[0]["password"] == s["app_password"]
    assert opds[1]["title"] == "Project Gutenberg"  # the reader's own entries stay
    assert out["menu"] >= 8
    assert http.post("/koplugin/bootstrap", json={"secret": secret}).status_code == 410
    assert httpx.get(f"{base}/users/auth", headers=ko("owner", s["userkey"])).status_code == 200

    # Offline in the reader: suspend and open neither prompt nor reach kosync; the push waits.
    out = lua(plugin, sdir, "offline-push")
    assert out["auto_sync"] is True and out["pages_before_update"] == 10
    assert out["kosync_calls"] == 0 and out["prompts"] == 0 and out["messages"] == 0
    assert out["outbox"] == 1
    assert out["status_line"] == "Offline · 1 position waiting to send"

    # Wi-Fi back: the queued position goes out quietly.
    out = lua(plugin, sdir, "reconnect")
    assert out["scheduled"] == 1 and out["outbox"] == 0 and out["messages"] == 0
    got = httpx.get(
        f"{base}/syncs/progress/0123456789abcdef0123456789abcdef",
        headers=ko("owner", s["userkey"]),
    ).json()
    assert got["percentage"] == 0.42 and got["device"] == "Kindle"

    # Wi-Fi up but the server unreachable (Tailscale down): keep, back off, stay quiet.
    down = tmp_path / "down"
    shutil.copytree(sdir, down)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{sock.getsockname()[1]}"
    out = lua(plugin, down, "server-down", dead)
    assert out["outbox"] == 1 and out["retry_in"] == 30 and out["retry_in_2"] == 60
    assert out["messages"] == 0
    assert out["status_line"] == "Retrying · 1 position waiting to send"

    # Books sent to this device: listed, and a download delivers the queue item.
    out = lua(plugin, sdir, "status")
    assert out["status"]["queue"]["pending"] == 1
    path = Path(out["downloaded"])
    assert path.name == "Alpha - Ann.epub" and zipfile.is_zipfile(path)
    assert http.get(f"/api/device-queues/{pw_id}").json()["pending"] == []

    # Re-pairing after a reset with a code from the web app.
    code = http.post(f"/api/devices/{dev_id}/pairing-code").json()["code"]
    wrong_code = f"{(int(code) + 1) % 1_000_000:06d}"
    out = lua(plugin, sdir, "pair", base, "owner", wrong_code)
    assert "did not work" in out["message"]
    out = lua(plugin, sdir, "pair", base + "/", "owner", code)
    assert out["message"].startswith("Paired") and out["kosync"]["userkey"] != s["userkey"]
    assert httpx.get(f"{base}/users/auth", headers=ko("owner", s["userkey"])).status_code == 401

    # Revoked in the web app: Sync now says so plainly.
    http.post(f"/api/devices/{dev_id}/revoke")
    out = lua(plugin, sdir, "sync-now")
    assert "refused this device" in out["message"]

    # No secret in anything the plugin logged, in any of the runs above.
    logged = "\n".join(LOGGED)
    assert "set up as device Kindle PW5" in logged
    for value in (secret, s["userkey"], s["app_password"], code):
        assert value not in logged
