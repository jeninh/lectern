"""``lectern worker-agent``: the background worker the installer sets up and launchd keeps alive.

It runs :class:`lectern.align.worker.RemoteWorker` with every engine installed here, re-reads
``config.json`` every few seconds (the menu bar's toggles), pauses while the conditions say so
(:mod:`lectern.worker.conditions`), and writes ``state.json`` for the menu bar.

``lectern worker-enroll`` exchanges a one-time code for a worker credential and writes the
config; ``lectern worker-service install|uninstall`` manages the launchd agents (the worker and,
with "Start at login", the menu bar).
"""

from __future__ import annotations

import logging
import os
import platform
import plistlib
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from lectern import __version__
from lectern.worker import config as wcfg
from lectern.worker.conditions import Conditions, apply_network_env, http_options

log = logging.getLogger("lectern.worker.agent")

AGENT_LABEL = "dev.lectern.worker"
MENUBAR_LABEL = "dev.lectern.worker.menubar"
STATE_EVERY_S = 3.0


def build_runners(
    cfg: wcfg.AgentConfig, *, self_test: bool = True
) -> tuple[list, dict[str, str], dict[str, str]]:
    """Runners for every engine installed here (narrowed by ``cfg.capabilities``) whose
    self-test passes (:mod:`lectern.worker.doctor`, in a child process, downloading missing
    models first), the engine versions to report, and the capabilities withheld with why."""
    from lectern.align.mlx import worker_transcriber
    from lectern.align.worker import AlignRunner
    from lectern.worker.runners import (
        KokoroRunner,
        OcrRunner,
        TranscodeRunner,
        ffmpeg_available,
        kokoro_available,
        tesseract_available,
    )

    runners: list = []
    engines: dict[str, str] = {}
    withheld: dict[str, str] = {}
    allowed = set(cfg.capabilities) if cfg.capabilities is not None else None

    def want(cap: str) -> bool:
        return allowed is None or cap in allowed

    tr = worker_transcriber(cfg.engine, cfg.model) if want("align") else None
    candidates = [c for c, have in (("align", tr is not None),
                                    ("tts-kokoro", kokoro_available() and ffmpeg_available()),
                                    ("transcode", ffmpeg_available())) if want(c) and have]  # fmt: skip
    if self_test and candidates:
        from lectern.worker import doctor

        log.info("self-test: %s (downloads missing models first)", ", ".join(candidates))
        results = doctor.check_isolated(candidates, engine=cfg.engine, model=cfg.model)
        for cap, r in results.items():
            if r.ok:
                log.info("self-test %s: ok (%s)", cap, r.engine)
            else:
                log.error("self-test %s failed, not offering it: %s", cap, r.reason)
                withheld[cap] = r.reason or "self-test failed"
    if "align" in candidates and "align" not in withheld:
        runners.append(AlignRunner(tr))
        engines["align"] = tr.label
    if "tts-kokoro" in candidates and "tts-kokoro" not in withheld:
        runners.append(KokoroRunner())
        engines["tts-kokoro"] = "kokoro-82m"
    if "transcode" in candidates and "transcode" not in withheld:
        runners.append(TranscodeRunner())
        engines["transcode"] = "ffmpeg"
    if want("ocr") and tesseract_available():
        from lectern.ocr import engine as ocr_engine

        runners.append(OcrRunner())
        engines["ocr"] = ocr_engine.label(ocr_engine.probe()[0]) or "tesseract"
    return runners, engines, withheld


class Agent:
    def __init__(self, home: Path | None = None, *, conditions: Conditions | None = None,
                 runners: list | None = None, client=None) -> None:  # fmt: skip
        from lectern.align.worker import RemoteWorker

        self.home = home or wcfg.worker_home()
        self.cfg = wcfg.load_config(self.home)
        if not self.cfg.server:
            raise SystemExit("not enrolled: run the installer or `lectern worker-enroll`")
        if not self.cfg.server.startswith("https://") and not self.cfg.allow_http:
            raise SystemExit("refusing to send the worker credential over plain http")
        self.conditions = conditions or Conditions()
        if self.cfg.ca_bundle and not Path(self.cfg.ca_bundle).expanduser().is_file():
            # exit 0: launchd does not restart a clean exit, so no restart loop; the menu
            # bar shows why
            msg = f"ca_bundle {self.cfg.ca_bundle} not found: fix it in config.json, then "
            msg += "`lectern worker-service install`"
            wcfg.write_state({"state": "off", "error": msg, "updated_at": time.time(),
                              "server": self.cfg.server, "job": None}, self.home)  # fmt: skip
            log.error(msg)
            raise SystemExit(0)
        # before the engines load: their model downloads go through the same proxy and CAs
        apply_network_env(self.cfg.proxy, self.cfg.ca_bundle, self.home)
        self.stop = threading.Event()
        self._cfg_at = time.monotonic()
        self.worker = RemoteWorker(
            self.cfg.server,
            wcfg.read_key(self.home),
            name=self.cfg.name or platform.node() or "mac",
            lease_s=self.cfg.lease_s,
            poll_s=self.cfg.poll_s,
            runners=list(runners or []),
            pause=self.pause_reason,
            stop=self.stop,
            client=client,
            crash_marker=self.home / "current-job.json",
            info={
                "version": __version__,
                "platform": f"{sys.platform}-{platform.machine()}",
                "engines": {},
            },  # fmt: skip
            **http_options(self.cfg.proxy, self.cfg.ca_bundle),
        )
        self.last_error: str | None = None
        if runners is None:
            # A job the previous process died in is settled before the (possibly long)
            # self-test, while its lease is most likely still valid.
            self.worker.report_crash()
            wcfg.write_state({"state": "paused", "reason": "checking engines (first run downloads "
                              "models)", "updated_at": time.time(), "server": self.cfg.server,
                              "job": None, "pid": os.getpid()}, self.home)  # fmt: skip
            built, engines, withheld = build_runners(self.cfg, self_test=self.cfg.self_test)
            if not built and not withheld:
                raise SystemExit("no engine installed: nothing this worker could run")
            # With every self-test failed the agent stays up and tells the server why
            # (paused, "engines unavailable: …") instead of exiting into a launchd restart loop.
            self.worker.runners = built
            self.worker.withheld = withheld
            self.worker.info["engines"] = engines
        elif not runners:
            raise SystemExit("no engine installed: nothing this worker could run")

    def _reload(self) -> None:
        if time.monotonic() - self._cfg_at >= 2.0:
            try:
                self.cfg = wcfg.load_config(self.home)
            except (OSError, ValueError):
                log.warning("config.json unreadable; keeping the previous settings")
            self._cfg_at = time.monotonic()

    def pause_reason(self) -> str | None:
        self._reload()
        return self.conditions.reason(self.cfg)

    def state(self) -> dict:
        current = self.worker.current
        reason = None if current else self.pause_reason()
        if not current and not reason and not self.worker.capabilities:
            reason = "engines unavailable: " + "; ".join(sorted(set(
                self.worker.unavailable.values())))[:150] if self.worker.unavailable else None  # fmt: skip
        p, n = self.conditions.power, self.conditions.network
        return {
            "pid": os.getpid(),
            "updated_at": time.time(),
            "server": self.cfg.server,
            "name": self.worker.name,
            "state": "busy" if current else ("paused" if reason else "ready"),
            "reason": reason,
            "job": current,
            "capabilities": self.worker.capabilities,
            "engines": self.worker.info.get("engines", {}),
            "unavailable": self.worker.unavailable,
            "power": None if p is None else {"on_ac": p.on_ac, "percent": p.percent},
            "network": None if n is None else {"on_wifi": n.on_wifi, "ssid": n.ssid},
            "error": self.last_error,
        }

    def _publish(self) -> None:
        while not self.stop.wait(STATE_EVERY_S):
            try:
                wcfg.write_state(self.state(), self.home)
            except Exception:  # noqa: BLE001 - the menu bar just shows stale state
                log.debug("state write failed", exc_info=True)

    def run(self) -> None:
        def _on_signal(_signum, _frame) -> None:
            if self.stop.is_set():
                raise KeyboardInterrupt
            log.warning("stopping after handing back the current job")
            self.worker.mark_stopping()
            self.stop.set()

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
        wcfg.write_state(self.state(), self.home)
        threading.Thread(target=self._publish, name="lectern-state", daemon=True).start()
        try:
            self.worker.run_forever()
        except PermissionError as exc:
            self.last_error = str(exc)
            wcfg.write_state(self.state(), self.home)
            raise SystemExit(f"{exc}: the credential was revoked; enroll again") from exc
        finally:
            self.worker.report("off")
            state = self.state()
            state.update(state="off", job=None)
            wcfg.write_state(state, self.home)


# --- enrollment --------------------------------------------------------------------------------


def enroll(server: str, code: str, *, name: str | None = None, home: Path | None = None,
           proxy: str = "system", ca_bundle: str | None = None, allow_http: bool = False,
           client=None) -> wcfg.AgentConfig:  # fmt: skip
    """Exchange a one-time enrollment code for a credential and write config and key."""
    import httpx

    server = server.rstrip("/")
    if not server.startswith("https://") and not allow_http:
        raise SystemExit("the server URL must be https:// (or pass --allow-http on a trusted LAN)")
    name = name or platform.node() or "mac"
    http = client or httpx.Client(timeout=30.0, **http_options(proxy, ca_bundle))
    r = http.post(f"{server}/api/worker/enroll", json={"code": code.strip(), "name": name})
    if r.status_code == 401:
        raise SystemExit("the enrollment code is invalid, used, or expired: make a new one")
    r.raise_for_status()
    body = r.json()
    wcfg.save_key(body["secret"], home)
    cfg = wcfg.load_config(home)
    cfg.server, cfg.name, cfg.proxy, cfg.ca_bundle = server, name, proxy, ca_bundle
    cfg.allow_http = allow_http
    wcfg.save_config(cfg, home)
    return cfg


# --- launchd -----------------------------------------------------------------------------------


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_for(label: str, argv: list[str], *, keep_alive: bool, logs: Path,
              env: dict | None = None) -> bytes:  # fmt: skip
    data = {
        "Label": label,
        "ProgramArguments": argv,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False} if keep_alive else False,
        "ProcessType": "Background" if keep_alive else "Interactive",
        # Low I/O and CPU priority: the Mac stays responsive while a book renders.
        "LowPriorityIO": keep_alive,
        # Time to hand a job back on stop (a model load does not check for it); a worker
        # killed anyway leaves a "stopping" marker, released, not counted as a crash.
        "ExitTimeOut": 60,
        "Nice": 10 if keep_alive else 0,
        "ThrottleInterval": 30,
        "StandardOutPath": str(logs / f"{label}.log"),
        "StandardErrorPath": str(logs / f"{label}.log"),
        "EnvironmentVariables": env or {},
    }
    return plistlib.dumps(data)


def _lectern_argv() -> list[str]:
    exe = shutil.which("lectern")
    return [exe] if exe else [sys.executable, "-m", "lectern.cli"]


def _launchctl(*args: str) -> None:
    if shutil.which("launchctl"):
        subprocess.run(["launchctl", *args], check=False, capture_output=True)  # noqa: S603, S607


def install_service(*, menubar: bool = True, start_at_login: bool = True) -> list[Path]:
    """Write and load the launchd agents. The worker agent always starts at login (it pauses
    itself as configured); the menu bar agent only with ``start_at_login``."""
    if sys.platform != "darwin":
        raise SystemExit("launchd services are macOS only; run `lectern worker-agent` yourself")
    logs = wcfg.log_dir()
    logs.mkdir(parents=True, exist_ok=True)
    target = launch_agents_dir()
    target.mkdir(parents=True, exist_ok=True)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/opt/homebrew/bin")}
    if os.environ.get("LECTERN_WORKER_HOME"):
        env["LECTERN_WORKER_HOME"] = os.environ["LECTERN_WORKER_HOME"]
    written = []
    agents = [(AGENT_LABEL, [*_lectern_argv(), "worker-agent"], True)]
    if menubar:
        agents.append((MENUBAR_LABEL, [*_lectern_argv(), "worker-menubar"], False))
    for label, argv, keep in agents:
        path = target / f"{label}.plist"
        if label == MENUBAR_LABEL and not start_at_login:
            set_start_at_login(False)
            continue
        path.write_bytes(plist_for(label, argv, keep_alive=keep, logs=logs, env=env))
        _launchctl("bootout", f"gui/{os.getuid()}", str(path))
        _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
        written.append(path)
    return written


def restart_service() -> bool:
    """Restart the installed worker agent (it re-runs its self-test); ``False`` when it is
    not installed here."""
    if sys.platform != "darwin" or not (launch_agents_dir() / f"{AGENT_LABEL}.plist").exists():
        return False
    _launchctl("kickstart", "-k", f"gui/{os.getuid()}/{AGENT_LABEL}")
    return True


def uninstall_service() -> None:
    for label in (MENUBAR_LABEL, AGENT_LABEL):
        path = launch_agents_dir() / f"{label}.plist"
        if path.exists():
            _launchctl("bootout", f"gui/{os.getuid()}", str(path))
            path.unlink()


def start_at_login() -> bool:
    return (launch_agents_dir() / f"{MENUBAR_LABEL}.plist").exists()


def set_start_at_login(on: bool) -> None:
    """The menu bar's "Start at login": the menu bar's launch agent exists or not. (The
    worker agent itself stays installed; with the menu bar off it still runs and pauses as
    configured. "Turn off" in the menu is what stops it running jobs.)"""
    path = launch_agents_dir() / f"{MENUBAR_LABEL}.plist"
    if on:
        logs = wcfg.log_dir()
        logs.mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/opt/homebrew/bin")}
        if os.environ.get("LECTERN_WORKER_HOME"):
            env["LECTERN_WORKER_HOME"] = os.environ["LECTERN_WORKER_HOME"]
        # Not loaded now (the menu bar asking is already running): only at the next login.
        path.write_bytes(plist_for(MENUBAR_LABEL, [*_lectern_argv(), "worker-menubar"],
                                   keep_alive=False, logs=logs, env=env))  # fmt: skip
    else:
        path.unlink(missing_ok=True)
