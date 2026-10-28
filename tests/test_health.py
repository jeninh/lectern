"""Health page, transition alerts, and outbound-only push alerts (issue #75)."""

from __future__ import annotations

import json
import logging
import shutil
import threading
from collections import namedtuple
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from tests.conftest import api_post

from lectern import backups, egress, health, notify, push_alerts
from lectern.config import Settings
from lectern.db import session_factory
from lectern.models import Job, JobStatus, Notification, Work, WorkerCredential, utcnow

TOKEN = "tk_SuperSecretPushToken123"  # noqa: S105 - a test value
TOPIC = "secret-topic-9f3a"
Usage = namedtuple("Usage", "total used free")


class _Fake(BaseHTTPRequestHandler):
    got: list = []
    code = 200

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length", 0))
        type(self).got.append(
            {"path": self.path, "headers": dict(self.headers), "body": self.rfile.read(n)}
        )
        self.send_response(type(self).code)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_push():
    _Fake.got, _Fake.code = [], 200
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, _Fake
    srv.shutdown()


def _notes(db, kind=health.KIND):
    return list(db.query(Notification).filter(Notification.kind == kind).order_by(Notification.id))


def _check(view, cid):
    return next(c for c in view["checks"] if c["id"] == cid)


def _work(db, title="Secret Title"):
    w = Work(title=title, sort_title=title, author="A", sort_author="a", dir_path=f"A/{title}")
    db.add(w)
    db.flush()
    return w


# --- page and access --------------------------------------------------------------------------


def test_owner_only_and_healthz_stays_liveness_only(client, owner, settings):
    r = client.get("/api/admin/health")
    assert r.status_code == 200
    v = r.json()
    assert v["level"] in ("ok", "warning", "critical")
    groups = {c["group"] for c in v["checks"]}
    assert {"backups", "disk", "jobs", "workers", "other"} <= groups
    assert _check(v, "database.migrations")["level"] == "ok"
    assert _check(v, "backups.nightly")["level"] == "info"  # off by default
    assert _check(v, "alerts.push")["detail"]["state"] == "off"  # off by default
    assert client.get("/api/admin/health/summary").json()["level"] == v["level"]
    assert client.get("/healthz").json() == {"status": "ok"}
    client.cookies.clear()
    assert client.get("/api/admin/health").status_code == 401


def test_member_cannot_see_health(client, owner):
    from tests.conftest import login, make_user

    mia = make_user(client, owner["csrf"], "mia", "member")
    client.cookies.clear()
    login(client, "mia", mia["password"])
    assert client.get("/api/admin/health").status_code == 403
    assert client.get("/api/admin/health/summary").status_code == 403


# --- transitions --------------------------------------------------------------------------------


def test_disk_threshold_alerts_once_and_recovers_once(client, owner, settings, monkeypatch):
    real = shutil.disk_usage
    free = {"pct": 50}
    monkeypatch.setattr(
        health.shutil,
        "disk_usage",
        lambda p: (
            Usage(1000, 1000 - free["pct"] * 10, free["pct"] * 10)
            if str(p).startswith(str(settings.data_dir))
            else real(p)
        ),
    )
    db = session_factory()()
    t0 = utcnow()
    assert _check(health.refresh(db, settings, t0), "disk.data")["level"] == "ok"
    free["pct"] = 7
    v = health.refresh(db, settings, t0 + timedelta(minutes=5))
    assert _check(v, "disk.data")["level"] == "warning" and v["level"] != "ok"
    health.refresh(db, settings, t0 + timedelta(minutes=10))  # still low: no second alert
    assert [n.title for n in _notes(db)] == ["Data volume: warning"]
    free["pct"] = 2
    v = health.refresh(db, settings, t0 + timedelta(minutes=15))
    assert _check(v, "disk.data")["level"] == "critical" and v["level"] == "critical"
    free["pct"] = 60
    health.refresh(db, settings, t0 + timedelta(minutes=20))
    health.refresh(db, settings, t0 + timedelta(minutes=25))
    titles = [n.title for n in _notes(db)]
    assert titles == ["Data volume: warning", "Data volume: critical", "Data volume: back to OK"]
    # bouncing again the same day does not alert again (at most once a day per level)
    free["pct"] = 7
    health.refresh(db, settings, t0 + timedelta(minutes=30))
    assert len(_notes(db)) == 3
    db.close()


def test_backup_overdue_and_failed_job_and_kokoro_worker(client, owner, settings, monkeypatch):
    db = session_factory()()
    now = utcnow()
    monkeypatch.setattr(
        backups,
        "target_info",
        lambda s: {"exists": True, "writable": True, "free_bytes": 1, "restic_available": True},
    )
    backups.save_config(db, backups.BackupConfig(enabled=True), 1)
    backups.record_status(
        db, last_success_at=(now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    w = _work(db)
    cred = WorkerCredential(label="test-worker", key_hash="x" * 64, advertised=["tts-kokoro"])
    db.add(cred)
    db.commit()
    queue_presence(cred, now)
    db.commit()
    v = health.refresh(db, settings, now)
    assert _check(v, "backups.nightly")["level"] == "ok"
    assert _check(v, "workers.tts-kokoro")["level"] == "ok"
    assert _check(v, "jobs.failed")["level"] == "ok"
    assert _notes(db) == []

    # Backups stop, a narration fails, and the Mac worker goes away.
    later = now + timedelta(hours=30)
    db.add(Job(kind="tts", work_id=w.id, status=JobStatus.FAILED, error="boom", finished_at=later))
    db.commit()
    v = health.refresh(db, settings, later)
    assert _check(v, "backups.nightly")["level"] == "warning"
    assert _check(v, "jobs.failed")["level"] == "warning"
    assert _check(v, "jobs.failed")["detail"]["items"][0]["work_title"] == "Secret Title"
    assert _check(v, "workers.tts-kokoro")["level"] == "warning"
    kinds = sorted(n.data["check"] for n in _notes(db))
    assert kinds == ["backups.nightly", "jobs.failed", "workers.tts-kokoro"]
    # No titles or worker names in what would be pushed.
    for n in _notes(db):
        assert "Secret Title" not in json.dumps(n.data["push"])
        assert "test-worker" not in json.dumps(n.data["push"])

    # Recovery: a backup, the job succeeds on retry, the worker is back.
    backups.record_status(db, last_success_at=later.strftime("%Y-%m-%dT%H:%M:%SZ"))
    db.add(Job(kind="tts", work_id=w.id, status=JobStatus.SUCCEEDED, finished_at=later))
    queue_presence(cred, later)
    db.commit()
    health.refresh(db, settings, later + timedelta(minutes=1))
    health.refresh(db, settings, later + timedelta(minutes=2))
    back = sorted(n.data["check"] for n in _notes(db) if n.data["level"] == "ok")
    assert back == ["backups.nightly", "jobs.failed", "workers.tts-kokoro"]
    assert len(_notes(db)) == 6
    db.close()


def queue_presence(cred, at):
    from lectern import queue

    queue.record_presence(cred, state="ready", now=at)


def test_registered_check_is_shown_and_alerted(client, owner, settings):
    level = {"v": "ok"}
    health.register_check(
        "restore_drill",
        lambda db, s, now: health.Check(
            "restore.drill", "backups", "Restore drill", level["v"], "Last drill: " + level["v"]
        ),
    )
    try:
        db = session_factory()()
        health.refresh(db, settings)
        level["v"] = "critical"
        v = health.refresh(db, settings)
        assert _check(v, "restore.drill")["level"] == "critical"
        assert [n.title for n in _notes(db)] == ["Restore drill: critical"]
        db.close()
    finally:
        health.unregister_check("restore_drill")


# --- push sink ----------------------------------------------------------------------------------


def _settings(settings: Settings, **kw) -> Settings:
    return settings.model_copy(update=kw)


def test_push_config_is_off_by_default_and_validated(settings):
    from pydantic import SecretStr

    assert push_alerts.load(settings) == (None, [])
    bad = _settings(settings, alert_ntfy_url=SecretStr("ftp://x/" + TOPIC))
    assert push_alerts.load(bad) == (None, ["LECTERN_ALERT_NTFY_URL"])
    both = _settings(
        settings, alert_ntfy_url=SecretStr("https://a/t"), alert_webhook_url=SecretStr("https://b")
    )
    assert push_alerts.load(both)[0] is None
    ok = _settings(
        settings,
        alert_ntfy_url=SecretStr(f"https://ntfy.example.ts.net/{TOPIC}"),
        alert_ntfy_token=SecretStr(TOKEN),
    )
    d = push_alerts.describe(ok)
    assert d["server"] == "https://ntfy.example.ts.net/…" and d["token_set"]
    assert TOPIC not in json.dumps(d) and TOKEN not in json.dumps(d)


@pytest.mark.parametrize("kind", ["ntfy", "webhook"])
def test_sink_delivers_only_health_alerts_once_without_leaking(
    client, owner, settings, fake_push, kind, caplog
):
    from pydantic import SecretStr

    srv, handler = fake_push
    url = f"http://127.0.0.1:{srv.server_port}/{TOPIC}"
    s = _settings(
        settings,
        **{f"alert_{kind}_url": SecretStr(url), f"alert_{kind}_token": SecretStr(TOKEN)},
    )
    caplog.set_level(logging.DEBUG)
    sink = push_alerts.install(s)
    try:
        db = session_factory()()
        from lectern.models import Role, User

        db.add(
            User(username="own2", role=Role.OWNER, webauthn_user_handle=b"own2".ljust(16, b"\0"))
        )
        db.flush()
        assert len(notify.owners(db)) == 2  # two owners: still one push
        # A non-health notification is never pushed.
        notify.notify(db, notify.owners(db)[0], "login.new", "New sign-in", "secret body")
        check = health.Check("disk.data", "disk", "Data volume", "critical", "3% free")
        health._alert(db, notify.owners(db), check, "critical", "disk.data:critical")
        db.commit()
        sink.join()
        assert len(handler.got) == 1
        got = handler.got[0]
        assert got["headers"]["Authorization"] == f"Bearer {TOKEN}"
        if kind == "ntfy":
            assert got["headers"]["Title"] == "Lectern: Data volume: critical"
            assert got["headers"]["Priority"] == "high" and got["body"] == b"3% free"
        else:
            body = json.loads(got["body"])
            assert body["title"] == "Lectern: Data volume: critical" and body["priority"] == 8
        # A failure is reported by code, never with the URL or token.
        handler.code = 401
        assert push_alerts.send(sink.target, "t", "m", "warning") == "http_401"
        r = client.get("/api/admin/health")
        assert r.status_code == 200
        everything = r.text + caplog.text + client.get("/api/notifications").text
        assert TOKEN not in everything and TOPIC not in everything
        db.close()
    finally:
        push_alerts.uninstall(sink)


def test_push_test_button(client, owner, settings, fake_push, monkeypatch):
    from pydantic import SecretStr

    srv, handler = fake_push
    r = api_post(client, "/api/admin/health/push-test", owner["csrf"])
    assert r.status_code == 400  # not configured
    s = _settings(settings, alert_ntfy_url=SecretStr(f"http://127.0.0.1:{srv.server_port}/{TOPIC}"))
    monkeypatch.setattr(client.app.state, "settings", s)
    r = api_post(client, "/api/admin/health/push-test", owner["csrf"])
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert len(handler.got) == 1 and TOPIC not in r.text
    egress.enforce(True)
    assert api_post(client, "/api/admin/health/push-test", owner["csrf"]).status_code == 409
    assert len(handler.got) == 1


def test_offline_mode_attempts_no_connection(client, owner, settings, fake_push, monkeypatch):
    from pydantic import SecretStr

    srv, handler = fake_push
    # A non-loopback target: the egress hook would refuse it. Nothing must even try.
    s = _settings(settings, alert_ntfy_url=SecretStr(f"https://ntfy.example.ts.net/{TOPIC}"))
    attempts = []
    monkeypatch.setattr(
        push_alerts._OPENER, "open", lambda *a, **k: attempts.append(a) or pytest.fail("opened")
    )
    sink = push_alerts.install(s)
    try:
        egress.enforce(True)
        db = session_factory()()
        check = health.Check("jobs.failed", "jobs", "Failed jobs", "warning", "1 failed")
        assert health._alert(db, notify.owners(db), check, "warning", "jobs.failed:warning")
        db.commit()
        sink.join()
        assert attempts == [] and handler.got == []
        assert len(_notes(db)) == 1  # the alert stays in the app
        assert push_alerts.send(sink.target, "t", "m", "warning") == "offline"
        assert push_alerts.describe(s)["state"] == "offline"
        db.close()
    finally:
        egress.enforce(False)
        push_alerts.uninstall(sink)


def test_settings_summary_masks_push_secrets(settings):
    from pydantic import SecretStr

    s = _settings(
        settings, alert_ntfy_url=SecretStr("https://n/" + TOPIC), alert_ntfy_token=SecretStr(TOKEN)
    )
    dumped = json.dumps({k: str(v) for k, v in s.model_dump().items()})
    assert TOKEN not in dumped and TOPIC not in dumped and TOKEN not in repr(s)


# --- review regressions ---------------------------------------------------------------------------


def test_broken_providers_show_could_not_run_and_never_push_twice(
    client, owner, settings, fake_push
):
    from datetime import datetime

    from pydantic import SecretStr

    srv, handler = fake_push
    s = _settings(settings, alert_ntfy_url=SecretStr(f"http://127.0.0.1:{srv.server_port}/{TOPIC}"))
    health.register_check("not_checks", lambda db, st, now: {"a": 1})  # a dict, not Checks
    health.register_check(
        "bad_detail",
        lambda db, st, now: health.Check(
            "drill", "backups", "Drill", "critical", "failed", {"at": datetime.now()}
        ),
    )
    sink = push_alerts.install(s)
    try:
        db = session_factory()()
        t0 = utcnow()
        for i in range(3):
            v = health.refresh(db, s, t0 + timedelta(minutes=i))
        assert client.get("/api/admin/health").status_code == 200
        broken = {c["id"]: c for c in v["checks"] if c["summary"] == "Could not run"}
        assert set(broken) == {"check.not_checks", "check.bad_detail"}
        sink.join()
        # Both broken providers turned amber once: one alert each, one push each, not one a refresh.
        assert sorted(n.data["check"] for n in _notes(db)) == sorted(broken)
        assert len(handler.got) == 2
        db.close()
    finally:
        health.unregister_check("not_checks")
        health.unregister_check("bad_detail")
        push_alerts.uninstall(sink)


def test_a_push_waits_for_the_commit(client, owner, settings, fake_push):
    from pydantic import SecretStr

    srv, handler = fake_push
    s = _settings(settings, alert_ntfy_url=SecretStr(f"http://127.0.0.1:{srv.server_port}/{TOPIC}"))
    sink = push_alerts.install(s)
    try:
        db = session_factory()()
        check = health.Check("disk.data", "disk", "Data volume", "critical", "3% free")
        for _ in range(3):  # a refresh whose commit fails (e.g. the database is locked)
            assert health._alert(db, notify.owners(db), check, "critical", "disk.data:critical")
            db.rollback()
        sink.join()
        assert handler.got == [] and _notes(db) == []
        assert health._alert(db, notify.owners(db), check, "critical", "disk.data:critical")
        db.commit()
        sink.join()
        assert len(handler.got) == 1
        db.close()
    finally:
        push_alerts.uninstall(sink)


def test_a_check_that_flaps_back_to_bad_is_alerted_again(client, owner, settings):
    level = {"v": "ok"}
    health.register_check(
        "flap", lambda db, s, now: health.Check("flap", "other", "Flap", level["v"], level["v"])
    )
    try:
        db = session_factory()()
        t0 = utcnow()
        for i, lv in enumerate(["ok", "warning", "ok", "warning", "warning"]):
            level["v"] = lv
            health.refresh(db, settings, t0 + timedelta(minutes=i))
        assert [n.title for n in _notes(db)] == ["Flap: warning", "Flap: back to OK"]
        # Still amber once the day's dedupe is over: alerted again, then not again.
        for n in _notes(db):
            n.created_at -= timedelta(hours=21)
        db.commit()
        health.refresh(db, settings, t0 + timedelta(minutes=10))
        health.refresh(db, settings, t0 + timedelta(minutes=15))
        assert [n.title for n in _notes(db)][-1] == "Flap: warning" and len(_notes(db)) == 3
        db.close()
    finally:
        health.unregister_check("flap")


def test_disk_thresholds_must_be_ordered(settings):
    with pytest.raises(ValueError, match="CRITICAL_PERCENT"):
        Settings(**{**settings.model_dump(), "health_disk_critical_percent": 20.0})
