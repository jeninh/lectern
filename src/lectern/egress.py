"""Offline mode's choke point: while it is on, no socket in this process reaches past this machine.

Every outbound connection Python makes, whatever the library (``urllib``, ``httpx``,
``requests``, ``huggingface_hub``), goes through the ``socket`` module, which raises an audit
event (:pep:`578`) *before* it resolves a name or connects. (The server runs uvicorn on the
standard asyncio loop for this reason: uvloop connects and resolves in libuv, out of sight.) One audit hook refuses those events
while offline mode is on, so nothing has to remember to check: a new feature that talks to the
internet is refused here too. The hook raises :class:`OfflineError`, a
``ConnectionRefusedError``, so libraries report it as an ordinary connection failure and the
message says why.

Allowed while offline:

- Unix sockets and loopback (``127.0.0.0/8``, ``::1``, ``localhost``): the reverse proxy or
  ``tailscale serve`` in front of Lectern, and the health check;
- a UDP *connect* to Tailscale's resolver (:data:`TAILSCALE_PROBE`) and nowhere else: it sends
  nothing; KOReader plugin zips use it to learn Lectern's own Tailscale address
  (:func:`lectern.koplugin.detect_tailscale_ip`).
  ``send()`` on a connected socket raises no audit event, so a process could still send DNS
  queries to that one address this way; the uid-scoped firewall rules of
  ``deploy/offline-firewall.sh`` (docs/offline-mode.md) are what close that gap.

Everything else is refused: name lookups other than ``localhost`` (DNS is outbound traffic
too), reverse lookups, and connections to any other address, LAN included (a LAN proxy could
forward to the internet).

Child processes (the alignment child) inherit the switch through :data:`CHILD_ENV` and call
:func:`enforce_from_env`; ``HF_HUB_OFFLINE=1`` makes Hugging Face use only its cache there. (In
this process it is inert once ``huggingface_hub`` was imported, which reads it at import; the
hook still refuses its requests and it falls back to the cache.)
Programs that are not Python (ffmpeg, restic, Tesseract) are outside this hook; firewall rules give the
hard guarantee for those (docs/offline-mode.md).

An audit hook cannot be removed; turning offline mode off only makes it a no-op.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys

# Set (to "1") in this process's environment while offline mode is on, so child processes
# started meanwhile enforce it too.
CHILD_ENV = "LECTERN_OFFLINE_ENFORCED"
_ENV = {CHILD_ENV: "1", "HF_HUB_OFFLINE": "1"}
# Tailscale's resolver, which tailscaled routes through the tailnet interface in kernel TUN mode.
TAILSCALE_PROBE = ("100.100.100.100", 53)
_INET = (socket.AF_INET, socket.AF_INET6)
_LOOKUPS = ("socket.getaddrinfo", "socket.gethostbyname")
_REVERSE = ("socket.gethostbyaddr", "socket.getnameinfo")

_on = False
_hooked = False
_set_env: set[str] = set()  # the variables enforce() added, so turning off removes only those


class OfflineError(ConnectionRefusedError):
    def __init__(self, target: str) -> None:
        super().__init__(
            f"Lectern is in offline mode: refused to contact {target} "
            "(LECTERN_OFFLINE or Admin → Offline mode)"
        )
        self.target = target


def offline() -> bool:
    return _on


def enforce(on: bool) -> None:
    global _on, _hooked
    if on and not _hooked:
        sys.addaudithook(_hook)
        _hooked = True
    _on = on
    for key, value in _ENV.items():
        if on and key not in os.environ:
            os.environ[key] = value
            _set_env.add(key)
        elif not on and key in _set_env:
            os.environ.pop(key, None)
            _set_env.discard(key)


def enforce_from_env() -> None:
    """In a child process: enforce offline mode if the parent had it on."""
    if os.environ.get(CHILD_ENV) == "1":
        enforce(True)


def _local_name(host: str) -> bool:
    h = host.lower().rstrip(".")
    return h == "localhost" or h.endswith(".localhost")


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def _loopback(host: str) -> bool:
    ip = _ip(host)
    return _local_name(host) if ip is None else (ip.is_loopback or ip.is_unspecified)


def _text(host: object) -> str:
    return host.decode(errors="replace") if isinstance(host, bytes) else str(host)


def _hook(event: str, args: tuple) -> None:
    if not _on or not event.startswith("socket."):
        return
    if event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
        sock, address = args
        if sock.family not in _INET or not isinstance(address, tuple):
            return  # Unix sockets, netlink: never leave the machine
        host = _text(address[0])
        if _loopback(host):
            return
        if (
            event == "socket.connect"
            and sock.type == socket.SOCK_DGRAM
            and (host, address[1]) == TAILSCALE_PROBE
        ):
            return  # a UDP connect sends nothing (see the module docstring)
        raise OfflineError(f"{host}:{address[1]}" if len(address) > 1 else host)
    if event in _LOOKUPS:
        host = args[0]
        if host is None or _ip(_text(host)) is not None or _local_name(_text(host)):
            return  # no name to resolve: the connect is checked on its own
        raise OfflineError(_text(host))
    if event in _REVERSE:
        target = args[0][0] if isinstance(args[0], tuple) else args[0]
        if not _loopback(_text(target)):
            raise OfflineError(_text(target))
