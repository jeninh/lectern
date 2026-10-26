"""Opt-in home Wi-Fi (LAN) access for e-readers without Tailscale (docs/home-network.md).

With ``LECTERN_LAN_PORT`` set, the server listens on a second port, which
``deploy/tailscale/lan.yaml`` publishes on the host's LAN address only. :class:`LANGuard`
makes that listener answer:

* only clients with a private source address (RFC 1918, IPv4 link-local, IPv6 link-local and
  unique-local); everything else, the tailnet's 100.64.0.0/10 included, gets 403;
* only the device protocols: OPDS, KOReader progress sync, the KOReader plugin's routes, and
  ``/healthz``. The web app stays on the canonical (Tailscale HTTPS) base URL: its cookies are
  Secure, and passkeys and CSRF checks are bound to that origin.

Authentication, rate limits, alerts, and the trusted-proxy rules are the same as on the main
listener: nothing here grants access, it only refuses.

The plugin checks it is talking to *this* Lectern before it sends a credential to the home
address (another Wi-Fi may have a different machine at the same private IP):
``POST /koplugin/hello`` answers a nonce with :func:`proof`, an HMAC keyed by the SHA-256 of a
secret the device holds (its app password, or before setup its one-time setup secret). The
server stores exactly those hashes, so it can answer without knowing the secret, and nobody
without the hash can.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

PRIVATE_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "fe80::/10",
        "fc00::/7",
    )
)

# Path prefixes served on the LAN listener: OPDS, kosync (/users/auth, /users/create,
# /syncs/progress), the plugin's bootstrap/pair/status/hello, and liveness.
DEVICE_PATHS = ("/opds", "/syncs", "/users", "/koplugin", "/healthz")

PROOF_CONTEXT = "lectern-lan:"


def is_private_ip(value: str | None) -> bool:
    """True for RFC 1918, link-local, and IPv6 unique-local addresses (IPv4-mapped too)."""
    try:
        addr = ipaddress.ip_address(value or "")
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in PRIVATE_NETS)


def is_device_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in DEVICE_PATHS)


def normalize_lan_url(value: str) -> str:
    """``http://host[:port]`` where host is a private IP or a ``.local`` (mDNS) name."""
    from lectern.koplugin import normalize_server_url

    url = normalize_server_url(value)
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "http":
        raise ValueError("invalid_lan_url")
    if not (is_private_ip(host) or (host.endswith(".local") and len(host) > len(".local"))):
        raise ValueError("invalid_lan_url")
    return url


def is_home_address(url: str) -> bool:
    """A private IP or ``.local`` host (any scheme): what the plugin never trusts unverified."""
    host = urlsplit(url).hostname or ""
    return is_private_ip(host) or (host.endswith(".local") and len(host) > len(".local"))


def proof(key_hash: str, nonce: str) -> str:
    """HMAC-SHA256 of the nonce, keyed by the hex SHA-256 of the device's secret."""
    return hmac.new(key_hash.encode(), (PROOF_CONTEXT + nonce).encode(), hashlib.sha256).hexdigest()


class LANGuard:
    """Refuse non-private clients and non-device paths on the LAN listener (pure ASGI, so it
    runs before routing, sessions, and every other middleware)."""

    def __init__(self, app: ASGIApp, lan_port: int) -> None:
        self.app = app
        self.lan_port = lan_port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and self.lan_port:
            server = scope.get("server")
            if server and server[1] == self.lan_port:
                client = scope.get("client")
                if not is_private_ip(client[0] if client else None):
                    return await _refuse(scope, send, "Lectern answers only the home network here.")
                if not is_device_path(scope.get("path", "")):
                    return await _refuse(
                        scope,
                        send,
                        "This is Lectern's home Wi-Fi address for e-readers (catalogue and "
                        "progress sync). Open Lectern at its Tailscale address instead.",
                    )
        await self.app(scope, receive, send)


async def _refuse(scope: Scope, send: Send, text: str) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    body = (text + "\n").encode()
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
