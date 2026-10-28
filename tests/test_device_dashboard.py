"""The Devices dashboard (issue #86): the KOReader plugin's report (``PUT /koplugin/report``)
and who may read it (``GET /api/devices/dashboard[/{id}]``).

Expected values come from how the household was built here, never from the code under test.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.conftest import (
    OWNER_PASSWORD,
    api_post,
    drain_inbox,
    login,
    make_epub,
    make_user,
    put_json,
)

from lectern import device_report
from lectern.db import make_engine, session_factory
from lectern.migrate import alembic_config, upgrade_to_head
from lectern.models import BookFile, Work


def basic(username: str, secret: str) -> dict:
    return {"authorization": "Basic " + base64.b64encode(f"{username}:{secret}".encode()).decode()}


@pytest.fixture
def home(client, owner, app, settings):
    """Owner, member mia, reader rex, each with one device. Alpha and Bravo are on a shelf
    shared with rex; Charlie is not."""
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    for t in ("Alpha", "Bravo", "Charlie"):
        make_epub(settings.inbox_path / f"{t}.epub", title=t, authors=[f"Author {t}"])
    drain_inbox(settings)
    with session_factory()() as db:
        ids = {w.title: w.id for w in db.scalars(select(Work))}
        digest = {
            w.title: db.scalar(select(BookFile.partial_md5).where(BookFile.work_id == w.id))
            for w in db.scalars(select(Work))
        }
    mia = make_user(client, owner["csrf"], "mia", "member")
    rex = make_user(client, owner["csrf"], "rex", "reader")
    shelf = api_post(client, "/api/shelves", owner["csrf"], {"name": "Shared"}).json()
    for t in ("Alpha", "Bravo"):
        api_post(client, f"/api/shelves/{shelf['id']}/items", owner["csrf"], {"work_id": ids[t]})
    r = put_json(
        client,
        f"/api/shelves/{shelf['id']}/grants/{rex['id']}",
        owner["csrf"],
        {"permission": "view"},
    )
    assert r.status_code == 200, r.text
    people = {}
    for name, password, label in (
        ("owner", OWNER_PASSWORD, "Kobo"),
        ("mia", mia["password"], "Kindle"),
        ("rex", rex["password"], "Boox"),
    ):
        http = TestClient(app)
        csrf = login(http, name, password)
        d = api_post(http, "/api/devices", csrf, {"label": label}).json()
        people[name] = {
            "http": http,
            "csrf": csrf,
            "pw": d["device"]["app_password"]["id"],
            "basic": basic(name, d["opds_secret"]),
        }
    return {"ids": ids, "digest": digest, **people}


def report(client: TestClient, headers: dict, **body):
    return client.put("/koplugin/report", json=body, headers=headers)


def test_a_report_shows_on_the_dashboard_and_books_are_resolved_as_the_device_user_sees_them(
    home, client
):
    ids, rex = home["ids"], home["rex"]
    r = report(
        client,
        rex["basic"],
        age=600,
        battery=41,
        charging=True,
        free_bytes=1_234_567_890,
        plugin_version="1.0",
        koreader_version="v2025.10",
        books=[
            {"work_id": ids["Alpha"], "percentage": 0.5},
            {"document": home["digest"]["Charlie"], "percentage": 0.2},  # not shared with rex
            {"document": home["digest"]["Alpha"], "percentage": 0.9},  # Alpha again
            {"document": "0123456789abcdef0123456789abcdef"},  # not in the library
            {"work_id": 999_999},  # not in the library
        ],
    )
    assert r.status_code == 200 and r.json() == {"stored": True}, r.text
    # The owner sends Alpha (already on the device) and Bravo (not yet downloaded).
    owner = home["owner"]
    for t in ("Alpha", "Bravo"):
        q = api_post(
            owner["http"],
            f"/api/device-queues/{rex['pw']}/items",
            owner["csrf"],
            {"work_ids": [ids[t]]},
        )
        assert q.status_code == 200, q.text

    dash = rex["http"].get("/api/devices/dashboard").json()
    assert [d["label"] for d in dash["devices"]] == ["Boox"]
    rep = dash["devices"][0]["report"]
    assert (rep["battery"], rep["charging"], rep["free_bytes"]) == (41, True, 1_234_567_890)
    assert (rep["plugin_version"], rep["koreader_version"]) == ("1.0", "v2025.10")
    assert rep["books"] == 2  # Alpha and the hidden one; unknown books and repeats not counted
    as_of = datetime.fromisoformat(rep["as_of"])
    expected = datetime.now(UTC) - timedelta(seconds=600)
    assert abs((as_of - expected).total_seconds()) < 60
    assert dash["devices"][0]["pending"] == 2

    detail = rex["http"].get(f"/api/devices/dashboard/{rex['pw']}")
    assert detail.status_code == 200
    body = detail.json()
    assert [(b["work"] and b["work"]["title"], b["percentage"]) for b in body["books"]] == [
        ("Alpha", 0.5),
        (None, 0.2),
    ]
    assert body["books"][1]["hidden"] is True
    assert body["not_in_library"] == 2
    assert [q["work"]["title"] for q in body["queued"]] == ["Bravo"]
    assert "Charlie" not in detail.text

    # The owner sees it too, still without the title rex cannot see.
    seen = owner["http"].get(f"/api/devices/dashboard/{rex['pw']}")
    assert seen.status_code == 200 and "Charlie" not in seen.text
    assert [b.get("hidden", False) for b in seen.json()["books"]] == [False, True]


def test_only_the_owner_sees_other_peoples_devices(home, client, app):
    for who in ("owner", "mia", "rex"):
        assert report(client, home[who]["basic"], battery=50).status_code == 200
    labels = {
        who: sorted(
            d["label"] for d in home[who]["http"].get("/api/devices/dashboard").json()["devices"]
        )
        for who in ("owner", "mia", "rex")
    }
    assert labels == {"owner": ["Boox", "Kindle", "Kobo"], "mia": ["Kindle"], "rex": ["Boox"]}
    for who, other in (("rex", "mia"), ("mia", "rex"), ("mia", "owner"), ("rex", "owner")):
        r = home[who]["http"].get(f"/api/devices/dashboard/{home[other]['pw']}")
        assert r.status_code == 404, (who, other)
    assert (
        home["owner"]["http"].get(f"/api/devices/dashboard/{home['mia']['pw']}").status_code == 200
    )
    assert TestClient(app).get("/api/devices/dashboard").status_code == 401


def test_a_late_report_does_not_replace_a_newer_one_and_needs_the_devices_password(home, client):
    rex = home["rex"]
    assert report(client, rex["basic"], battery=80).json() == {"stored": True}
    # Sent late from the outbox: read an hour ago, older than what is stored.
    assert report(client, rex["basic"], battery=20, age=3600).json() == {"stored": False}
    rep = rex["http"].get("/api/devices/dashboard").json()["devices"][0]["report"]
    assert rep["battery"] == 80
    assert client.put("/koplugin/report", json={"battery": 5}).status_code == 401
    wrong = basic("rex", "not-the-password")
    assert client.put("/koplugin/report", json={"battery": 5}, headers=wrong).status_code == 401
    assert report(client, rex["basic"], battery=101).status_code == 422


def test_update_available_compares_versions_as_numbers(monkeypatch):
    monkeypatch.setattr(device_report, "bundled_plugin_version", lambda: "1.10.0")
    assert device_report.update_available("1.9.3") is True
    assert device_report.update_available("1.10") is False  # not a string comparison
    assert device_report.update_available("1.10.0") is False
    assert device_report.update_available("2.0") is False
    assert device_report.update_available(None) is False
    assert device_report.update_available("dev") is False
    monkeypatch.setattr(device_report, "bundled_plugin_version", lambda: None)
    assert device_report.update_available("0.1") is False


def test_0031_round_trips(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm31.db'}")
    upgrade_to_head(engine)
    assert "device_reports" in sa.inspect(engine).get_table_names()
    cfg = alembic_config(str(engine.url))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        parent = ScriptDirectory.from_config(cfg).get_revision("0031_device_reports").down_revision
        command.downgrade(cfg, parent)
    assert "device_reports" not in sa.inspect(engine).get_table_names()
    upgrade_to_head(engine)
    assert "device_reports" in sa.inspect(engine).get_table_names()
