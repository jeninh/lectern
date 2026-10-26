"""Opt-in home Wi-Fi access (docs/home-network.md, issue #32): the second listener answers only
private addresses and only the device protocols, with the same authentication; the plugin's
home-address proof; and the settings that switch it on.

Expected values come from the networks named in the issue (RFC 1918, link-local) and from the
secrets handed out here, not from the code under test.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, update
from tests.conftest import ORIGIN, api_post
from tests.test_koplugin import add_device, basic, get_zip, ko, secret_of, unzip

from lectern import lan as lan_module
from lectern.config import Settings
from lectern.db import session_factory
from lectern.lan import is_home_address
from lectern.models import AuditLog, DeviceBootstrap, utcnow

LAN_URL = "http://192.168.1.20:8080"
LAN_PORT = 8081


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        base_url=ORIGIN,
        insecure_http=True,
        lan_port=LAN_PORT,
        lan_url=LAN_URL,
    )


@pytest.fixture
def kindle(client, owner):
    dev = add_device(client, owner["csrf"], "Kindle PW5")
    md5 = hashlib.md5(dev["sync_secret"].encode(), usedforsecurity=False).hexdigest()
    return {"id": dev["device"]["id"], "opds": dev["opds_secret"], "userkey": md5}


def on_lan(app, ip: str) -> TestClient:
    """A client reaching the LAN listener (the socket's local port) from ``ip``."""
    return TestClient(app, base_url=f"http://192.168.1.20:{LAN_PORT}", client=(ip, 50000))


PRIVATE = ["192.168.1.30", "10.0.0.7", "172.16.0.1", "172.31.255.254", "169.254.10.1"]
PRIVATE_V6 = ["fe80::1", "fd12:3456::5", "::ffff:192.168.1.31"]
# The tailnet (CGNAT), loopback, public addresses, and the edges just outside 172.16.0.0/12
# and 192.168.0.0/16.
NOT_PRIVATE = ["100.101.102.103", "127.0.0.1", "::1", "203.0.113.5", "172.32.0.1", "192.169.0.1"]


@pytest.mark.parametrize("ip", PRIVATE + PRIVATE_V6)
def test_private_addresses_get_the_device_protocols_with_the_same_auth(app, client, kindle, ip):
    lan = on_lan(app, ip)
    assert lan.get("/opds", headers=basic("owner", kindle["opds"])).status_code == 200
    assert lan.get("/users/auth", headers=ko("owner", kindle["userkey"])).status_code == 200
    assert lan.get("/koplugin/status", headers=basic("owner", kindle["opds"])).status_code == 200
    assert lan.get("/healthz").status_code == 200
    # Same authentication as everywhere: no credentials, or wrong ones, are refused.
    assert lan.get("/opds").status_code == 401
    assert lan.get("/opds", headers=basic("owner", "wrong")).status_code == 401
    assert lan.get("/users/auth", headers=ko("owner", "0" * 32)).status_code == 401


@pytest.mark.parametrize("ip", NOT_PRIVATE)
def test_everything_else_is_refused_on_the_lan_listener(app, client, kindle, ip):
    lan = on_lan(app, ip)
    for path in ("/opds", "/healthz", "/users/auth", "/app/"):
        r = lan.get(path, headers=basic("owner", kindle["opds"]))
        assert r.status_code == 403, path
        assert "home network" in r.text
    # The main (Tailscale) listener is not affected by any of this.
    assert client.get("/opds", headers=basic("owner", kindle["opds"])).status_code == 200


def test_the_web_app_is_not_served_on_the_lan_listener(app, client, owner):
    lan = on_lan(app, "192.168.1.30")
    for method, path in (
        ("GET", "/"),
        ("GET", "/app/"),
        ("GET", "/login"),
        ("POST", "/login"),
        ("GET", "/api/me"),
        ("GET", "/setup"),
        ("POST", "/mcp"),
        ("GET", "/worker/install.sh"),
        ("GET", "/opdsx"),  # a prefix match must be a whole path segment
    ):
        r = lan.request(method, path)
        assert r.status_code == 403, (method, path)
        assert "Tailscale address" in r.text and "set-cookie" not in r.headers
    assert client.get("/api/me").status_code == 200  # the same session works on the main one


def test_rate_limits_and_audit_see_the_lan_address_and_proxy_headers_stay_untrusted(app, client):
    lan = on_lan(app, "192.168.1.30")
    r = lan.post(
        "/koplugin/bootstrap", json={"secret": "lb_nope"}, headers={"x-forwarded-for": "10.9.9.9"}
    )
    assert r.status_code == 410
    with session_factory()() as db:
        row = db.scalar(select(AuditLog).where(AuditLog.action == "koplugin.bootstrap_failed"))
    assert row.ip == "192.168.1.30"
    codes = [
        lan.post("/koplugin/bootstrap", json={"secret": "lb_x"}).status_code for _ in range(12)
    ]
    assert codes[-1] == 429  # the per-address limit applies on the LAN as well


# --- the plugin's home-address proof ------------------------------------------------------------


def expected_proof(secret: str, nonce: str) -> str:
    key = hashlib.sha256(secret.encode()).hexdigest().encode()
    return hmac.new(key, f"lectern-lan:{nonce}".encode(), hashlib.sha256).hexdigest()


def hello(http, device_id: str, nonce: str, setup: bool = False):
    return http.post(
        "/koplugin/hello", json={"device_id": device_id, "nonce": nonce, "setup": setup}
    )


def test_hello_proves_knowledge_of_the_devices_own_secret(app, client, owner, kindle):
    nonce = "0123456789abcdef" * 2
    lan = on_lan(app, "192.168.1.30")
    r = hello(lan, kindle["id"], nonce)
    assert r.status_code == 200
    assert r.json() == {"proof": expected_proof(kindle["opds"], nonce)}
    # A different nonce, a different answer (nothing to replay).
    other = hello(lan, kindle["id"], "f" * 32).json()["proof"]
    assert other == expected_proof(kindle["opds"], "f" * 32) != r.json()["proof"]

    # Before setup: keyed by the zip's one-time secret, while it can still be exchanged.
    secret = secret_of(unzip(get_zip(client, owner["csrf"], kindle["id"])))
    assert hello(lan, kindle["id"], nonce, setup=True).json() == {
        "proof": expected_proof(secret, nonce)
    }
    creds = lan.post("/koplugin/bootstrap", json={"secret": secret}).json()
    assert creds["lan_url"] == LAN_URL and creds["server_url"] == "http://100.101.102.103:8080"
    # The rotated app password keys it now; the old one does not.
    assert hello(lan, kindle["id"], nonce).json() == {
        "proof": expected_proof(creds["app_password"], nonce)
    }
    # Retries of the setup secret stay possible for 15 minutes, then it no longer keys anything.
    assert hello(lan, kindle["id"], nonce, setup=True).status_code == 200
    with session_factory()() as db:
        db.execute(update(DeviceBootstrap).values(used_at=utcnow() - timedelta(minutes=16)))
        db.commit()
    assert hello(lan, kindle["id"], nonce, setup=True).status_code == 404

    # Unknown or revoked devices get nothing.
    assert hello(lan, "k999", nonce).status_code == 404
    assert hello(lan, "p1", nonce).status_code == 404
    for odd in ("k²", "k¹1", "k１", "k 1", "k-1"):  # non-ASCII digits: 404, not a crash
        assert hello(lan, odd, nonce).status_code == 404, odd
    assert api_post(client, f"/api/devices/{kindle['id']}/revoke", owner["csrf"]).status_code == 200
    assert hello(lan, kindle["id"], nonce).status_code == 404
    for bad in ("short", "Z" * 32, "a" * 129):
        assert hello(lan, kindle["id"], bad).status_code == 422


def test_pairing_answers_with_the_tailscale_address_too(app, client, owner, kindle, monkeypatch):
    """A device that pairs over the home address needs a fallback that is not that address."""
    from lectern import koplugin

    monkeypatch.setattr(koplugin, "detect_tailscale_ip", lambda: "100.88.1.2")
    code = api_post(client, f"/api/devices/{kindle['id']}/pairing-code", owner["csrf"]).json()
    r = on_lan(app, "192.168.1.30").post(
        "/koplugin/pair", json={"username": "owner", "code": code["code"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["server_url"] == "http://100.88.1.2:8080"
    assert r.json()["lan_url"] == LAN_URL


# One table for both classifiers (lectern.lan.is_home_address, Core.homeLike in the plugin):
# True = never trusted as the unverified Tailscale fallback. Expected values follow the rule
# "private IPv4, any IPv6 literal, .local, or not canonically classifiable", not either code.
HOME_CASES = [
    ("http://192.168.7.7:8080", True),
    ("http://10.0.0.1:8080", True),
    ("http://172.16.0.1", True),
    ("http://172.31.255.255:80", True),
    ("http://169.254.1.1:8080", True),
    ("http://lectern.local:8080", True),
    ("http://lectern.local.:8080", True),  # DNS-root dot
    ("http://LECTERN.LOCAL..:8080", True),
    ("http://[::ffff:192.168.7.7]:8080", True),  # IPv4-mapped IPv6
    ("http://[fd7a:115c:a1e0::1]:8080", True),
    ("http://[fe80::1]:8080", True),
    ("http://10.1:8080", True),  # inet_aton: 10.0.0.1
    ("http://192.168.1:8080", True),
    ("http://3232237319:8080", True),  # one-number IPv4
    ("http://0x0a.0.0.1:8080", True),
    ("http://010.0.0.1:8080", True),  # octal in inet_aton
    ("http://user@100.101.102.103:8080", True),
    ("http://:8080", True),
    ("http://100.101.102.103:8080", False),  # the tailnet
    ("http://100.101.102.103.:8080", False),
    ("http://8.8.8.8:8080", False),
    ("http://172.32.0.1:8080", False),
    ("http://192.169.0.1:8080", False),
    ("http://lectern.tail1234.ts.net", False),
    ("https://lectern.example.:443", False),
]


@pytest.mark.parametrize(("url", "home"), HOME_CASES)
def test_python_home_classifier(url, home):
    assert is_home_address(url) is home


def test_plugin_home_classifier_agrees_on_every_case():
    luajit = shutil.which("luajit")
    if not luajit:
        if os.environ.get("LUAJIT_REQUIRED"):
            pytest.fail("LUAJIT_REQUIRED is set but luajit is not installed")
        pytest.skip("luajit not installed")
    plugin = Path(lan_module.__file__).parent / "koreader" / "lectern.koplugin"
    script = (
        f"package.path = {json.dumps(str(plugin) + '/?.lua;')} .. package.path "
        "local Core = require('lectern_core') "
        "for line in io.lines() do io.write(tostring(Core.homeLike(line)), '\\n') end"
    )
    run = subprocess.run(
        [luajit, "-e", script],
        input="\n".join(u for u, _ in HOME_CASES) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    got = run.stdout.split()
    assert dict(zip([u for u, _ in HOME_CASES], got, strict=True)) == {
        u: str(h).lower() for u, h in HOME_CASES
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.20:8080",
        "http://10.1.2.3:8080",
        "http://lectern.local:8080",
        "http://lectern.local.:8080",
        "http://10.1:8080",
        "http://[::ffff:192.168.1.20]:8080",
    ],
)
def test_the_zips_fallback_address_cannot_be_a_home_address(client, owner, kindle, url):
    """The plugin uses the zip's address without a check; the home one only after it."""
    r = get_zip(client, owner["csrf"], kindle["id"], url)
    assert r.status_code == 422 and r.json()["detail"] == "server_url_is_home"


def test_device_url_cannot_be_a_home_address_while_lan_access_is_on(tmp_path):
    with pytest.raises(ValidationError):
        Settings(
            data_dir=tmp_path,
            lan_port=LAN_PORT,
            lan_url=LAN_URL,
            device_url="http://192.168.1.20:8080",
        )
    off = Settings(data_dir=tmp_path, device_url="http://192.168.1.20:8080")  # LAN off: as before
    assert off.device_url == "http://192.168.1.20:8080"


def test_zip_status_and_defaults_carry_the_home_address(client, owner, kindle):
    device = unzip(get_zip(client, owner["csrf"], kindle["id"]))
    text = device["lectern.koplugin/lectern_device.lua"].decode()
    assert f'lan_url = "{LAN_URL}"' in text
    assert client.get("/api/devices/koplugin").json()["lan_url"] == LAN_URL
    st = client.get("/koplugin/status", headers=basic("owner", kindle["opds"])).json()
    assert st["lan_url"] == LAN_URL
    assert client.get("/api/admin/settings").json()["lan"] == {"enabled": True, "url": LAN_URL}


# --- settings -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8:8080",
        "http://0.0.0.0:8080",
        "http://100.64.1.1:8080",  # the tailnet, not the home network
        "http://lectern.example:8080",
        "https://192.168.1.20:8443",  # nothing terminates TLS on the LAN listener
        "http://192.168.1.20:8080/opds",
        "http://.local:8080",
    ],
)
def test_lan_url_must_be_a_home_network_address(tmp_path, url):
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, lan_port=LAN_PORT, lan_url=url)


@pytest.mark.parametrize(
    "url", ["http://192.168.1.20:8080", "http://lectern.local:8080", "http://[fd00::20]:8080"]
)
def test_home_network_addresses_are_accepted(tmp_path, url):
    assert Settings(data_dir=tmp_path, lan_port=LAN_PORT, lan_url=url + "/").lan_url == url


def test_lan_listener_needs_its_own_port(tmp_path):
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, port=8080, lan_port=8080)
    assert Settings(data_dir=tmp_path).lan_enabled is False  # off by default
