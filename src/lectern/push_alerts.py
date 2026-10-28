"""Optional push alerts (issue #75, docs/health.md): outbound only, off by default.

When the owner sets one push target in ``.env`` (never from the web), a :class:`PushSink` is
registered with :mod:`lectern.notify` and sends alerts that carry a ``push`` payload (the
Health page's transition alerts, :mod:`lectern.health`) to it:

- **ntfy** (``LECTERN_ALERT_NTFY_URL``, the topic URL, and optionally
  ``LECTERN_ALERT_NTFY_TOKEN``, an access token): a plain-text POST with ntfy's ``Title``,
  ``Priority``, and ``Tags`` headers;
- **generic webhook** (``LECTERN_ALERT_WEBHOOK_URL`` and optionally
  ``LECTERN_ALERT_WEBHOOK_TOKEN``, sent as a bearer token): a JSON POST of ``title``,
  ``message``, ``priority`` (Gotify's 0-10 scale), and ``level``.

Lectern only makes that one outbound HTTP(S) request per alert: it opens no port, adds no
callback route, follows no redirect, and uses no proxy. While offline mode (#31) is on nothing
is attempted at all; alerts stay in the app. Every other notification (sign-ins, downloads,
anything without a ``push`` payload) is never pushed, and a ``push`` payload holds a status
and a short reason only: no book titles, usernames, worker names, or paths.

The URLs and tokens are secrets: they are never logged, never put into errors, notifications,
or API responses (the Health page shows the scheme and host only), and the settings summary in
backups masks them (``SecretStr``). The full ``.env``, these keys included, is in every backup's
encrypted ``config/lectern.env`` (docs/backups.md).
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from lectern import egress, notify
from lectern.config import Settings

log = logging.getLogger("lectern.push_alerts")

NTFY, WEBHOOK = "ntfy", "webhook"
TIMEOUT_S = 10
QUEUE_MAX = 32
_TOKEN = re.compile(r"^[\x21-\x7e]{1,512}$")  # printable ASCII: no header injection
# ntfy priority names and Gotify's 0-10 scale, by level ("ok" = a recovery, "test").
_NTFY_PRIORITY = {"critical": "high", "warning": "default", "ok": "low", "test": "low"}
_NTFY_TAGS = {"critical": "rotating_light", "warning": "warning", "ok": "white_check_mark"}
_GOTIFY_PRIORITY = {"critical": 8, "warning": 5, "ok": 2, "test": 2}

_last: dict = {}  # the last delivery: {"at", "ok", "error"} (an error code, never a message)
_last_lock = threading.Lock()


@dataclass(frozen=True)
class Target:
    kind: str
    url: str = field(repr=False)
    token: str = field(repr=False)


def _url_ok(url: str) -> bool:
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname) and " " not in url


def load(settings: Settings) -> tuple[Target | None, list[str]]:
    """The push target, or None with the names of the keys to fix. Nothing set is
    ``(None, [])`` (off). Never raises, never returns a value in the problems."""
    ntfy = settings.alert_ntfy_url.get_secret_value().strip()
    hook = settings.alert_webhook_url.get_secret_value().strip()
    if not ntfy and not hook:
        return None, []
    if ntfy and hook:
        return None, ["LECTERN_ALERT_NTFY_URL", "LECTERN_ALERT_WEBHOOK_URL"]  # one, not both
    kind, url = (NTFY, ntfy) if ntfy else (WEBHOOK, hook)
    prefix = f"LECTERN_ALERT_{kind.upper()}"
    secret = settings.alert_ntfy_token if kind == NTFY else settings.alert_webhook_token
    token = secret.get_secret_value().strip()
    problems = []
    if not _url_ok(url):
        problems.append(f"{prefix}_URL")
    if token and not _TOKEN.match(token):
        problems.append(f"{prefix}_TOKEN")
    if problems:
        return None, problems
    return Target(kind, url, token), []


def masked(url: str) -> str:
    """``https://ntfy.example.ts.net/…``: enough to recognise the server, never the topic,
    path, query, or credentials."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}/…"


def last_delivery() -> dict:
    with _last_lock:
        return dict(_last)


def _record(error: str | None) -> None:
    with _last_lock:
        _last.clear()
        _last.update(
            at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), ok=error is None, error=error
        )


def describe(settings: Settings) -> dict:
    """What the Health page shows: never the URL or the token themselves."""
    target, problems = load(settings)
    if target is None:
        state = "not_configured" if problems else "off"
    else:
        state = "offline" if egress.offline() else "on"
    return {
        "state": state,
        "kind": target.kind if target else None,
        "server": masked(target.url) if target else None,
        "token_set": bool(target and target.token),
        "problems": problems,
        "last": last_delivery() if target else {},
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None  # a redirect is an error: the token never goes to another address


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _request(target: Target, title: str, message: str, level: str) -> urllib.request.Request:
    headers = {"User-Agent": "Lectern"}
    if target.token:
        headers["Authorization"] = f"Bearer {target.token}"
    if target.kind == NTFY:
        headers["Title"] = title.encode("ascii", "replace").decode()
        headers["Priority"] = _NTFY_PRIORITY.get(level, "default")
        if level in _NTFY_TAGS:
            headers["Tags"] = _NTFY_TAGS[level]
        headers["Content-Type"] = "text/plain; charset=utf-8"
        body = message.encode()
    else:
        headers["Content-Type"] = "application/json"
        body = json.dumps(
            {
                "title": title,
                "message": message,
                "priority": _GOTIFY_PRIORITY.get(level, 5),
                "level": level,
            }
        ).encode()
    # load() admitted http(s) URLs only
    return urllib.request.Request(target.url, data=body, headers=headers, method="POST")  # noqa: S310


def send(target: Target, title: str, message: str, level: str) -> str | None:
    """Deliver one alert now. Returns None, or an error code (``offline``, ``http_401``,
    ``timeout``, ``unreachable``, …) that names no URL or token. While offline mode is on
    nothing is attempted."""
    if egress.offline():
        error: str | None = "offline"
    else:
        try:
            with _OPENER.open(_request(target, title, message, level), timeout=TIMEOUT_S) as r:
                r.read(1024)
            error = None
        except urllib.error.HTTPError as exc:
            error = f"http_{exc.code}"
        except egress.OfflineError:
            error = "offline"
        except TimeoutError:
            error = "timeout"
        except urllib.error.URLError as exc:
            reason = exc.reason
            error = (
                "offline"
                if isinstance(reason, egress.OfflineError)
                else "timeout"
                if isinstance(reason, TimeoutError)
                else "unreachable"
            )
        except Exception as exc:  # noqa: BLE001 - reported by type only: messages can hold the URL
            error = type(exc).__name__
    _record(error)
    if error and error != "offline":
        log.warning("push alert not delivered (%s, %s)", target.kind, error)
    return error


class PushSink:
    """A :class:`lectern.notify.Sink`: alerts with a ``push`` payload go to a queue that one
    thread delivers from, so a slow push server never holds up the caller. The same event sent
    to several owners is pushed once."""

    def __init__(self, target: Target) -> None:
        self.target = target
        self._queue: queue.Queue[dict | None] = queue.Queue(maxsize=QUEUE_MAX)
        self._seen: deque[str] = deque(maxlen=256)
        self._thread = threading.Thread(target=self._run, name="lectern-push-alerts", daemon=True)
        self._thread.start()

    def __repr__(self) -> str:
        return f"PushSink({self.target.kind})"

    def deliver(self, db, note, user) -> None:  # noqa: ANN001 - notify.Sink
        push = (note.data or {}).get("push")
        if not isinstance(push, dict):
            return
        event = str((note.data or {}).get("event") or note.id)
        if event in self._seen:
            return
        self._seen.append(event)
        if egress.offline():
            _record("offline")  # no connection is attempted; the alert stays in the app
            return
        try:
            self._queue.put_nowait(push)
        except queue.Full:
            log.warning("push alert queue full; an alert stays in the app only")

    def _run(self) -> None:
        while (push := self._queue.get()) is not None:
            try:
                send(
                    self.target,
                    str(push.get("title", "Lectern"))[:120],
                    str(push.get("message", ""))[:500],
                    str(push.get("level", "warning")),
                )
            finally:
                self._queue.task_done()
        self._queue.task_done()

    def join(self, timeout: float = 5.0) -> None:
        """Wait until every queued alert was attempted (tests)."""
        done = threading.Event()
        threading.Thread(target=lambda: (self._queue.join(), done.set()), daemon=True).start()
        done.wait(timeout)

    def stop(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=TIMEOUT_S + 1)


def install(settings: Settings) -> PushSink | None:
    """Register the push sink when a target is configured (at startup)."""
    target, problems = load(settings)
    if problems:
        log.warning("push alerts are off: check %s in .env", ", ".join(problems))
    if target is None:
        return None
    sink = PushSink(target)
    notify.register_sink(sink)
    log.info(
        "push alerts on (%s)%s",
        target.kind,
        " but paused by offline mode" if egress.offline() else "",
    )
    return sink


def uninstall(sink: PushSink | None) -> None:
    if sink is not None:
        notify.unregister_sink(sink)
        sink.stop()


def send_test(settings: Settings) -> dict:
    """The Health page's test button: one sample alert, sent now. ``ValueError`` when no
    target is configured."""
    target, _problems = load(settings)
    if target is None:
        raise ValueError("not_configured")
    error = send(target, "Lectern: test alert", "Push alerts from Lectern work.", "test")
    return {"ok": error is None, "error": error}
