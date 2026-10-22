"""``lectern worker-menubar``: the small macOS menu-bar app for the worker agent.

It never talks to the server. It reads the agent's ``state.json`` and writes the toggles
into ``config.json``; the agent picks those up within seconds. The menu:

    Lectern Worker — Aligning work 12: 40 %       (or Ready / Paused: on battery / Off)
    ─────────
    ✓ Worker on                                  on/off; off hands a running job back
    ✓ Start at login
    ✓ Only when plugged in                       pause on battery
      Pause on battery below 30 %                (when "Only when plugged in" is off)
      Only on Wi-Fi
    ─────────
      Open Lectern…  ·  Show logs  ·  Quit

:func:`menu_model` builds that from plain data, so it is tested without macOS; :func:`main`
draws it with `rumps <https://github.com/jaredks/rumps>`_ (``lectern[mac]``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from lectern.worker import config as wcfg

STALE_S = 30.0
KIND_VERBS = {"align": "Aligning", "tts": "Making audiobook", "transcode": "Transcoding",
              "ocr": "Recognising text"}  # fmt: skip


@dataclass(frozen=True)
class Item:
    key: str
    title: str
    checked: bool | None = None  # None: not a toggle
    enabled: bool = True


def status_line(state: dict | None, now: float | None = None) -> str:
    now = time.time() if now is None else now
    if not state or now - float(state.get("updated_at") or 0) > STALE_S:
        return "Not running"
    if state.get("error"):
        return f"Error: {state['error']}"
    job = state.get("job")
    if job:
        verb = KIND_VERBS.get(job.get("kind"), "Working on")
        pct = round(float(job.get("progress") or 0.0) * 100)
        return f"{verb} work {job.get('work_id')}: {pct} %"
    if state.get("state") == "paused":
        return f"Paused: {state.get('reason') or 'paused'}"
    if state.get("state") == "off":
        return "Off"
    return "Ready, waiting for jobs"


def title_icon(state: dict | None, cfg: wcfg.AgentConfig, now: float | None = None) -> str:
    """The text shown in the menu bar itself: a book, plus the progress while busy."""
    line = status_line(state, now)
    if not cfg.enabled:
        return "📕"
    if state and state.get("job") and not line.startswith("Not running"):
        return f"📖 {round(float(state['job'].get('progress') or 0) * 100)}%"
    return "📗" if line.startswith("Ready") else "📙"


def menu_model(state: dict | None, cfg: wcfg.AgentConfig, login: bool,
               now: float | None = None) -> list[Item | None]:  # fmt: skip
    """The menu as items (``None`` = separator)."""
    job = (state or {}).get("job")
    items: list[Item | None] = [Item("status", status_line(state, now), enabled=False)]
    if job and job.get("message"):
        items.append(Item("detail", str(job["message"])[:80], enabled=False))
    caps = (state or {}).get("capabilities") or []
    if caps:
        items.append(Item("caps", "Runs: " + ", ".join(caps), enabled=False))
    items += [
        None,
        Item("enabled", "Worker on", checked=cfg.enabled),
        Item("login", "Start at login", checked=login),
        Item("pause_on_battery", "Only when plugged in", checked=cfg.pause_on_battery),
        Item(
            "battery_min",
            f"Pause on battery below {cfg.battery_min_percent} %",
            checked=cfg.battery_min_percent > 0,
            enabled=not cfg.pause_on_battery,
        ),  # fmt: skip
        Item(
            "only_on_wifi",
            "Only on Wi-Fi" + (f" ({', '.join(cfg.wifi_networks)})" if cfg.wifi_networks else ""),
            checked=cfg.only_on_wifi,
        ),  # fmt: skip
        None,
        Item("open", "Open Lectern…", enabled=bool(cfg.server)),
        Item("logs", "Show logs"),
        Item("quit", "Quit menu bar"),
    ]
    return items


def toggle(key: str, cfg: wcfg.AgentConfig) -> dict:
    """The config change a click on a toggle makes."""
    if key == "enabled":
        return {"enabled": not cfg.enabled}
    if key == "pause_on_battery":
        return {"pause_on_battery": not cfg.pause_on_battery}
    if key == "battery_min":
        return {"battery_min_percent": 0 if cfg.battery_min_percent > 0 else 30}
    if key == "only_on_wifi":
        return {"only_on_wifi": not cfg.only_on_wifi}
    raise KeyError(key)


def main() -> None:  # pragma: no cover - needs macOS and rumps
    import subprocess
    import webbrowser

    try:
        import rumps
    except ImportError as exc:
        raise SystemExit("the menu bar needs macOS and lectern[mac] (rumps)") from exc

    from lectern.worker import agent

    class App(rumps.App):
        def __init__(self) -> None:
            super().__init__("Lectern Worker", title="📗", quit_button=None)
            self.timer = rumps.Timer(self.refresh, 2)
            self.timer.start()
            self.refresh(None)

        def refresh(self, _sender) -> None:
            cfg = wcfg.load_config()
            state = wcfg.read_state()
            self.title = title_icon(state, cfg)
            self.menu.clear()
            for item in menu_model(state, cfg, agent.start_at_login()):
                if item is None:
                    self.menu.add(rumps.separator)
                    continue
                mi = rumps.MenuItem(item.title, callback=self.click if item.enabled else None)
                mi.key_name = item.key
                if item.checked is not None:
                    mi.state = 1 if item.checked else 0
                self.menu.add(mi)

        def click(self, sender) -> None:
            key = sender.key_name
            cfg = wcfg.load_config()
            if key == "login":
                agent.set_start_at_login(not agent.start_at_login())
            elif key == "open":
                webbrowser.open(cfg.server + "/app/admin")
            elif key == "logs":
                subprocess.run(["open", str(wcfg.log_dir())], check=False)  # noqa: S603, S607
            elif key == "quit":
                rumps.quit_application()
                return
            else:
                wcfg.update_config(**toggle(key, cfg))
            self.refresh(None)

    App().run()
