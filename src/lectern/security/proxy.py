"""Client IP resolution behind a trusted reverse proxy (cloudflared, Caddy, nginx).

Forwarding headers are honoured only when the socket peer is inside one of the networks in
``LECTERN_TRUSTED_PROXIES``. Precedence for a trusted peer: ``CF-Connecting-IP``, then the
first valid ``X-Forwarded-For`` hop, then the peer itself. An untrusted peer's headers are
ignored entirely, so a client talking to Lectern directly cannot pick its own address.

Every consumer of a client address (rate limits, login records, audit log, session rows) goes
through :func:`lectern.security.sessions.client_ip`, which calls :func:`resolve_client_ip`.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from ipaddress import IPv4Network, IPv6Network

Network = IPv4Network | IPv6Network

CF_HEADER = "cf-connecting-ip"
XFF_HEADER = "x-forwarded-for"


def parse_networks(spec: str) -> list[Network]:
    """Parse a comma-separated list of IPs or CIDRs. Raises ValueError on a bad entry."""
    networks: list[Network] = []
    for part in spec.split(","):
        part = part.strip()
        if part:
            networks.append(ipaddress.ip_network(part, strict=False))
    return networks


def parse_ip(value: str) -> str | None:
    """Canonical IP from one header value; ``None`` unless it is a bare IP (port allowed)."""
    v = value.strip()
    if not v:
        return None
    if v.startswith("[") and "]" in v:  # [2001:db8::1]:443
        v = v[1 : v.index("]")]
    elif v.count(":") == 1:  # 203.0.113.9:51234
        v = v.partition(":")[0]
    try:
        return str(ipaddress.ip_address(v))
    except ValueError:
        return None


def is_trusted_peer(peer: str | None, trusted: Sequence[Network]) -> bool:
    if not peer or not trusted:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:  # e.g. a Unix socket or the test client's "testclient"
        return False
    return any(addr in net for net in trusted)


def resolve_client_ip(
    peer: str | None, headers: Mapping[str, str], trusted: Sequence[Network]
) -> str | None:
    """The address to treat as the client: peer, or what a trusted proxy says it forwarded."""
    if not is_trusted_peer(peer, trusted):
        return peer
    forwarded = parse_ip(headers.get(CF_HEADER, ""))
    if forwarded:
        return forwarded
    for hop in headers.get(XFF_HEADER, "").split(","):
        forwarded = parse_ip(hop)
        if forwarded:
            return forwarded
    return peer
