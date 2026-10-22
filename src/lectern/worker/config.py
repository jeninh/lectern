"""The worker agent's settings and state files.

Both live in the worker's home: ``~/Library/Application Support/Lectern Worker`` on macOS,
``$XDG_CONFIG_HOME/lectern-worker`` elsewhere, or ``$LECTERN_WORKER_HOME``.

- ``config.json``: written by ``lectern worker-enroll`` and the menu bar, read by the agent
  (toggles take effect within seconds; server, key, and engine changes on restart).
- ``key``: the ``lw_…`` worker credential, mode 0600 in a 0700 directory.
- ``state.json``: written by the agent every few seconds (what it is doing), read by the menu
  bar. Atomic replace, so a reader never sees half a file.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

DEFAULT_SERVER = ""


def worker_home() -> Path:
    env = os.environ.get("LECTERN_WORKER_HOME")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Lectern Worker"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "lectern-worker"


def log_dir() -> Path:
    if sys.platform == "darwin" and not os.environ.get("LECTERN_WORKER_HOME"):
        return Path.home() / "Library" / "Logs" / "Lectern Worker"
    return worker_home() / "logs"


@dataclass
class AgentConfig:
    server: str = DEFAULT_SERVER
    name: str = ""
    # On/off from the menu bar. Off: no new jobs; a running job is handed back.
    enabled: bool = True
    # Pause whenever the Mac runs on battery ("Only when plugged in").
    pause_on_battery: bool = True
    # With pause_on_battery off: still pause on battery below this charge (0 = never).
    battery_min_percent: int = 30
    # Run only while the Mac's active connection is Wi-Fi, optionally only these networks.
    only_on_wifi: bool = False
    wifi_networks: list[str] = field(default_factory=list)
    # "system" (HTTPS_PROXY, else the macOS proxy settings), "none", or a proxy URL.
    proxy: str = "system"
    # Extra CA certificates (PEM) to trust, for networks that inspect TLS (school mode).
    ca_bundle: str | None = None
    # Alignment engine: "auto" (MLX on Apple Silicon, else CPU), "mlx", or "cpu".
    engine: str = "auto"
    model: str = "small"
    # Limit what this machine offers (None = everything it can run).
    capabilities: list[str] | None = None
    lease_s: int = 600
    poll_s: float = 15.0
    allow_http: bool = False
    # Run `lectern worker-doctor --prepare` checks at start and offer only what passes.
    self_test: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> AgentConfig:
        known = {f.name for f in fields(cls)}
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        cfg.battery_min_percent = max(0, min(100, int(cfg.battery_min_percent)))
        cfg.wifi_networks = [str(n) for n in cfg.wifi_networks or [] if str(n).strip()]
        return cfg

    def to_dict(self) -> dict:
        return asdict(self)


def config_path(home: Path | None = None) -> Path:
    return (home or worker_home()) / "config.json"


def key_path(home: Path | None = None) -> Path:
    return (home or worker_home()) / "key"


def state_path(home: Path | None = None) -> Path:
    return (home or worker_home()) / "state.json"


def load_config(home: Path | None = None) -> AgentConfig:
    try:
        return AgentConfig.from_dict(json.loads(config_path(home).read_text()))
    except FileNotFoundError:
        return AgentConfig()


def _private_dir(d: Path) -> None:
    """The worker's home: a real directory owned by this user, mode 0700 (tightened if an
    earlier run or another tool left it more open)."""
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise PermissionError(f"{d} must be a directory owned by you, not a link")
    if st.st_mode & 0o077:
        os.chmod(d, 0o700)


def _write_private(path: Path, text: str, mode: int = 0o600) -> None:
    _private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp")
    # O_EXCL | O_NOFOLLOW: never write through a file or link someone placed there.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def save_config(cfg: AgentConfig, home: Path | None = None) -> None:
    _write_private(config_path(home), json.dumps(cfg.to_dict(), indent=2) + "\n")


def update_config(home: Path | None = None, **changes) -> AgentConfig:
    cfg = load_config(home)
    for k, v in changes.items():
        if not hasattr(cfg, k):
            raise KeyError(k)
        setattr(cfg, k, v)
    cfg = AgentConfig.from_dict(cfg.to_dict())
    save_config(cfg, home)
    return cfg


def save_key(secret: str, home: Path | None = None) -> Path:
    path = key_path(home)
    _write_private(path, secret.strip() + "\n")
    return path


def read_key(home: Path | None = None) -> str:
    return key_path(home).read_text().strip()


def write_state(state: dict, home: Path | None = None) -> None:
    _write_private(state_path(home), json.dumps(state) + "\n", mode=0o644)


def read_state(home: Path | None = None) -> dict | None:
    try:
        return json.loads(state_path(home).read_text())
    except (FileNotFoundError, ValueError):
        return None
