"""JSON API used by the single-page app."""

import base64
from urllib.parse import quote

from tests.conftest import (
    ORIGIN,
    OWNER_PASSWORD,
    api_post,
    csrf_of,
    ko_headers,
    login,
    make_epub,
    make_sync_key_full,
    post_form,
)

from lectern.db import session_factory
from lectern.ingest.watcher import InboxWatcher


def _ingest(settings, *books):
    w = InboxWatcher(settings.inbox_path, settings.library_dir, session_factory(), 0.01)
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    (settings.inbox_path / "_rejected").mkdir(exist_ok=True)
    for i, (title, author) in enumerate(books):
        make_epub(
            settings.inbox_path / f"b{i}.epub",
            title=title,
            authors=[author],
            cover=True,
            padding=i * 37,
        )
    w.scan_once()
    w.scan_once()


def test_me_and_spa_routes(client, owner):
    me = client.get("/api/me").json()
    assert me["user"]["username"] == "owner" and me["user"]["role"] == "owner"
    assert me["csrf_token"] == owner["csrf"]
    assert me["opds_url"].endswith("/opds") and me["passkeys_possible"] is False
    # SPA shell is served (or a clear 503 if the frontend is not built) only when signed in.
    r = client.get("/app/library")
    assert r.status_code in (200, 503)
    assert client.get("/app/assets/nothing.js").status_code == 404
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    r = client.get("/library", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/app/library"


def test_unauthenticated_api_is_json_401(client):
    r = client.get("/api/library", headers={"accept": "text/html"})
    # Even browser-looking requests to the API get a JSON 401; only the SPA shell redirects.
    assert r.status_code == 401 and r.json() == {"detail": "Authentication required"}


def test_library_search_sort_and_detail(client, owner, settings):
    _ingest(settings, ("Zeta", "Ann Author"), ("Alpha", "Bob Writer"), ("Middle", "Ann Author"))
    lib = client.get("/api/library").json()
    assert [w["title"] for w in lib["works"]] == ["Alpha", "Middle", "Zeta"]
    assert lib["total"] == 3
    assert all(w["progress"] is None for w in lib["works"])
    by_author = client.get("/api/library?sort=author").json()["works"]
    assert [w["author"] for w in by_author] == ["Ann Author", "Ann Author", "Bob Writer"]
    found = client.get("/api/library?q=bob").json()["works"]
    assert [w["title"] for w in found] == ["Alpha"]
    assert client.get("/api/library?sort=evil").status_code == 422
    assert client.get("/api/works/999").status_code == 404
    detail = client.get(f"/api/works/{found[0]['id']}").json()
    assert detail["files"][0]["download_url"].endswith("/download")
    assert client.get(detail["cover_url"]).headers["content-type"] == "image/jpeg"


def test_continue_joins_sync_progress_to_works(client, owner, settings):
    _ingest(settings, ("Dune", "Frank Herbert"), ("Emma", "Jane Austen"))
    assert client.get("/api/continue").json() == {"items": []}
    created = make_sync_key_full(client, owner["csrf"], "Kobo")
    h = ko_headers("owner", created["secret"])
    works = {w["title"]: w for w in client.get("/api/library").json()["works"]}
    doc = works["Emma"]["files"][0]["partial_md5"]
    client.put(
        "/syncs/progress",
        json={
            "document": doc,
            "progress": "p",
            "percentage": 0.4,
            "device": "Kobo Clara",
            "device_id": "K1",
        },
        headers=h,
    )
    # A sync for a document we do not have must not break the join.
    client.put(
        "/syncs/progress",
        json={"document": "f" * 32, "progress": "p", "percentage": 0.9},
        headers=h,
    )

    cont = client.get("/api/continue").json()["items"]
    assert [c["title"] for c in cont] == ["Emma"]
    assert cont[0]["progress"]["percentage"] == 0.4
    assert cont[0]["progress"]["device"] == "Kobo Clara"
    assert cont[0]["progress"]["sync_key_label"] == "Kobo"
    assert cont[0]["progress_file_id"] == works["Emma"]["files"][0]["id"]
    assert cont[0]["cover_tint"]

    lib = {w["title"]: w for w in client.get("/api/library").json()["works"]}
    assert lib["Emma"]["progress"]["percentage"] == 0.4 and lib["Dune"]["progress"] is None


def test_add_device_flow_reports_first_sync(client, owner, settings):
    _ingest(settings, ("Dune", "Frank Herbert"))
    created = make_sync_key_full(client, owner["csrf"], "New Kindle")
    key_id = created["sync_key"]["id"]
    assert created["username"] == "owner" and len(created["secret"]) > 20
    status = client.get(f"/api/devices/sync-keys/{key_id}").json()
    assert status["connected"] is False and status["latest_sync"] is None

    h = ko_headers("owner", created["secret"])
    client.get("/users/auth", headers=h)  # KOReader's Login button
    status = client.get(f"/api/devices/sync-keys/{key_id}").json()
    assert status["connected"] is True and status["latest_sync"] is None

    doc = client.get("/api/library").json()["works"][0]["files"][0]["partial_md5"]
    client.put(
        "/syncs/progress",
        json={"document": doc, "progress": "p", "percentage": 0.12, "device": "Kindle PW"},
        headers=h,
    )
    status = client.get(f"/api/devices/sync-keys/{key_id}").json()
    assert status["latest_sync"]["title"] == "Dune" and status["latest_sync"]["percentage"] == 0.12
    assert status["latest_sync"]["device"] == "Kindle PW"

    # Other users cannot poll it.
    api_post(
        client,
        "/api/admin/users",
        owner["csrf"],
        {"username": "mem", "role": "member", "password": "member password 123"},
    )
    post_form(client, "/logout", {"csrf_token": owner["csrf"]})
    login(client, "mem", "member password 123")
    assert client.get(f"/api/devices/sync-keys/{key_id}").status_code == 404


def test_devices_list_and_app_password_revoke(client, owner):
    r = api_post(client, "/api/devices/app-passwords", owner["csrf"], {"label": "Kobo"})
    assert r.status_code == 201
    secret, pw_id = r.json()["secret"], r.json()["app_password"]["id"]
    auth = {"authorization": "Basic " + base64.b64encode(f"owner:{secret}".encode()).decode()}
    assert client.get("/opds", headers=auth).status_code == 200
    devices = client.get("/api/devices").json()
    assert devices["app_passwords"][0]["last_used_at"] is not None
    assert (
        api_post(client, f"/api/devices/app-passwords/{pw_id}/revoke", owner["csrf"]).status_code
        == 200
    )
    assert client.get("/opds", headers=auth).status_code == 401
    assert (
        api_post(client, "/api/devices/app-passwords", owner["csrf"], {"label": ""}).status_code
        == 422
    )
    assert api_post(client, "/api/devices/sync-keys/999/revoke", owner["csrf"]).status_code == 404


def test_device_issues_paired_credentials_and_revokes_together(client, owner):
    legacy = api_post(client, "/api/devices/app-passwords", owner["csrf"], {"label": "Thorium"})
    r = api_post(client, "/api/devices", owner["csrf"], {"label": " Kobo Clara "})
    assert r.status_code == 201
    out = r.json()
    dev = out["device"]
    assert dev["label"] == "Kobo Clara" and dev["id"] == f"k{dev['sync_key']['id']}"
    assert dev["app_password"]["label"] == "Kobo Clara"
    assert out["sync_secret"] != out["opds_secret"]

    ko = ko_headers("owner", out["sync_secret"])
    opds = {
        "authorization": "Basic "
        + base64.b64encode(f"owner:{out['opds_secret']}".encode()).decode()
    }
    assert client.get("/users/auth", headers=ko).status_code == 200
    assert client.get("/opds", headers=opds).status_code == 200
    # Each secret works only for its own protocol.
    wrong_ko = ko_headers("owner", out["opds_secret"])
    assert client.get("/users/auth", headers=wrong_ko).status_code == 401
    assert (
        client.get(
            "/opds",
            headers={
                "authorization": "Basic "
                + base64.b64encode(f"owner:{out['sync_secret']}".encode()).decode()
            },
        ).status_code
        == 401
    )

    devices = client.get("/api/devices").json()["devices"]
    assert [d["label"] for d in devices] == ["Kobo Clara", "Thorium"]
    assert devices[0]["last_used_at"] is not None
    assert devices[1]["sync_key"] is None
    assert devices[1]["id"] == f"p{legacy.json()['app_password']['id']}"

    # Revoking via the app-password half still takes the whole device down.
    pw_half = f"p{dev['app_password']['id']}"
    r = api_post(client, f"/api/devices/{pw_half}/revoke", owner["csrf"])
    assert r.status_code == 200 and r.json()["device"]["revoked_at"] is not None
    assert client.get("/users/auth", headers=ko).status_code == 401
    assert client.get("/opds", headers=opds).status_code == 401
    actions = [e["action"] for e in client.get("/api/activity").json()["events"]]
    assert actions[:2] == ["device.revoke", "device.create"]

    assert api_post(client, "/api/devices/x1/revoke", owner["csrf"]).status_code == 404
    assert api_post(client, "/api/devices/k999/revoke", owner["csrf"]).status_code == 404
    assert (
        client.post("/api/devices", json={"label": "x"}, headers={"origin": ORIGIN}).status_code
        == 403
    )


def _upload(client, csrf, data: bytes, name="My Book.epub", ctype="application/epub+zip"):
    headers = {"origin": ORIGIN, "content-type": ctype, "x-filename": quote(name)}
    if csrf:
        headers["x-csrf-token"] = csrf
    return client.post("/api/books/upload", content=data, headers=headers)


def test_upload_streams_into_inbox_and_ingests(client, owner, settings, tmp_path):
    epub = make_epub(
        tmp_path / "src.epub", title="Uploaded Tale", authors=["Ida Upload"]
    ).read_bytes()
    assert _upload(client, "", epub).status_code == 403  # CSRF required
    assert _upload(client, owner["csrf"], b"not a zip").status_code == 422
    assert _upload(client, owner["csrf"], epub, ctype="application/pdf").status_code == 415
    r = _upload(client, owner["csrf"], epub, name="../../Évil name.epub")
    assert r.status_code == 202, r.text
    queued = settings.inbox_path / r.json()["queued"]
    assert queued.parent == settings.inbox_path and queued.name.startswith("Evil name-")
    assert sorted(p.name for p in settings.inbox_path.iterdir()) == [queued.name]

    w = InboxWatcher(settings.inbox_path, settings.library_dir, session_factory(), 0.01)
    w.scan_once()
    w.scan_once()
    titles = [x["title"] for x in client.get("/api/library").json()["works"]]
    assert titles == ["Uploaded Tale"]


def test_upload_limits_and_roles(client, owner, settings, tmp_path):
    settings.upload_max_bytes = 1000
    big = b"PK\x03\x04" + b"x" * 2000
    assert _upload(client, owner["csrf"], big).status_code == 413  # by Content-Length
    chunks = (big[i : i + 300] for i in range(0, len(big), 300))  # chunked: no length
    assert _upload(client, owner["csrf"], chunks).status_code == 413
    assert list(settings.inbox_path.iterdir()) == []  # partial upload cleaned up
    api_post(
        client,
        "/api/admin/users",
        owner["csrf"],
        {"username": "rdr", "role": "reader", "password": "reader password 123"},
    )
    post_form(client, "/logout", {"csrf_token": owner["csrf"]})
    csrf = login(client, "rdr", "reader password 123")
    assert _upload(client, csrf, b"PK\x03\x04").status_code == 403
    post_form(client, "/logout", {"csrf_token": csrf})
    assert _upload(client, csrf, b"PK\x03\x04").status_code == 401


def test_activity_scopes_and_since(client, owner, settings):
    _ingest(settings, ("Dune", "Frank Herbert"))
    created = make_sync_key_full(client, owner["csrf"], "Kobo")
    doc = client.get("/api/library").json()["works"][0]["files"][0]["partial_md5"]
    client.put(
        "/syncs/progress",
        json={"document": doc, "progress": "p", "percentage": 0.5, "device": "Kobo"},
        headers=ko_headers("owner", created["secret"]),
    )
    ev = client.get("/api/activity").json()
    actions = [e["action"] for e in ev["events"]]
    assert (
        "sync.progress" in actions
        and "sync_key.create" in actions
        and "setup.owner_created" in actions
    )
    sync = next(e for e in ev["events"] if e["kind"] == "sync")
    assert sync["target"] == "Dune" and sync["percentage"] == 0.5 and sync["device"] == "Kobo"
    # since= returns only newer events.
    later = client.get("/api/activity", params={"since": ev["now"]}).json()
    assert later["events"] == []
    assert client.get("/api/activity", params={"since": "garbage"}).status_code == 400

    # A member sees only their own events.
    api_post(
        client,
        "/api/admin/users",
        owner["csrf"],
        {"username": "mem", "role": "member", "password": "member password 123"},
    )
    post_form(client, "/logout", {"csrf_token": owner["csrf"]})
    login(client, "mem", "member password 123")
    mine = client.get("/api/activity").json()["events"]
    assert all(e["actor"] == "mem" or e["target"] == "mem" for e in mine)
    assert not any(e["kind"] == "sync" for e in mine)


def test_security_overview_sessions_and_password(client, owner):
    sec = client.get("/api/security").json()
    assert sec["has_password"] is True and sec["totp_enabled"] is False
    assert len(sec["sessions"]) == 1 and sec["sessions"][0]["current"] is True

    # Second browser signs in; first one revokes it.
    first_cookie = client.cookies.get("lectern_session")
    client.cookies.clear()
    login(client, "owner", OWNER_PASSWORD)
    second_cookie = client.cookies.get("lectern_session")
    client.cookies.set("lectern_session", first_cookie)
    sessions = client.get("/api/security").json()["sessions"]
    assert len(sessions) == 2
    other = next(s for s in sessions if not s["current"])
    assert (
        api_post(client, f"/api/security/sessions/{other['id']}/revoke", owner["csrf"]).status_code
        == 200
    )
    client.cookies.set("lectern_session", second_cookie)
    assert client.get("/api/me").status_code == 401
    client.cookies.set("lectern_session", first_cookie)

    r = api_post(
        client,
        "/api/security/password",
        owner["csrf"],
        {"current_password": "wrong", "new_password": "a brand new password"},
    )
    assert r.status_code == 400
    r = api_post(
        client,
        "/api/security/password",
        owner["csrf"],
        {"current_password": OWNER_PASSWORD, "new_password": "short"},
    )
    assert r.status_code == 400
    r = api_post(
        client,
        "/api/security/password",
        owner["csrf"],
        {"current_password": OWNER_PASSWORD, "new_password": "a brand new password"},
    )
    assert r.status_code == 200 and r.json()["signed_out"] is True
    assert client.get("/api/me").status_code == 401
    client.cookies.clear()
    login(client, "owner", "a brand new password")


def test_admin_users_api(client, owner):
    r = api_post(
        client,
        "/api/admin/users",
        owner["csrf"],
        {
            "username": "pat",
            "role": "reader",
            "password": "reader password 123",
            "display_name": "Pat",
        },
    )
    assert r.status_code == 201 and r.json()["user"]["role"] == "reader"
    uid = r.json()["user"]["id"]
    assert (
        api_post(
            client,
            "/api/admin/users",
            owner["csrf"],
            {"username": "pat", "role": "reader", "password": "reader password 123"},
        ).status_code
        == 400
    )
    assert (
        api_post(
            client,
            "/api/admin/users",
            owner["csrf"],
            {"username": "weak", "role": "reader", "password": "short"},
        ).status_code
        == 400
    )
    assert api_post(client, f"/api/admin/users/{uid}/disable", owner["csrf"]).status_code == 200
    users = {u["username"]: u for u in client.get("/api/admin/users").json()["users"]}
    assert users["pat"]["is_active"] is False
    me_id = users["owner"]["id"]
    assert api_post(client, f"/api/admin/users/{me_id}/disable", owner["csrf"]).status_code == 400
    assert api_post(client, f"/api/admin/users/{uid}/enable", owner["csrf"]).status_code == 200
    audit = client.get("/api/admin/audit").json()["rows"]
    assert [a["action"] for a in audit[:3]] == ["user.enable", "user.disable", "user.create"]


def test_csrf_header_only_on_api_writes(client, owner):
    # GETs never need the token; writes always do, even with the cookie present.
    assert client.get("/api/devices").status_code == 200
    r = client.post("/api/security/totp/begin", headers={"origin": "http://testserver"})
    assert r.status_code == 403
    assert csrf_of(client) == owner["csrf"]
