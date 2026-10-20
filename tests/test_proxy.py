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

LOOPBACK = parse_networks("127.0.0.0/8, ::1")  # `tailscale serve` on the host or in a sidecar
CHAIN = parse_networks("127.0.0.0/8, ::1, 10.0.0.0/8")  # serve behind an internal proxy tier
CLOUDFLARED = parse_networks("172.18.0.0/16")


def _resolve(peer, trusted=LOOPBACK, **headers):
    return resolve_client_ip(
        peer, Headers({k.replace("_", "-"): v for k, v in headers.items()}), trusted
    )


# --- resolver ------------------------------------------------------------------------------


def test_untrusted_peer_headers_are_ignored():
    assert _resolve("203.0.113.9", cf_connecting_ip="198.51.100.1") == "203.0.113.9"
    assert _resolve("203.0.113.9", x_forwarded_for="198.51.100.1") == "203.0.113.9"
    assert _resolve("203.0.113.9", trusted=[], x_forwarded_for="198.51.100.1") == "203.0.113.9"
    # A tailnet peer reaching port 8080 directly (an e-reader) is not the proxy.
    assert _resolve("100.101.102.103", x_forwarded_for="198.51.100.1") == "100.101.102.103"
    # Not an IP at all (unix socket, test client): never trusted, returned as-is.
    assert _resolve("testclient", x_forwarded_for="198.51.100.1") == "testclient"
    assert _resolve(None, x_forwarded_for="198.51.100.1") is None


def test_tailscale_serve_single_hop():
    # serve replaces X-Forwarded-For with the caller's tailnet address.
    assert _resolve("127.0.0.1", x_forwarded_for="100.101.102.103") == "100.101.102.103"
    assert _resolve("::1", x_forwarded_for="fd7a:115c:a1e0::1") == "fd7a:115c:a1e0::1"
    assert _resolve("127.0.0.1") == "127.0.0.1"  # no header: the proxy itself


def test_xff_is_read_right_to_left_so_the_leftmost_entry_cannot_spoof():
    # The client sent "X-Forwarded-For: 192.0.2.5"; the proxy appended the real address.
    assert _resolve("127.0.0.1", x_forwarded_for="192.0.2.5, 198.51.100.7") == "198.51.100.7"
    # Spoofed entries in any number are still ignored.
    assert _resolve("127.0.0.1", x_forwarded_for="1.1.1.1, 2.2.2.2, 198.51.100.7") == (
        "198.51.100.7"
    )
    # Trusted hops to the right are skipped, the first untrusted one from the right wins.
    assert (
        _resolve("127.0.0.1", trusted=CHAIN, x_forwarded_for="192.0.2.5, 198.51.100.7, 10.0.0.2")
        == "198.51.100.7"
    )
    # An intermediate proxy that is NOT trusted is the client: nothing to its left is believed.
    assert _resolve("127.0.0.1", x_forwarded_for="198.51.100.7, 10.0.0.2") == "10.0.0.2"


def test_all_trusted_chain_falls_back_to_peer():
    assert _resolve("127.0.0.1", x_forwarded_for="127.0.0.1") == "127.0.0.1"
    assert _resolve("127.0.0.1", trusted=CHAIN, x_forwarded_for="10.0.0.2, 10.0.0.3") == (
        "127.0.0.1"
    )
    assert _resolve("10.0.0.9", trusted=CHAIN, x_forwarded_for="127.0.0.1, 10.0.0.2") == "10.0.0.9"


def test_malformed_hops_stop_the_walk_and_fall_back_to_peer():
    # Rightmost hop is junk: the proxy did not append an address, so trust nothing.
    assert _resolve("127.0.0.1", x_forwarded_for="198.51.100.7, unknown") == "127.0.0.1"
    assert _resolve("127.0.0.1", x_forwarded_for="198.51.100.7, 999.1.1.1") == "127.0.0.1"
    assert _resolve("127.0.0.1", x_forwarded_for="198.51.100.7, ") == "127.0.0.1"
    assert _resolve("127.0.0.1", x_forwarded_for="<script>") == "127.0.0.1"
    assert _resolve("127.0.0.1", x_forwarded_for=";;") == "127.0.0.1"
    assert _resolve("127.0.0.1", x_forwarded_for="   ") == "127.0.0.1"
    # Junk to the LEFT of the trusted-appended hop is irrelevant.
    assert _resolve("127.0.0.1", x_forwarded_for="unknown, , 198.51.100.7") == "198.51.100.7"
    # A junk hop between trusted ones ends the walk too.
    assert _resolve("127.0.0.1", trusted=CHAIN, x_forwarded_for="198.51.100.7, bad, 10.0.0.2") == (
        "127.0.0.1"
    )


def test_xff_accepts_port_and_bracket_forms_and_mixed_families():
    assert _resolve("127.0.0.1", x_forwarded_for="192.0.2.5, 198.51.100.7:51234") == (
        "198.51.100.7"
    )
    assert _resolve("127.0.0.1", x_forwarded_for="192.0.2.5, [2001:db8::9]:443") == "2001:db8::9"
    assert _resolve("127.0.0.1", x_forwarded_for="2001:db8::9, 198.51.100.7") == "198.51.100.7"
    assert _resolve("::1", trusted=CHAIN, x_forwarded_for="2001:DB8::9, 10.0.0.2") == "2001:db8::9"


def test_cf_connecting_ip_only_behind_a_trusted_peer_and_before_xff():
    assert (
        _resolve("172.18.0.7", trusted=CLOUDFLARED, cf_connecting_ip="2001:db8::9") == "2001:db8::9"
    )
    assert (
        _resolve("127.0.0.1", cf_connecting_ip="198.51.100.1", x_forwarded_for="192.0.2.5")
        == "198.51.100.1"
    )
    # Bad CF header -> XFF rule; untrusted peer -> ignored.
    assert _resolve("127.0.0.1", cf_connecting_ip="nope", x_forwarded_for="192.0.2.5") == (
        "192.0.2.5"
    )
    assert _resolve("172.19.0.7", trusted=CLOUDFLARED, cf_connecting_ip="2001:db8::9") == (
        "172.19.0.7"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" 192.0.2.5 ", "192.0.2.5"),
        ("192.0.2.5:51234", "192.0.2.5"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("2001:DB8::1", "2001:db8::1"),
        ("192.0.2.5/24", None),
        ("example.com", None),
        ("unknown", None),
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
    """App behind `tailscale serve` on loopback with setup done; the client is the proxy peer."""
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        base_url=ORIGIN,
        insecure_http=True,
        trusted_proxies="127.0.0.1",
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
    a = {"x_forwarded_for": "100.101.102.103"}
    # Two failures allowed per address, the third is throttled...
    assert [_failed_login(proxied, **a) for _ in range(3)] == [401, 401, 429]
    # ...but a different forwarded client is not, even though the peer socket is the same.
    assert _failed_login(proxied, x_forwarded_for="100.101.102.104") == 401
    # A client-supplied leftmost entry does not move it into a fresh bucket: the rightmost,
    # proxy-appended hop is the key, and it is already throttled.
    assert _failed_login(proxied, x_forwarded_for="198.51.100.9, 100.101.102.103") == 429
    # Audit rows carry the forwarded address, never the proxy's.
    assert _audit_ips("login.failed") == [
        "100.101.102.103",
        "100.101.102.103",
        "100.101.102.104",
    ]


def test_untrusted_peer_cannot_pick_its_own_address(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        base_url=ORIGIN,
        insecure_http=True,
        trusted_proxies="127.0.0.1",
    )
    app = create_app(settings, start_watcher=False)
    app.state.limiters["login"].limit = 2
    with TestClient(app, client=("100.64.0.9", 40000)) as c:
        codes = [_failed_login(c, x_forwarded_for=f"198.51.100.{i}") for i in range(3)]
    assert codes == [401, 401, 429]  # all keyed on the real peer despite distinct headers
    assert _audit_ips("login.failed") == ["100.64.0.9"] * 2  # the throttled one is not logged
