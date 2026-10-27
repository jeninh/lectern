"""When the worker may run: power, network, and the proxy to reach the server through.

The probes call macOS tools (``pmset``, ``networksetup``, ``route``, ``ipconfig``, ``scutil``)
and the parsers are plain functions over their output, so the rules are tested anywhere. On
other systems power is unknown (never a reason to pause) and Wi-Fi cannot be detected, so
"Only on Wi-Fi" pauses there.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from lectern.worker.config import AgentConfig


@dataclass(frozen=True)
class Power:
    on_ac: bool
    percent: int | None  # None: no battery (a desktop Mac) or unknown


@dataclass(frozen=True)
class Network:
    on_wifi: bool
    ssid: str | None  # None: not on Wi-Fi, or macOS would not say (Location permission)


def parse_pmset(text: str) -> Power | None:
    """``pmset -g batt``: ``Now drawing from 'AC Power'`` / ``'Battery Power'`` and
    ``…InternalBattery-0 (id=…)\t54%; discharging; …``."""
    m = re.search(r"Now drawing from '([^']+)'", text)
    if not m:
        return None
    pct = re.search(r"(\d{1,3})%;", text)
    return Power(on_ac=m.group(1) != "Battery Power", percent=int(pct.group(1)) if pct else None)


def parse_wifi_device(text: str) -> str | None:
    """``networksetup -listallhardwareports``: the device of the ``Wi-Fi`` port."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() in ("Hardware Port: Wi-Fi", "Hardware Port: AirPort"):
            for nxt in lines[i + 1 : i + 3]:
                if nxt.startswith("Device:"):
                    return nxt.split(":", 1)[1].strip() or None
    return None


def parse_default_interface(text: str) -> str | None:
    """``route -n get default``: ``interface: en0``."""
    m = re.search(r"^\s*interface:\s*(\S+)", text, re.M)
    return m.group(1) if m else None


def parse_ssid(text: str) -> str | None:
    """``ipconfig getsummary <dev>``: ``  SSID : Home`` (``<redacted>`` without permission)."""
    m = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", text, re.M)
    if not m or m.group(1) in ("<redacted>", ""):
        return None
    return m.group(1)


def _scutil_global(text: str) -> str:
    """``scutil --proxy`` without its per-interface ``__SCOPED__`` settings (listed last)."""
    return text.split("__SCOPED__", 1)[0]


def parse_scutil_proxy(text: str) -> str | None:
    """``scutil --proxy``: the HTTPS proxy as ``http://host:port`` when it is enabled. PAC
    files (``ProxyAutoConfigEnable``) are not evaluated: set the proxy URL explicitly."""
    text = _scutil_global(text)
    values = dict(re.findall(r"^\s*(\w+)\s*:\s*(\S+)\s*$", text, re.M))
    if values.get("HTTPSEnable") == "1" and values.get("HTTPSProxy"):
        port = values.get("HTTPSPort") or "443"
        return f"http://{values['HTTPSProxy']}:{port}"
    return None


def parse_scutil_exceptions(text: str) -> tuple[list[str], bool]:
    """``scutil --proxy``: the macOS proxy exception list (``ExceptionsList``, "Bypass proxy
    settings for these Hosts & Domains") and whether simple hostnames are excluded."""
    text = _scutil_global(text)
    block = re.search(r"ExceptionsList\s*:\s*<array>\s*\{(.*?)\}", text, re.S)
    items = re.findall(r"^\s*\d+\s*:\s*(.+?)\s*$", block.group(1), re.M) if block else []
    simple = re.search(r"^\s*ExcludeSimpleHostnames\s*:\s*1\s*$", text, re.M) is not None
    return items, simple


# Tailscale's address ranges (CGNAT IPv4 and its IPv6 ULA prefix). No proxy reaches them.
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _resolve(host: str) -> list[str]:
    try:
        return [ai[4][0] for ai in socket.getaddrinfo(host, None)]
    except (OSError, UnicodeError):
        return []


def on_tailnet(host: str) -> bool:
    """``host`` is a Tailscale address, or a name that resolves to one right now (a MagicDNS
    name while Tailscale is up; with Tailscale down a Funnel name resolves publicly)."""
    addrs = [host] if _ip(host) else _resolve(host)
    return any((ip := _ip(a)) is not None and any(ip in n for n in TAILNET) for a in addrs)


def matches_exception(host: str, pattern: str) -> bool:
    """``host`` matches one macOS exception list or ``NO_PROXY`` entry: ``*``, a host or IP,
    a domain (``example.com``, ``.example.com``, ``*.example.com`` match its subdomains), a
    wildcard (``192.168.*``), or a network (``10.0.0.0/8``, macOS's short ``169.254/16``)."""
    host, pattern = host.strip("[]").lower().rstrip("."), pattern.strip().lower()
    if pattern.startswith("["):  # [::1] or [::1]:8080
        pattern = pattern[1:].partition("]")[0]
    elif pattern.count(":") == 1:  # host:8080 (a port is not compared)
        pattern = pattern.partition(":")[0]
    if not pattern:
        return False
    if pattern == "*":
        return True
    if "/" in pattern:
        addr, _, bits = pattern.partition("/")
        if ":" not in addr:
            addr = ".".join((addr.split(".") + ["0"] * 4)[:4])
        try:
            net = ipaddress.ip_network(f"{addr}/{bits}", strict=False)
        except ValueError:
            return False
        ip = _ip(host)
        return ip is not None and ip.version == net.version and ip in net
    if "*" in pattern.lstrip("*."):
        return fnmatch.fnmatchcase(host, pattern)
    domain = pattern.lstrip("*").lstrip(".")
    return host == domain or host.endswith("." + domain)


def _run(argv: list[str]) -> str:
    if shutil.which(argv[0]) is None:
        return ""
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def probe_power() -> Power | None:
    if sys.platform != "darwin":
        return None
    return parse_pmset(_run(["pmset", "-g", "batt"]))


def probe_network() -> Network | None:
    if sys.platform != "darwin":
        return None
    device = parse_wifi_device(_run(["networksetup", "-listallhardwareports"]))
    active = parse_default_interface(_run(["route", "-n", "get", "default"]))
    if device is None or active != device:
        return Network(on_wifi=False, ssid=None)
    return Network(on_wifi=True, ssid=parse_ssid(_run(["ipconfig", "getsummary", device])))


def _scutil_proxy() -> str:
    return _run(["scutil", "--proxy"]) if sys.platform == "darwin" else ""


def resolve_proxy(setting: str, scutil_output: str | None = None) -> str | None:
    """The proxy URL for ``proxy`` = ``system`` (``HTTPS_PROXY``, else macOS settings),
    ``none``, or an explicit URL."""
    setting = (setting or "system").strip()
    if setting == "none":
        return None
    if setting != "system":
        return setting
    env = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if env:
        return env
    if scutil_output is None:
        scutil_output = _scutil_proxy()
    return parse_scutil_proxy(scutil_output)


def proxy_exceptions(setting: str, scutil_output: str | None = None) -> tuple[list[str], bool]:
    """Hosts that skip the proxy with ``proxy`` = ``system``: ``NO_PROXY`` and the macOS
    exception list, and whether simple hostnames (no dot) do. An explicit URL has none."""
    if (setting or "system").strip() != "system":
        return [], False
    env = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    if scutil_output is None:
        scutil_output = _scutil_proxy()
    mac, simple = parse_scutil_exceptions(scutil_output)
    return [p.strip() for p in env.split(",") if p.strip()] + mac, simple


class ProxyRouting:
    """httpx transport: a request goes through ``proxy`` unless its host is on the tailnet
    (no school or office proxy can reach it, while Tailscale itself may connect fine on that
    network) or matches ``exceptions``. Decided per host and remembered for ``ttl`` seconds,
    so a worker started before Tailscale connected goes direct once it has."""

    def __init__(self, proxy: str, verify, exceptions=(), exclude_simple: bool = False,
                 ttl: float = 60.0) -> None:  # fmt: skip
        import httpx

        self.direct = httpx.HTTPTransport(verify=verify)
        self.proxied = httpx.HTTPTransport(verify=verify, proxy=proxy)
        self.exceptions, self.exclude_simple = list(exceptions), exclude_simple
        self.ttl = ttl
        self._seen: dict[str, tuple[float, bool]] = {}

    def bypass(self, host: str) -> bool:
        if any(matches_exception(host, p) for p in self.exceptions):
            return True
        if self.exclude_simple and "." not in host and _ip(host) is None:
            return True
        now = time.monotonic()
        at, direct = self._seen.get(host, (-1e9, False))
        if now - at >= self.ttl:
            direct = on_tailnet(host)
            self._seen[host] = (now, direct)
        return direct

    def handle_request(self, request):
        transport = self.direct if self.bypass(request.url.host) else self.proxied
        return transport.handle_request(request)

    def close(self) -> None:
        self.direct.close()
        self.proxied.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
CA_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def _public_roots() -> str | None:
    """The public CA bundle httpx and requests use by default (certifi), if installed."""
    try:
        import certifi
    except ImportError:
        return None
    return certifi.where()


def tls_verify(ca_bundle: str | None):
    """``verify=`` for httpx: the usual public roots *plus* ``ca_bundle`` (a school's or
    company's TLS-inspection CA). Adding it must not replace the public roots: the same
    worker still reaches the server and the model hosts outside the inspecting network."""
    import ssl

    if not ca_bundle:
        return True
    ctx = ssl.create_default_context()
    roots = _public_roots()
    if roots:
        ctx.load_verify_locations(cafile=roots)
    ctx.load_verify_locations(cafile=str(Path(ca_bundle).expanduser()))
    return ctx


def http_options(proxy: str, ca_bundle: str | None) -> dict:
    """Keyword arguments for every ``httpx.Client`` the worker makes to Lectern. ``proxy``
    ``none`` means no proxy at all: environment proxies are ignored too (``trust_env=False``).
    With a proxy, tailnet hosts and (for ``system``) the exception list go direct
    (:class:`ProxyRouting`)."""
    verify = tls_verify(ca_bundle)
    if (proxy or "system").strip() == "none":
        return {"proxy": None, "verify": verify, "trust_env": False}
    scutil = _scutil_proxy() if (proxy or "system").strip() == "system" else ""
    url = resolve_proxy(proxy, scutil)
    if url is None:
        return {"proxy": None, "verify": verify}
    exceptions, simple = proxy_exceptions(proxy, scutil)
    return {"transport": ProxyRouting(url, verify, exceptions, simple), "trust_env": False}


def network_env(proxy: str, ca_bundle: str | None, home: Path) -> dict[str, str | None]:
    """Environment for libraries that make their own connections (model downloads from
    Hugging Face by mlx-whisper, faster-whisper, and Kokoro): the same proxy, and a CA bundle
    that is the public roots plus ``ca_bundle``, written to ``home/ca-bundle.pem``. ``None``
    values mean: remove the variable."""
    env: dict[str, str | None] = {}
    if (proxy or "system").strip() == "none":
        env.update(dict.fromkeys(PROXY_VARS))
        env["NO_PROXY"] = env["no_proxy"] = "*"
    elif url := resolve_proxy(proxy):
        for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            env[var] = url
    if ca_bundle:
        extra = Path(ca_bundle).expanduser().read_text()
        roots = _public_roots()
        combined = home / "ca-bundle.pem"
        home.mkdir(parents=True, exist_ok=True)
        tmp = combined.with_name(f".ca-bundle.{os.getpid()}.tmp")
        tmp.write_text((Path(roots).read_text() if roots else "") + "\n" + extra + "\n")
        os.replace(tmp, combined)
        for var in CA_VARS:
            env[var] = str(combined)
    return env


def apply_network_env(proxy: str, ca_bundle: str | None, home: Path) -> dict[str, str | None]:
    """Put :func:`network_env` into this process's environment (before engines load)."""
    env = network_env(proxy, ca_bundle, home)
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return env


def pause_reason(cfg: AgentConfig, power: Power | None, net: Network | None) -> str | None:
    """Why the worker must not run right now, or ``None``."""
    if not cfg.enabled:
        return "switched off"
    if power is not None and not power.on_ac:
        if cfg.pause_on_battery:
            return "on battery"
        if power.percent is not None and power.percent < cfg.battery_min_percent:
            return f"battery below {cfg.battery_min_percent}%"
    if cfg.only_on_wifi:
        if net is None or not net.on_wifi:
            return "not on Wi-Fi"
        if cfg.wifi_networks:
            if net.ssid is None:
                return "Wi-Fi network name unavailable (allow Location for Lectern Worker)"
            if net.ssid not in cfg.wifi_networks:
                return f"Wi-Fi network {net.ssid!r} is not allowed"
    return None


class Conditions:
    """Cached probes: the agent asks often (every progress report), macOS is asked at most
    every ``ttl`` seconds."""

    def __init__(self, ttl: float = 10.0, power=probe_power, network=probe_network) -> None:
        self.ttl = ttl
        self._power, self._network = power, network
        self._at = -1e9
        self.power: Power | None = None
        self.network: Network | None = None

    def refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._at >= self.ttl:
            self.power, self.network = self._power(), self._network()
            self._at = now

    def reason(self, cfg: AgentConfig) -> str | None:
        if not cfg.enabled:
            return "switched off"
        self.refresh()
        return pause_reason(cfg, self.power, self.network)
