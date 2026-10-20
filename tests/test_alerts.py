"""Security alerts (new sign-in, new device, download bursts) and hourly download caps."""

from __future__ import annotations

import base64

import pytest
from tests.conftest import (
    OWNER_PASSWORD,
    api_post,
    drain_inbox,
    ko_headers,
    login,
    make_app_password,
    make_epub,
    make_sync_key,
    make_user,
)

from lectern import notify
from lectern.security.alerts import DEVICE_COOKIE, describe_user_agent


def _notes(client):
    return client.get("/api/notifications").json()


def test_sign_in_alerts_new_and_known_devices(client, owner):
    csrf = owner["csrf"]
    # Setup registered this browser quietly.
    assert client.cookies.get(DEVICE_COOKIE)
    assert _notes(client) == {"items": [], "unread": 0}
    mia = make_user(client, csrf, "mia", "member")

    # Mia signs in from a fresh browser: alert for her and for the owner.
    client.cookies.clear()
    ua = {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1"}
    client.headers.update(ua)
    login(client, "mia", mia["password"])
    mia_cookie = client.cookies.get(DEVICE_COOKIE)
    assert mia_cookie
    n = _notes(client)
    assert n["unread"] == 1 and n["items"][0]["kind"] == "device.new"
    assert n["items"][0]["title"] == "Sign-in from a new device: Safari on iPhone"
    assert n["items"][0]["severity"] == "warning"

    # Same browser again: recognised, so only an informational sign-in note.
    client.cookies.clear()
    client.cookies.set(DEVICE_COOKIE, mia_cookie)
    login(client, "mia", mia["password"])
    assert client.cookies.get(DEVICE_COOKIE) == mia_cookie  # not replaced
    kinds = [i["kind"] for i in _notes(client)["items"]]
    assert kinds == ["login.new", "device.new"]
    mia_csrf = client.get("/api/me").json()["csrf_token"]
    assert api_post(client, "/api/notifications/read", mia_csrf, {"all": True}).status_code == 200
    assert _notes(client)["unread"] == 0
    assert client.get("/api/me").json()["counts"]["unread_notifications"] == 0

    # A cookie from another user's device does not make this browser known for the owner.
    client.cookies.clear()
    client.cookies.set(DEVICE_COOKIE, mia_cookie)
    login(client, "owner", OWNER_PASSWORD)
    titles = [i["title"] for i in _notes(client)["items"]]
    assert titles[0] == "Sign-in from a new device: Safari on iPhone"
    assert "mia signed in from a new device: Safari on iPhone" in titles
    devices = client.get("/api/security").json()["known_devices"]
    assert len(devices) == 2 and devices[0]["label"] == "Safari on iPhone"


def test_first_use_of_device_credentials_alerts(client, owner):
    csrf = owner["csrf"]
    key = make_sync_key(client, csrf, "Kobo Libra")
    pw = make_app_password(client, csrf, "Tablet")
    assert client.get("/users/auth", headers=ko_headers("owner", key)).status_code == 200
    assert client.get("/users/auth", headers=ko_headers("owner", key)).status_code == 200
    basic = base64.b64encode(f"owner:{pw}".encode()).decode()
    assert client.get("/opds", headers={"authorization": f"Basic {basic}"}).status_code == 200
    titles = [i["title"] for i in _notes(client)["items"]]
    assert titles == ["Tablet connected", "Kobo Libra connected"]  # once each


@pytest.fixture
def books(client, owner, settings):
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        make_epub(settings.inbox_path / f"{i}.epub", title=f"Book {i}", padding=i * 10)
    drain_inbox(settings)
    return [w["files"][0]["id"] for w in client.get("/api/library").json()["works"]]


def test_user_cap_blocks_with_clear_errors_audit_and_alerts(client, owner, settings, books):
    csrf = owner["csrf"]
    mia = make_user(client, csrf, "mia", "member")
    r = api_post(client, f"/api/admin/users/{mia['id']}/download-cap", csrf, {"cap": 2})
    assert r.status_code == 200 and r.json()["user"]["download_cap_per_hour"] == 2
    login(client, "mia", mia["password"])
    assert client.get(f"/library/files/{books[0]}/download").status_code == 200
    assert client.get(f"/library/files/{books[1]}/download").status_code == 200
    r = client.get(f"/library/files/{books[2]}/download")
    assert r.status_code == 429
    body = r.json()
    assert body["detail"] == "download_cap" and body["scope"] == "user" and body["limit"] == 2
    assert 1 <= int(r.headers["retry-after"]) <= 3601 and body["retry_after"] == int(
        r.headers["retry-after"]
    )
    # Blocked attempts do not count, and the alert is not repeated within the hour.
    assert client.get(f"/library/files/{books[2]}/download").status_code == 429
    kinds = [i["kind"] for i in _notes(client)["items"]]
    assert kinds.count("download.cap") == 1
    usage = client.get("/api/security").json()["downloads"]
    assert usage["used"] == 2 and usage["cap"] == 2

    ocsrf = login(client, "owner", OWNER_PASSWORD)
    audit = client.get("/api/admin/audit").json()["rows"]
    blocked = [r for r in audit if r["action"] == "download.blocked"]
    assert len(blocked) == 2 and "user cap 2/h" in blocked[0]["detail"]
    owner_titles = [i["title"] for i in _notes(client)["items"]]
    assert owner_titles.count("mia hit the download limit (account)") == 1
    # The owner lifts the cap (0 = none); null returns to the server default.
    api_post(client, f"/api/admin/users/{mia['id']}/download-cap", ocsrf, {"cap": 0})
    login(client, "mia", mia["password"])
    assert client.get(f"/library/files/{books[2]}/download").status_code == 200


def test_device_cap_applies_per_app_password(client, owner, settings, books):
    csrf = owner["csrf"]
    pw_a = make_app_password(client, csrf, "Kobo A")
    pw_b = make_app_password(client, csrf, "Kobo B")
    pws = {p["label"]: p for p in client.get("/api/devices").json()["app_passwords"]}
    r = api_post(
        client, f"/api/devices/app-passwords/{pws['Kobo A']['id']}/download-cap", csrf, {"cap": 1}
    )
    assert r.status_code == 200 and r.json()["app_password"]["download_cap_per_hour"] == 1

    def get(pw, fid):
        basic = base64.b64encode(f"owner:{pw}".encode()).decode()
        return client.get(
            f"/opds/files/{fid}/download", headers={"authorization": f"Basic {basic}"}
        )

    assert get(pw_a, books[0]).status_code == 200
    r = get(pw_a, books[1])
    assert r.status_code == 429 and r.json()["scope"] == "device"
    assert get(pw_b, books[1]).status_code == 200  # another device is unaffected
    # /library/* accepts the same app password (Basic) and counts it as the same device.
    client.cookies.clear()
    basic = base64.b64encode(f"owner:{pw_a}".encode()).decode()
    r = client.get(
        f"/library/files/{books[2]}/download", headers={"authorization": f"Basic {basic}"}
    )
    assert r.status_code == 429 and r.json()["scope"] == "device"


def test_burst_alerts_user_and_owner_once(client, owner, settings, books):
    settings.download_burst_count = 3
    csrf = owner["csrf"]
    mia = make_user(client, csrf, "mia", "member")
    login(client, "mia", mia["password"])
    for fid in books + books:
        assert client.get(f"/library/files/{fid}/download").status_code == 200
    bursts = [i for i in _notes(client)["items"] if i["kind"] == "download.burst"]
    assert len(bursts) == 1 and bursts[0]["title"].startswith("3 downloads in 10 minutes")
    login(client, "owner", OWNER_PASSWORD)
    assert any(i["title"].startswith("mia: 3 downloads") for i in _notes(client)["items"])
    actions = [r["action"] for r in client.get("/api/admin/audit").json()["rows"]]
    assert actions.count("download.burst") == 1


def test_web_reader_copy_is_counted_once_then_cached(client, owner, settings, books):
    settings.download_device_cap_per_hour = 1
    work = client.get("/api/library").json()["works"][0]
    r = client.get(f"/library/works/{work['id']}/book.epub")
    assert r.status_code == 200 and r.headers["etag"] == f'"{work["files"][0]["sha256"]}"'
    again = client.get(
        f"/library/works/{work['id']}/book.epub", headers={"if-none-match": r.headers["etag"]}
    )
    assert again.status_code == 304  # reopening the book does not use up the cap
    assert client.get(f"/library/files/{books[1]}/download").status_code == 429


def test_extension_sinks_receive_notifications(client, owner):
    seen = []

    class Sink:
        def deliver(self, db, note, user):
            seen.append((user.username, note.kind))

    sink = Sink()
    notify.register_sink(sink)
    try:
        make_sync_key(client, owner["csrf"], "Kobo")
        key = client.get("/api/devices").json()  # no alert yet
        assert seen == [] and key
        client.cookies.clear()
        login(client, "owner", OWNER_PASSWORD)
        assert seen == [("owner", "device.new")]
    finally:
        notify.unregister_sink(sink)


def test_describe_user_agent():
    assert (
        describe_user_agent(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) Gecko/20100101 Firefox/131.0"
        )
        == "Firefox on macOS"
    )
    assert (
        describe_user_agent("Mozilla/5.0 (Linux; Android 14) Chrome/129.0 Mobile Safari/537.36")
        == "Chrome on Android"
    )
    assert (
        describe_user_agent("Mozilla/5.0 (Windows NT 10.0) Chrome/129 Safari/537.36 Edg/129.0")
        == "Edge on Windows"
    )
    assert describe_user_agent(None) == "Browser"


def test_cap_check_and_insert_are_atomic_under_concurrency(client, owner, settings):
    """Many threads racing for the last download slot: exactly the cap gets through."""
    import threading

    from fastapi import HTTPException

    from lectern.db import session_factory
    from lectern.models import DownloadEvent, User
    from lectern.security import downloads

    settings.download_cap_per_hour = 3
    settings.download_burst_count = 0
    results: list[int] = []
    start = threading.Barrier(8)

    def grab(i):
        with session_factory()() as db:
            user = db.query(User).filter_by(username="owner").one()
            start.wait()
            try:
                downloads.admit(
                    db, settings, user, downloads.Device(f"session:{i}", "t"), "x", None
                )
                results.append(200)
            except HTTPException as exc:
                results.append(exc.status_code)

    threads = [threading.Thread(target=grab, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200] * 3 + [429] * 5
    with session_factory()() as db:
        assert db.query(DownloadEvent).filter_by(blocked=False).count() == 3


def test_range_parser_rejects_absurd_numbers():
    """Ranges are parsed for metered streams; a 30-digit number must not reach int()."""
    from lectern.http_range import parse_range

    assert parse_range("bytes=" + "9" * 30 + "-", 100) is None
    assert parse_range("bytes=0-" + "1" * 20, 100) is None
    assert parse_range("bytes=0-" + "9" * 19, 100) == (0, 99)
