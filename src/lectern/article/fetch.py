"""A bounded HTTP GET that only reaches the public internet, for saving web articles.

A URL someone pastes is fetched by the server, so it must not become a way to reach what only
the server can: Lectern itself on loopback, the Tailscale resolver and peers (100.64.0.0/10,
``fd7a:115c:a1e0::/48``), the home network, link-local metadata endpoints. So:

- ``http`` and ``https`` only, no credentials in the URL;
- the host name is resolved here, every address it resolves to must be a global unicast
  address (:func:`public_addresses`), and the connection goes to that checked address (the
  TLS handshake still verifies the certificate for the host name), so a second DNS answer
  cannot swap in a private address between the check and the connect;
- redirects are followed by hand, at most :data:`MAX_REDIRECTS`, each one checked again;
- no proxy from the environment (``http.client`` uses none);
- the body is read up to a byte cap, gzip/deflate decompressed up to the same cap, and the
  whole request (redirects included) has a deadline.

Offline mode needs nothing extra: :mod:`lectern.egress` refuses the name lookup and the
connect, and that comes back as :class:`FetchError` ``offline``.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
import zlib
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from lectern import egress

MAX_REDIRECTS = 5
CONNECT_TIMEOUT_S = 20.0
USER_AGENT = "Mozilla/5.0 (compatible; Lectern; +https://github.com/jeninh/lectern) save-article"
MAX_URL = 2048


class FetchError(Exception):
    """``code``: ``bad_url``, ``blocked_address``, ``offline``, ``unreachable``, ``timeout``,
    ``tls_error``, ``http_error`` (``status`` set), ``too_large``, ``too_many_redirects``,
    ``unsupported_encoding``."""

    def __init__(self, code: str, status: int | None = None) -> None:
        super().__init__(code if status is None else f"{code} {status}")
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Response:
    url: str  # after redirects
    media_type: str  # lowercased, without parameters
    charset: str | None
    data: bytes


def check_url(url: str) -> str:
    """The URL if it is one this module would fetch (``http``/``https``, a host, no
    credentials, a valid port); :class:`FetchError` ``bad_url`` otherwise."""
    url = url.strip()
    if not url or len(url) > MAX_URL or any(c in url for c in "\r\n\t "):
        raise FetchError("bad_url")
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise FetchError("bad_url") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("bad_url")
    if parts.username is not None or parts.password is not None or port == 0:
        raise FetchError("bad_url")
    try:
        _ip(parts.hostname)
    except ValueError:
        return url  # a name: checked when it is resolved (public_addresses)
    if not is_public(parts.hostname):
        raise FetchError("blocked_address")
    return url


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    ip = ipaddress.ip_address(text.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def _embedded(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """IPv4 addresses an IPv6 address carries (NAT64, 6to4, Teredo): a gateway may forward
    to them, so they must be public too."""
    out = []
    if any(ip in net for net in _NAT64):
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if ip.sixtofour is not None:
        out.append(ip.sixtofour)
    if ip.teredo is not None:
        out.extend(ip.teredo)
    return out


def is_public(text: str) -> bool:
    """A global unicast address: not loopback, private (RFC 1918), link-local (cloud metadata
    endpoints), shared (100.64.0.0/10, where Tailscale addresses and its resolver live),
    unique-local (Tailscale's IPv6), multicast, or reserved; and an IPv6 address that embeds
    an IPv4 one only when that one is public too."""
    try:
        ip = _ip(text)
    except ValueError:
        return False
    if not ip.is_global or ip.is_multicast:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        return all(v4.is_global and not v4.is_multicast for v4 in _embedded(ip))
    return True


def public_addresses(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to, if all of them are public; else
    :class:`FetchError` ``blocked_address`` (a name with one private answer is refused whole)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise FetchError("offline" if egress.offline() else "unreachable") from None
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    if not addresses or not all(is_public(a) for a in addresses):
        raise FetchError("blocked_address")
    return addresses


class _Plain(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _TLS(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise FetchError("timeout")
    return left


def _read(resp: http.client.HTTPResponse, sock, max_bytes: int, deadline: float) -> bytes:
    encoding = (resp.getheader("Content-Encoding") or "identity").strip().lower()
    if encoding in ("gzip", "x-gzip", "deflate"):
        inflate = zlib.decompressobj(zlib.MAX_WBITS | 32)  # gzip or zlib, detected
    elif encoding == "identity":
        inflate = None
    else:
        raise FetchError("unsupported_encoding")
    out = bytearray()
    received = 0
    while True:
        if sock is not None:
            sock.settimeout(min(CONNECT_TIMEOUT_S, _remaining(deadline)))
        block = resp.read(64 * 1024)
        if not block:
            break
        received += len(block)
        if received > max_bytes:
            raise FetchError("too_large")
        if inflate is None:
            out += block
        else:
            try:
                out += inflate.decompress(block, max_bytes + 1 - len(out))
            except zlib.error:
                raise FetchError("unreachable") from None
            if inflate.unconsumed_tail:
                raise FetchError("too_large")
        if len(out) > max_bytes:
            raise FetchError("too_large")
        _remaining(deadline)
    return bytes(out)


def get(url: str, *, max_bytes: int, accept: str, deadline: float) -> Response:
    """GET ``url`` (following redirects) and return the body, at most ``max_bytes`` of it,
    before ``deadline`` (``time.monotonic()``). Raises :class:`FetchError`."""
    for _ in range(MAX_REDIRECTS + 1):
        url = check_url(url)
        parts = urlsplit(url)
        tls = parts.scheme == "https"
        host = parts.hostname or ""
        port = parts.port or (443 if tls else 80)
        timeout = min(CONNECT_TIMEOUT_S, _remaining(deadline))
        address = public_addresses(host, port)[0]
        conn = (_TLS if tls else _Plain)(host, port, address, timeout)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        try:
            conn.request(
                "GET",
                target,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": accept,
                    "Accept-Encoding": "gzip, deflate",
                },
            )
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                if not location:
                    raise FetchError("http_error", resp.status)
                url = urljoin(url, location.strip())
                continue
            if resp.status != 200:
                raise FetchError("http_error", resp.status)
            declared = resp.getheader("Content-Length") or ""
            if declared.isdigit() and int(declared) > max_bytes:
                raise FetchError("too_large")
            data = _read(resp, conn.sock, max_bytes, deadline)
            media, _, params = (resp.getheader("Content-Type") or "").partition(";")
            charset = None
            for p in params.split(";"):
                key, _, value = p.partition("=")
                if key.strip().lower() == "charset" and value.strip():
                    charset = value.strip().strip("\"'")[:40]
            return Response(url, media.strip().lower(), charset, data)
        except FetchError:
            raise
        except TimeoutError:
            raise FetchError("timeout") from None
        except ssl.SSLError:
            raise FetchError("tls_error") from None
        except (OSError, http.client.HTTPException):
            raise FetchError("offline" if egress.offline() else "unreachable") from None
        finally:
            conn.close()
    raise FetchError("too_many_redirects")
