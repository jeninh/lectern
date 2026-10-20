"""Trusted-proxy client IP resolution: headers count only from configured peers."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.datastructures import Headers
from tests.conftest import ORIGIN, OWNER_PASSWORD, _TokenCapture, post_form

from lectern.config import Settings
from lectern.db import session_factory
from lectern.main import create_app
from lectern.models import AuditLog
from lectern.security.proxy import parse_ip, parse_networks, resolve_client_ip

LOOPBACK = parse_networks("127.0.0.0/8, ::1")
CLOUDFLARED = parse_networks("172.18.0.0/16")


def _resolve(peer, trusted=LOOPBACK, **headers):
    return resolve_client_ip(
        peer, Headers({k.replace("_", "-"): v for k, v in headers.items()}), trusted
    )


# --- resolver ------------------------------------------------------------------------------


def test_untrusted_peer_headers_are_ignored():
    assert _resolve("203.0.113.9", cf_connecting_ip="198.51.100.1") == "203.0.113.9"
    assert _resolve("203.0.113.9", x_forwarded_for="198.51.100.1") == "203.0.113.9"
    assert _resolve("203.0.113.9", trusted=[], cf_connecting_ip="198.51.100.1") == "203.0.113.9"
    # Not an IP at all (unix socket, test client): never trusted, returned as-is.
    assert _resolve("testclient", cf_connecting_ip="198.51.100.1") == "testclient"
    assert _resolve(None, cf_connecting_ip="198.51.100.1") is None


def test_trusted_peer_precedence_cf_then_first_xff_hop_then_peer():
    assert (
        _resolve("127.0.0.1", cf_connecting_ip="198.51.100.1", x_forwarded_for="192.0.2.5")
        == "198.51.100.1"
    )
    assert _resolve("127.0.0.1", x_forwarded_for="192.0.2.5, 10.0.0.2, 127.0.0.1") == "192.0.2.5"
    assert _resolve("127.0.0.1") == "127.0.0.1"
    assert _resolve("172.18.0.7", trusted=CLOUDFLARED, cf_connecting_ip="2001:db8::9") == (
        "2001:db8::9"
    )
    assert _resolve("172.19.0.7", trusted=CLOUDFLARED, cf_connecting_ip="2001:db8::9") == (
        "172.19.0.7"
    )


def test_malformed_headers_fall_through():
    # Bad CF header -> XFF; bad XFF hops skipped; nothing valid -> peer.
    assert _resolve("127.0.0.1", cf_connecting_ip="nope", x_forwarded_for="192.0.2.5") == (
        "192.0.2.5"
    )
    assert _resolve("127.0.0.1", x_forwarded_for="unknown, , 999.1.1.1, 192.0.2.5") == "192.0.2.5"
    assert _resolve("127.0.0.1", cf_connecting_ip="", x_forwarded_for=";;") == "127.0.0.1"
    assert _resolve("127.0.0.1", x_forwarded_for="<script>") == "127.0.0.1"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" 192.0.2.5 ", "192.0.2.5"),
        ("192.0.2.5:51234", "192.0.2.5"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("2001:DB8::1", "2001:db8::1"),
        ("192.0.2.5/24", None),
        ("example.com", None),
        ("", None),
    ],
)
def test_parse_ip(raw, expected):
    assert parse_ip(raw) == expected


def test_settings_reject_bad_networks(tmp_path):
    with pytest.raises(ValueError, match="network"):
        Settings(data_dir=tmp_path, library_dir=tmp_path, trusted_proxies="10.0.0.0/8, lan")
    s = Settings(data_dir=tmp_path, library_dir=tmp_path, trusted_proxies="")
    assert s.trusted_proxy_networks == []


# --- through the app: rate limit keys and audit rows use the resolved address -------------


@pytest.fixture
def proxied(tmp_path):
    """App behind a trusted proxy at 127.0.0.1 with setup done; the client is the proxy peer."""
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        base_url=ORIGIN,
        insecure_http=True,
        trusted_proxies="127.0.0.0/8",
    )
    app = create_app(settings, start_watcher=False)
    app.state.limiters["login"].limit = 2
    cap = _TokenCapture()
    logging.getLogger("lectern.setup").addHandler(cap)
    try:
        with TestClient(app, client=("127.0.0.1", 40000)) as c:
            form = {
                "token": cap.token,
                "username": "owner",
                "password": OWNER_PASSWORD,
                "password_confirm": OWNER_PASSWORD,
            }
            assert post_form(c, "/setup", form, follow_redirects=False).status_code == 303
            yield c
    finally:
        logging.getLogger("lectern.setup").removeHandler(cap)


def _failed_login(client, **headers):
    headers = {k.replace("_", "-"): v for k, v in headers.items()} | {"origin": ORIGIN}
    r = client.post("/login", data={"username": "nobody", "password": "x" * 12}, headers=headers)
    return r.status_code


def _audit_ips(action: str) -> list[str | None]:
    with session_factory()() as db:
        return list(
            db.scalars(select(AuditLog.ip).where(AuditLog.action == action).order_by(AuditLog.id))
        )


def test_trusted_proxy_rate_limits_and_audits_the_forwarded_address(proxied):
    cf = {"cf_connecting_ip": "198.51.100.1"}
    # Two failures allowed per address, the third is throttled...
    assert [_failed_login(proxied, **cf) for _ in range(3)] == [401, 401, 429]
    # ...but a different forwarded client is not, even though the peer socket is the same;
    # CF-Connecting-IP wins over X-Forwarded-For.
    assert (
        _failed_login(proxied, cf_connecting_ip="198.51.100.2", x_forwarded_for="198.51.100.1")
        == 401
    )
    # Without the Cloudflare header the first XFF hop is the key.
    xff = {"x_forwarded_for": "198.51.100.3, 10.0.0.2"}
    assert [_failed_login(proxied, **xff) for _ in range(3)] == [401, 401, 429]
    # Audit rows carry the forwarded address, never the proxy's.
    assert _audit_ips("login.failed") == [
        "198.51.100.1",
        "198.51.100.1",
        "198.51.100.2",
        "198.51.100.3",
        "198.51.100.3",
    ]


def test_untrusted_peer_cannot_pick_its_own_address(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        base_url=ORIGIN,
        insecure_http=True,
    )
    app = create_app(settings, start_watcher=False)
    app.state.limiters["login"].limit = 2
    with TestClient(app, client=("203.0.113.9", 40000)) as c:
        codes = [_failed_login(c, cf_connecting_ip=f"198.51.100.{i}") for i in range(3)]
    assert codes[-1] == 429  # all three keyed on the real peer despite distinct headers
