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
        "lan_url": None,  # home Wi-Fi access is off by default
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
    assert set(creds) == {
        "username",
        "device_id",
        "label",
        "userkey",
        "app_password",
        "server_url",
        "lan_url",
    }
    assert creds["lan_url"] is None  # home Wi-Fi access is off by default
    assert creds["username"] == "owner" and creds["label"] == "Kindle PW5"
    assert creds["server_url"] == SERVER and creds["device_id"] == kindle["id"]
    # KOReader sends its userkey as is; the new OPDS password works over Basic.
    assert re.fullmatch(r"[0-9a-f]{32}", creds["userkey"])
    assert client.get("/users/auth", headers=ko("owner", creds["userkey"])).status_code == 200
    assert client.get("/opds", headers=basic("owner", creds["app_password"])).status_code == 200
    # The previous credentials of this device stop working.
    assert client.get("/users/auth", headers=ko("owner", md5)).status_code == 401
    assert client.get("/opds", headers=basic("owner", old_opds)).status_code == 401

    # The response may never have reached the device: the same secret works again for 15
    # minutes after its first use, rotating again, so only the last holder's keys work.
    again = bootstrap(client, secret)
    assert again.status_code == 200, again.text
    retried = again.json()
    assert retried["userkey"] != creds["userkey"]
    assert client.get("/users/auth", headers=ko("owner", creds["userkey"])).status_code == 401
    assert client.get("/opds", headers=basic("owner", creds["app_password"])).status_code == 401
    assert client.get("/users/auth", headers=ko("owner", retried["userkey"])).status_code == 200
    with session_factory()() as db:
        db.execute(
            update(DeviceBootstrap).values(
                used_at=utcnow() - koplugin.BOOTSTRAP_RETRY - timedelta(seconds=1)
            )
        )
        db.commit()
    late = bootstrap(client, secret)
    assert late.status_code == 410 and late.json()["detail"] == "setup_secret_invalid"
    assert client.get("/users/auth", headers=ko("owner", retried["userkey"])).status_code == 200
    assert client.get("/api/devices").json()["devices"][0]["plugin"]["setup_state"] == "used"
    actions = [(a.action, a.detail) for a in audit_rows() if a.action.startswith("koplugin.")]
    assert actions == [
        ("koplugin.download", SERVER),
        ("koplugin.bootstrap", None),
        ("koplugin.bootstrap", "retry"),
        ("koplugin.bootstrap_failed", "used"),
    ]


def test_retry_window_does_not_outlive_a_new_zip_a_code_or_revocation(client, owner, kindle):
    csrf, dev = owner["csrf"], kindle["id"]
    for end in ("zip", "code", "revoke"):
        if end == "revoke":
            dev = add_device(client, csrf, "Kobo")["device"]["id"]
        secret = secret_of(unzip(get_zip(client, csrf, dev)))
        assert bootstrap(client, secret).status_code == 200
        if end == "zip":
            get_zip(client, csrf, dev)
        elif end == "code":
            c = new_code(client, csrf, dev)
            assert pair(client, c["code"]).status_code == 200
        else:
            assert api_post(client, f"/api/devices/{dev}/revoke", csrf).status_code == 200
        assert bootstrap(client, secret).status_code == 410, end


def test_a_retry_does_not_extend_the_window(client, owner, kindle):
    secret = secret_of(unzip(get_zip(client, owner["csrf"], kindle["id"])))
    assert bootstrap(client, secret).status_code == 200
    first_use = utcnow() - koplugin.BOOTSTRAP_RETRY + timedelta(seconds=30)
    with session_factory()() as db:
        db.execute(update(DeviceBootstrap).values(used_at=first_use))
        db.commit()
    assert bootstrap(client, secret).status_code == 200
    with session_factory()() as db:
        used_at = db.scalar(select(DeviceBootstrap.used_at))
    assert abs(used_at - first_use) < timedelta(seconds=1)


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
    # Malformed codes can never match, so they do not spend the code's attempts.
    c = new_code(client, csrf, dev)
    for junk in ("12", "abcdef", "1234567", "0", "99 9", "x"):
        assert pair(client, junk).status_code == 401
    assert pair(client, c["code"]).status_code == 200


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
    # Once the username's budget of wrong guesses is spent, even the right code waits.
    assert pair(client, c["code"]).status_code == 429
    app.state.limiters["koplugin_code"].limit = 1
    r = api_post(client, f"/api/devices/{kindle['id']}/pairing-code", owner["csrf"])
    assert r.status_code == 429


def test_nobody_can_lock_a_person_out_without_guessing_a_live_code(client, owner, kindle, app):
    """Regression: junk requests for a known username used to spend its 15-minute budget
    (and the live code's attempts), so a tailnet peer could lock pairing out indefinitely."""
    app.state.limiters["koplugin"].limit = 1000
    limit = app.state.limiters["koplugin_pair_user"].limit
    for i in range(limit * 3):
        pair(client, ["x", "12", "abcdefgh", f"{i:06d}"][i % 4])  # no code shown yet
    c = new_code(client, owner["csrf"], kindle["id"])
    for junk in ["x", "123", "1234567"] * limit:
        assert pair(client, junk).status_code == 401
    assert pair(client, c["code"]).status_code == 200


def test_a_successful_pairing_clears_the_usernames_budget(client, owner, kindle, app):
    app.state.limiters["koplugin"].limit = 1000
    app.state.limiters["koplugin_pair_user"].limit = 2
    c = new_code(client, owner["csrf"], kindle["id"])
    assert pair(client, wrong(c["code"])).status_code == 401
    assert pair(client, c["code"]).status_code == 200
    c = new_code(client, owner["csrf"], kindle["id"])
    assert pair(client, wrong(c["code"])).status_code == 401
    assert pair(client, c["code"]).status_code == 200


def test_unknown_users_and_rate_limits_are_audited_once(client, owner, kindle, app):
    app.state.limiters["koplugin"].limit = 2
    for _ in range(6):
        pair(client, "123456", "nobody")
    for _ in range(3):
        bootstrap(client, "lb_nope")
    rows = [(a.action, a.actor_user_id, a.detail) for a in audit_rows()]
    assert ("koplugin.pair_failed", None, "unknown_user (user 'nobody')") in rows
    assert rows.count(("koplugin.pair_failed", None, "rate_limited_address (user 'nobody')")) == 1
    assert rows.count(("koplugin.bootstrap_failed", None, "rate_limited")) == 1
    # Owners see them in Activity.
    actions = [e["action"] for e in client.get("/api/activity").json()["events"]]
    assert "koplugin.pair_failed" in actions and "koplugin.bootstrap_failed" in actions


def test_plugin_rows_do_not_leak_to_a_user_named_like_the_device(client, owner, app):
    bob = make_user(client, owner["csrf"], "bob", "member")
    dev = add_device(client, owner["csrf"], "bob")["device"]["id"]
    get_zip(client, owner["csrf"], dev)
    bob_http = TestClient(app)
    login(bob_http, "bob", bob["password"])
    events = bob_http.get("/api/activity").json()["events"]
    assert not [e for e in events if e["action"].startswith("koplugin.")]


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

    # Lectern not reachable at first start (Tailscale still connecting, or Wi-Fi off and
    # NetworkMgr saying so): setup tries, fails, and retries with backoff, quietly; the secret
    # stays.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{sock.getsockname()[1]}"
    early = tmp_path / "early"
    early.mkdir()
    (early / "lectern.lua").write_text(f'return {{ ["server_url"] = "{dead}" }}\n')
    for network in ("online", "offline"):
        out = lua(plugin, early, "setup-retry", network)
        assert out["pending"] and out["delays"] == [30, 60, 120] and out["messages"] == 0
        assert out["after_reconnect"] == 3
        assert out["status_line"] == "Setting up: will retry (connection refused)"
    assert (plugin / "lectern_bootstrap.txt").exists()

    # Sync now while it is not reachable says so, with the real error and the next try.
    out = lua(plugin, early, "sync-now", "offline")
    assert out["messages"] == 1 and out["prompts"] == 0
    assert "not reachable yet (connection refused)" in out["message"]
    assert out["retry_in"] == 60 and "by itself in 1 min" in out["message"]

    # NetworkMgr says offline but the network works (Wi-Fi managed outside KOReader on a
    # jailbroken Kindle, issue #45): setup still happens at first start.
    out = lua(plugin, sdir, "setup", "offline")
    assert out["paired"] and not out["secret_on_disk"] and not out["pending"]
    assert out["messages"] == 1
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
    with session_factory()() as db:
        assert db.scalar(select(DeviceBootstrap.used_at)) is not None
    assert httpx.get(f"{base}/users/auth", headers=ko("owner", s["userkey"])).status_code == 200

    # NetworkMgr says offline but the network works: the push goes out anyway, quietly.
    looks_offline = tmp_path / "looks-offline"
    shutil.copytree(sdir, looks_offline)
    out = lua(plugin, looks_offline, "offline-push")
    assert out["kosync_calls"] == 0 and out["prompts"] == 0 and out["messages"] == 0
    assert out["outbox"] == 0 and out.get("retry_in") is None
    assert out["settings"]["last_push_at"]

    # Offline in the reader: suspend and open neither prompt nor reach kosync; the push
    # waits, a retry is scheduled, and later pushes wait for it instead of trying each time.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{sock.getsockname()[1]}"
    out = lua(plugin, sdir, "offline-push", dead)
    assert out["auto_sync"] is True and out["pages_before_update"] == 10
    assert out["kosync_calls"] == 0 and out["prompts"] == 0 and out["messages"] == 0
    assert out["outbox"] == 1 and out["retry_in"] == 30
    assert out["status_line"] == "Offline · 1 position waiting to send"

    # Wi-Fi back: the queued position goes out quietly.
    out = lua(plugin, sdir, "reconnect")
    # The outbox retry, and the quiet check of the queue a little later.
    assert out["scheduled"] == 2 and out["fetch_in"] == 5
    assert out["outbox"] == 0 and out["messages"] == 0
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

    # A wrong address that answers 404 (another web server) keeps the queued position.
    wrong_server = tmp_path / "wrong"
    shutil.copytree(sdir, wrong_server)
    out = lua(plugin, wrong_server, "server-down", base + "/not-lectern")
    assert out["outbox"] == 1 and out["messages"] == 0

    # Books sent to this device: listed, and a download delivers the queue item without
    # replacing a book of the same name that is already there.
    (sdir / "books").mkdir()
    (sdir / "books" / "Alpha - Ann.epub").write_bytes(b"my annotated copy")
    out = lua(plugin, sdir, "status")
    assert out["status"]["queue"]["pending"] == 1
    path = Path(out["downloaded"])
    assert path.name == "Alpha - Ann (2).epub" and zipfile.is_zipfile(path)
    assert (sdir / "books" / "Alpha - Ann.epub").read_bytes() == b"my annotated copy"
    # Delivery is recorded once the server finished sending, which can trail the client a moment.
    for _ in range(50):
        if http.get(f"/api/device-queues/{pw_id}").json()["pending"] == []:
            break
        time.sleep(0.1)
    assert http.get(f"/api/device-queues/{pw_id}").json()["pending"] == []

    # Re-pairing after a reset with a code from the web app.
    code = http.post(f"/api/devices/{dev_id}/pairing-code").json()["code"]
    wrong_code = f"{(int(code) + 1) % 1_000_000:06d}"
    out = lua(plugin, sdir, "pair", base, "owner", wrong_code)
    assert "did not work" in out["message"]
    out = lua(plugin, sdir, "pair", base + "/", "owner", code)
    assert out["message"].startswith("Paired") and out["kosync"]["userkey"] != s["userkey"]
    assert httpx.get(f"{base}/users/auth", headers=ko("owner", s["userkey"])).status_code == 401

    # Sync now with NetworkMgr saying offline still syncs and says so (no Wi-Fi prompt).
    out = lua(plugin, sdir, "sync-now", "offline")
    assert out["prompts"] == 0 and out["message"].startswith("Synced with Lectern.")

    # Setup failed at start, then Sync now sets up: the start's backoff is dropped, so the
    # next automatic push goes out at once even while NetworkMgr says offline.
    z = http.post(f"/api/devices/{dev_id}/koplugin", json={"server_url": base})
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        (plugin / "lectern_bootstrap.txt").write_bytes(
            zf.read("lectern.koplugin/lectern_bootstrap.txt")
        )
    late = tmp_path / "late"
    late.mkdir()
    (late / "lectern.lua").write_text(f'return {{ ["server_url"] = "{dead}" }}\n')
    out = lua(plugin, late, "sync-now", "offline", "reachable-now")
    assert out["message"].startswith("Lectern is set up.")
    assert out["outbox_after_push"] == 0 and out["tasks_after_push"] == 0

    # Revoked in the web app: Sync now says so plainly.
    http.post(f"/api/devices/{dev_id}/revoke")
    out = lua(plugin, sdir, "sync-now")
    assert "refused this device" in out["message"]

    # No secret in anything the plugin logged, in any of the runs above.
    logged = "\n".join(LOGGED)
    assert "set up as device Kindle PW5" in logged
    for value in (secret, s["userkey"], s["app_password"], code):
        assert value not in logged


@needs_lua
def test_plugin_shelf_sync_downloads_and_cleans_up_quietly(live, settings, tmp_path):
    """Shelf sync (issue #68): with Auto-download on, the quiet check downloads the followed
    shelf's books without a prompt, within the device's hourly cap; with clean-up on, it
    deletes only its own unchanged copies of books that left the shelf, never the open book
    or the reader's own files."""
    http, base = live["http"], live["base"]
    LOGGED.clear()
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    for title in ("Alpha", "Bravo", "Charlie"):
        make_epub(settings.inbox_path / f"{title}.epub", title=title, authors=["Ann"])
    w = InboxWatcher(settings.inbox_path, settings.library_dir, session_factory(), 0.01)
    w.scan_once()
    w.scan_once()
    with session_factory()() as db:
        ids = {wk.title: wk.id for wk in db.scalars(select(Work))}
    dev = http.post("/api/devices", json={"label": "Kindle"}).json()
    dev_id, pw_id = dev["device"]["id"], dev["device"]["app_password"]["id"]
    shelf = http.post("/api/shelves", json={"name": "On the Kindle"}).json()
    for t in ("Alpha", "Bravo"):
        http.post(f"/api/shelves/{shelf['id']}/items", json={"work_id": ids[t]})

    def shelf_sync(**opts):
        body = {"shelf_id": shelf["id"], "cleanup": True, "auto_download": True, **opts}
        r = http.post(f"/api/devices/app-passwords/{pw_id}/shelf-sync", json=body)
        assert r.status_code == 200, r.text

    shelf_sync()
    z = http.post(f"/api/devices/{dev_id}/koplugin", json={"server_url": base})
    root = tmp_path / "koreader"
    (root / "plugins").mkdir(parents=True)
    (root / "settings").mkdir()
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        zf.extractall(root / "plugins")
    plugin, sdir = root / "plugins" / "lectern.koplugin", root / "settings"
    assert lua(plugin, sdir, "setup")["paired"]
    books = sdir / "books"
    books.mkdir()
    (books / "Mine.epub").write_bytes(b"the reader's own book")

    out = lua(plugin, sdir, "auto")
    assert out["planned"] == 2 and out["messages"] == 0
    assert out["fetch_in"] == 900 and out["again"] == 0 and 55 <= out["again_in"] <= 60
    alpha, bravo = books / "Alpha - Ann.epub", books / "Bravo - Ann.epub"
    assert zipfile.is_zipfile(alpha) and zipfile.is_zipfile(bravo)
    assert set(out["downloads"]) == {str(ids["Alpha"]), str(ids["Bravo"])}
    for _ in range(50):
        if http.get(f"/api/device-queues/{pw_id}").json()["pending"] == []:
            break
        time.sleep(0.1)
    assert http.get(f"/api/device-queues/{pw_id}").json()["pending"] == []
    # Nothing new: the next check downloads nothing again.
    assert lua(plugin, sdir, "auto")["planned"] == 0

    # Alpha leaves the shelf while it is open in the reader: kept until it is closed.
    http.delete(f"/api/shelves/{shelf['id']}/items/{ids['Alpha']}")
    out = lua(plugin, sdir, "auto", "reading", str(alpha))
    assert out["deleted"] == 0 and alpha.exists()
    out = lua(plugin, sdir, "auto")
    assert out["deleted"] == 1 and not alpha.exists() and out["messages"] == 0
    assert set(out["downloads"]) == {str(ids["Bravo"])}
    assert (books / "Mine.epub").read_bytes() == b"the reader's own book" and bravo.exists()

    # A copy the reader changed since (say, annotations saved into it) is theirs now: kept.
    with bravo.open("ab") as f:
        f.write(b"\0")
    http.delete(f"/api/shelves/{shelf['id']}/items/{ids['Bravo']}")
    out = lua(plugin, sdir, "auto")
    assert out["deleted"] == 0 and bravo.exists() and out["downloads"] == {}

    # Auto-download off: a new book on the shelf is queued but waits for a tap.
    shelf_sync(auto_download=False)
    http.post(f"/api/shelves/{shelf['id']}/items", json={"work_id": ids["Charlie"]})
    out = lua(plugin, sdir, "auto")
    assert out["planned"] == 0 and not (books / "Charlie - Ann.epub").exists()

    # On again, but the device's hourly cap (2) is used up: refused quietly, tried later.
    r = http.post(f"/api/devices/app-passwords/{pw_id}/download-cap", json={"cap": 2})
    assert r.status_code == 200, r.text
    shelf_sync()
    out = lua(plugin, sdir, "auto")
    assert out["planned"] == 1 and out["messages"] == 0
    assert not (books / "Charlie - Ann.epub").exists() and out["downloads"] == {}
    assert [
        i["work"]["title"] for i in http.get(f"/api/device-queues/{pw_id}").json()["pending"]
    ] == ["Charlie"]
    s = out["settings"]
    logged = "\n".join(LOGGED)
    assert "automatic download stopped: server 429" in logged
    for value in (s["userkey"], s["app_password"]):
        assert value not in logged


@needs_lua
def test_plugin_prefers_the_home_address_only_when_it_proves_itself(live, settings, tmp_path):
    """Home Wi-Fi access (docs/home-network.md): the plugin keeps both addresses, uses the home
    one when the Lectern there proves it is this device's, and otherwise Tailscale, without
    ever sending a credential to whatever else answers at that private address."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    http, base = live["http"], live["base"]
    port = base.rsplit(":", 1)[1]
    # The same server under a second name stands in for the home address (the LAN listener
    # itself refuses loopback; tests/test_lan.py covers it).
    home = f"http://localhost:{port}"
    settings.lan_port, settings.lan_url = 18081, home

    seen: list[tuple[str, str, dict]] = []

    def bare_plugin() -> Path:
        """The plugin files without a zip's device file or setup secret."""
        bare = tmp_path / "bare-plugin"
        if not bare.exists():
            shutil.copytree(plugin, bare)
            (bare / "lectern_device.lua").unlink(missing_ok=True)
            (bare / "lectern_bootstrap.txt").unlink(missing_ok=True)
        return bare

    class Impostor(BaseHTTPRequestHandler):
        """Another network's machine at the same private address: answers everything."""

        def _answer(self):
            seen.append((self.command, self.path, dict(self.headers)))
            body = b'{"proof": "' + b"0" * 64 + b'"}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = _answer

        def log_message(self, *args):
            pass

    impostor = ThreadingHTTPServer(("127.0.0.1", 0), Impostor)
    threading.Thread(target=impostor.serve_forever, daemon=True).start()
    impostor_url = f"http://127.0.0.1:{impostor.server_address[1]}"

    dev = http.post("/api/devices", json={"label": "Kindle"}).json()
    z = http.post(f"/api/devices/{dev['device']['id']}/koplugin", json={"server_url": base})
    root = tmp_path / "koreader"
    (root / "plugins").mkdir(parents=True)
    (root / "settings").mkdir()
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        zf.extractall(root / "plugins")
    plugin, sdir = root / "plugins" / "lectern.koplugin", root / "settings"

    # First start at home: set up over the home address, and KOReader is set to it.
    out = lua(plugin, sdir, "setup")
    assert out["paired"]
    s = out["settings"]
    assert s["server_url"] == base and s["lan_url"] == home and s["active_url"] == home
    assert out["kosync"]["custom_server"] == home + "/"
    # The catalogue stays on Tailscale: KOReader's open OPDS browser keeps copies of links and
    # credentials that the plugin cannot take back when it leaves home.
    assert out["opds"][0]["url"] == base + "/opds" and len(out["opds"]) == 2
    with session_factory()() as db:
        assert db.scalar(select(DeviceBootstrap.used_ip)) == "127.0.0.1"

    # Away: something else answers at the home address and fails the proof. Tailscale is used,
    # and the impostor saw only the nonce check, never a credential.
    settings.lan_url = impostor_url
    away = tmp_path / "away"
    shutil.copytree(sdir, away)
    lectern_lua = away / "lectern.lua"
    lectern_lua.write_text(lectern_lua.read_text().replace(home, impostor_url))
    out = lua(plugin, away, "route")
    # The last run trusted the home address; this start does not, before any check: KOReader's
    # own sync and catalogue point at Tailscale at once, and stay there across the reconnect.
    assert out["kosync_at_start"] == base + "/" and out["kosync_after_connect"] == base + "/"
    assert out["early_status"] is True  # a request before the scheduled check: Tailscale
    assert out["active"] == base and out["status"] is True
    assert out["settings"].get("active_url", "") == ""
    assert out["kosync"]["custom_server"] == base + "/"
    assert out["opds"][0]["url"] == base + "/opds" and len(out["opds"]) == 2
    assert seen and all(m == "POST" and p == "/koplugin/hello" for m, p, _ in seen)
    for _, _, headers in seen:
        lowered = {k.lower(): v for k, v in headers.items()}
        assert "authorization" not in lowered and "x-auth-key" not in lowered
        sent = json.dumps(headers)
        assert s["app_password"] not in sent and s["userkey"] not in sent

    # Home address unreachable: Tailscale, quietly.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{sock.getsockname()[1]}"
    settings.lan_url = dead
    lectern_lua.write_text(lectern_lua.read_text().replace(impostor_url, dead))
    out = lua(plugin, away, "route")
    assert out["active"] == base and out["status"] is True

    # Back home: the home address again.
    settings.lan_url = home
    out = lua(plugin, sdir, "route")
    assert out["active"] == home and out["kosync"]["custom_server"] == home + "/"

    # Home Wi-Fi access turned off on the server: the plugin forgets the address.
    settings.lan_port = 0
    out = lua(plugin, sdir, "route")
    assert out["status"] is True and out["settings"]["lan_url"] == ""
    out = lua(plugin, sdir, "route")
    assert out["active"] == base and out["kosync"]["custom_server"] == base + "/"

    # Re-paired with a code over the home address: that address does not become the Tailscale
    # fallback, which is used without a check.
    settings.lan_port, settings.lan_url = 18081, home
    paired = tmp_path / "paired-at-home"
    shutil.copytree(sdir, paired)
    code = http.post(f"/api/devices/{dev['device']['id']}/pairing-code").json()["code"]
    out = lua(plugin, paired, "pair", home, "owner", code)
    assert out["message"].startswith("Paired"), out["message"]
    s = out["settings"]
    assert s["server_url"] == base and s["lan_url"] == home and s["active_url"] == home
    # Then another machine answers at the home address: only the nonce check reaches it.
    seen.clear()
    settings.lan_url = impostor_url
    lectern_lua = paired / "lectern.lua"
    lectern_lua.write_text(lectern_lua.read_text().replace(home, impostor_url))
    out = lua(plugin, paired, "route")
    assert out["active"] == base and out["status"] is True
    assert out["kosync"]["custom_server"] == base + "/"
    assert seen and all(m == "POST" and p == "/koplugin/hello" for m, p, _ in seen)
    for _, _, headers in seen:
        sent = json.dumps(headers).lower()
        assert "authorization" not in sent and "x-auth-key" not in sent
        assert s["app_password"].lower() not in sent and s["userkey"].lower() not in sent

    # A stored fallback that is itself a home-network address (a zip made while home Wi-Fi
    # access was off, with a private LECTERN_DEVICE_URL) and a home address that fails the
    # check: KOReader's own sync and catalogue lose their credentials instead of pointing at
    # an unverified host (a placeholder name is no barrier; DNS can answer anything).
    stale = tmp_path / "stale-fallback"
    shutil.copytree(paired, stale)
    lectern_lua = stale / "lectern.lua"
    lectern_lua.write_text(
        lectern_lua.read_text()
        .replace(f'"{base}"', '"http://192.168.7.7:8080"')
        .replace(impostor_url, dead)
    )
    seen.clear()
    out = lua(plugin, stale, "route")
    assert out.get("active") is None and out["status"] is False
    assert "userkey" not in out["kosync"] and "custom_server" not in out["kosync"]
    lectern_entry = [e for e in out["opds"] if e.get("lectern_device")]
    assert lectern_entry and "password" not in lectern_entry[0]
    assert "192.168.7.7" not in json.dumps(out["opds"])

    # The server stops announcing a home address (lan_url: null) while the stored fallback is
    # itself home-like: it stays untrusted for good, so KOReader's sync and catalogue lose
    # their credentials and nothing is sent to it. ("127.1" is how a shortened IPv4 alias of
    # this server looks; the classifier treats it like any home address.)
    alias = f"http://127.1:{port}"
    downgraded = tmp_path / "downgraded"
    shutil.copytree(paired, downgraded)
    lectern_lua = downgraded / "lectern.lua"
    lectern_lua.write_text(
        lectern_lua.read_text().replace(f'"{base}"', f'"{alias}"').replace(impostor_url, home)
    )
    settings.lan_port, settings.lan_url = 18081, home
    out = lua(plugin, downgraded, "route")
    assert out["status"] is True and out["active"] == home  # verified home, as before
    settings.lan_port = 0  # the next status announces lan_url: null
    out = lua(plugin, downgraded, "route")
    assert out["settings"]["lan_url"] == "" and out["settings"]["lan_seen"] is True
    assert "userkey" not in out["kosync"] and "custom_server" not in out["kosync"]
    out = lua(plugin, downgraded, "route")
    assert out.get("active") is None and out["status"] is False  # no request went out
    assert "userkey" not in out["kosync"]

    # Malformed server addresses the socket library would still resolve (a service-name port,
    # unbracketed IPv6) are refused before any request.
    aliased_bad = tmp_path / "aliased-bad"
    aliased_bad.mkdir()
    for bad in ("http://127.1:http-alt", "http://lectern.local:http-alt", f"http://::1:{port}"):
        out = lua(bare_plugin(), aliased_bad, "pair", bad, "owner", "123456")
        assert out["message"].startswith("Enter the server address"), (bad, out["message"])
        assert not out["paired"]

    # Pairing through such an alias of the home address is refused as well.
    settings.lan_port, settings.lan_url = 18081, home
    aliased = tmp_path / "aliased"
    aliased.mkdir()
    code = http.post(f"/api/devices/{dev['device']['id']}/pairing-code").json()["code"]
    out = lua(bare_plugin(), aliased, "pair", alias, "owner", code)
    assert out["message"].startswith("Not paired"), out["message"]
    assert not out["paired"]

    # Pairing over the home address with no Tailscale address known (the server detects none,
    # the plugin has no zip file): refused, rather than keeping the home address as fallback.
    bare = bare_plugin()
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    code = http.post(f"/api/devices/{dev['device']['id']}/pairing-code").json()["code"]
    settings.lan_url = home
    out = lua(bare, fresh, "pair", home, "owner", code)
    assert out["message"].startswith("Not paired"), out["message"]
    assert not out["paired"] and "server_url" not in out["settings"]
    impostor.shutdown()


@needs_lua
def test_plugin_sends_highlights_and_notes(live, settings, tmp_path):
    """Issue #65: the open book's highlights go with its position pushes and shortly after an
    edit, wait in the outbox while Lectern is unreachable, are sent only when they changed, and
    Sync now also picks up recently read books' sidecars. Their text is never logged."""
    from tests.test_annotations import OTHER, PHRASE, xp
    from tests.test_jobs import ingest_fixture

    from lectern.ingest.sentences import ensure_text_sentences
    from lectern.models import Annotation, BookFile

    http, base = live["http"], live["base"]
    LOGGED.clear()
    work_id = ingest_fixture(settings, tmp_path, audio=False)
    with session_factory()() as db:
        ensure_text_sentences(db, settings.library_dir, work_id)
        db.commit()
        digest = db.scalar(select(BookFile.partial_md5).where(BookFile.work_id == work_id))
    dev = http.post("/api/devices", json={"label": "Kindle PW5"}).json()
    z = http.post(f"/api/devices/{dev['device']['id']}/koplugin", json={"server_url": base})
    root = tmp_path / "koreader"
    (root / "plugins").mkdir(parents=True)
    (root / "settings").mkdir()
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        zf.extractall(root / "plugins")
    plugin, sdir = root / "plugins" / "lectern.koplugin", root / "settings"
    assert lua(plugin, sdir, "setup")["paired"]

    def run(spec: dict, settings_dir: Path = sdir) -> dict:
        path = tmp_path / "spec.json"
        path.write_text(json.dumps({"digest": digest, **spec}))
        return lua(plugin, settings_dir, "highlights", str(path))

    def shown() -> list[dict]:
        return http.get(f"/api/works/{work_id}/annotations").json()["annotations"]

    note = "Della's savings, again"
    first = {"datetime": "2026-10-01 09:30:00", "drawer": "lighten", "color": "yellow",
             "text": PHRASE, "chapter": "The Gift of the Magi", "pageno": 3,
             "pos0": xp(PHRASE), "pos1": xp(PHRASE)}  # fmt: skip
    second = {**first, "datetime": "2026-10-01 09:41:12", "text": OTHER, "note": note,
              "pos0": xp(OTHER), "pos1": xp(OTHER)}  # fmt: skip
    bookmark = {"datetime": "2026-10-01 09:50:00", "page": "/body/DocFragment[3]", "text": "x"}

    # A page-turn push sends the position and the book's highlights (not its bookmarks).
    out = run({"action": "push", "annotations": [first, second, bookmark]})
    assert out["requests"] == 1 and out["pending_books"] == 0
    got = shown()
    assert [a["text"] for a in got] == [PHRASE, OTHER] and got[1]["note"] == note
    assert got[0]["location"]["char"] > 0 and got[0]["device"] == "Kindle"

    # Nothing changed: the next push does not send them again.
    out = run({"action": "push", "annotations": [first, second, bookmark]})
    assert out["requests"] == 0

    # Edited and deleted on the device: sent 15 s later, and Lectern follows.
    edited = {**second, "note": "changed my mind", "datetime_updated": "2026-10-02 08:00:00"}
    out = run({"action": "modified", "annotations": [edited]})
    assert out["scheduled_in"] == 15 and out["before_task"] == 0 and out["requests"] == 1
    assert [(a["text"], a["note"]) for a in shown()] == [(OTHER, "changed my mind")]

    # Lectern not reachable: the highlights wait, quietly, and go out when Wi-Fi is back.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{sock.getsockname()[1]}"
    out = run({"action": "push", "annotations": [first, edited], "server_url": dead,
               "reconnect": True})  # fmt: skip
    assert out["pending_books"] == 1 and out["retry_in"] == 30
    assert out["status_line"] == "Retrying · 1 position and highlights of 1 book waiting to send"
    assert out["after_reconnect"] == 1 and out["pending_after_reconnect"] == 0
    assert [a["text"] for a in shown()] == [PHRASE, OTHER]

    # Sync now: the open book always, plus recently read books whose sidecars changed.
    other_doc = "a" * 32
    history = [{"file": "/mnt/us/books/Other.epub", "digest": other_doc,
                "annotations": [{**first, "datetime": "2026-09-01 07:00:00"}]},
               {"file": "/mnt/us/books/Plain.epub", "digest": "b" * 32, "annotations": []}]  # fmt: skip
    out = run({"action": "sync-now", "annotations": [first, edited], "history": history})
    assert out["requests"] == 2, out  # the open book (forced) and Other.epub; not Plain.epub
    assert "Highlights and notes of 2 book(s) sent." in out["message"]
    with session_factory()() as db:
        assert db.scalar(select(Annotation.text).where(Annotation.document == other_doc)) == PHRASE
    assert len(shown()) == 2  # a book that is not in the library shows nowhere

    # Deleted all of them in the open book: an empty list clears Lectern's copy.
    out = run({"action": "modified", "annotations": []})
    assert out["requests"] == 1 and shown() == []

    # Re-paired (possibly another sync key): books already sent are sent again on the next push.
    out = run({"action": "push", "annotations": [first]})
    assert out["requests"] == 1
    assert run({"action": "push", "annotations": [first]})["requests"] == 0
    code = http.post(f"/api/devices/{dev['device']['id']}/pairing-code").json()["code"]
    assert lua(plugin, sdir, "pair", base, "owner", code)["paired"]
    assert run({"action": "push", "annotations": [first]})["requests"] == 1

    logged = "\n".join(LOGGED)
    for private in (PHRASE, OTHER, note, "changed my mind"):
        assert private not in logged
