"""When the worker may run: power, network, and the proxy to reach the server through.

The probes call macOS tools (``pmset``, ``networksetup``, ``route``, ``ipconfig``, ``scutil``)
and the parsers are plain functions over their output, so the rules are tested anywhere. On
other systems power is unknown (never a reason to pause) and Wi-Fi cannot be detected, so
"Only on Wi-Fi" pauses there.
"""

from __future__ import annotations

import os
import re
import shutil
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


def parse_scutil_proxy(text: str) -> str | None:
    """``scutil --proxy``: the HTTPS proxy as ``http://host:port`` when it is enabled. PAC
    files (``ProxyAutoConfigEnable``) are not evaluated: set the proxy URL explicitly."""
    values = dict(re.findall(r"^\s*(\w+)\s*:\s*(\S+)\s*$", text, re.M))
    if values.get("HTTPSEnable") == "1" and values.get("HTTPSProxy"):
        port = values.get("HTTPSPort") or "443"
        return f"http://{values['HTTPSProxy']}:{port}"
    return None


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
        scutil_output = _run(["scutil", "--proxy"]) if sys.platform == "darwin" else ""
    return parse_scutil_proxy(scutil_output)


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
    """Keyword arguments for every ``httpx.Client`` the worker makes. ``proxy`` ``none``
    means no proxy at all: environment proxies are ignored too (``trust_env=False``)."""
    opts = {"proxy": resolve_proxy(proxy), "verify": tls_verify(ca_bundle)}
    if (proxy or "system").strip() == "none":
        opts["trust_env"] = False
    return opts


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
