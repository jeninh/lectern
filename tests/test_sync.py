"""KOReader sync protocol: wire format, auth, two-device round trip, revocation."""

from tests.conftest import ko_headers, make_sync_key, post_form

DOC = "d41d8cd98f00b204e9800998ecf8427e"


def test_users_create_is_closed(client, owner):
    r = client.post("/users/create", json={"username": "someone", "password": "x"})
    assert r.status_code == 403
    assert "message" in r.json()


def test_auth_requires_valid_key(client, owner):
    secret = make_sync_key(client, owner["csrf"], "kobo")
    assert client.get("/users/auth", headers=ko_headers("owner", secret)).json() == {
        "authorized": "OK"
    }
    bad = client.get("/users/auth", headers=ko_headers("owner", "not-the-key"))
    assert bad.status_code == 401 and bad.json() == {"message": "Unauthorized"}
    # Right key, wrong username: keys are bound to their user.
    assert client.get("/users/auth", headers=ko_headers("intruder", secret)).status_code == 401
    assert client.get("/users/auth").status_code == 401


def test_two_devices_share_progress(client, owner):
    kobo = ko_headers("owner", make_sync_key(client, owner["csrf"], "kobo"))
    phone = ko_headers("owner", make_sync_key(client, owner["csrf"], "phone"))

    assert client.get(f"/syncs/progress/{DOC}", headers=kobo).json() == {}

    put = client.put(
        "/syncs/progress",
        json={
            "document": DOC,
            "progress": "/body/DocFragment[5]/body/p[3]/text().0",
            "percentage": 0.25,
            "device": "Kobo",
            "device_id": "A1",
        },
        headers=kobo,
    )
    assert put.status_code == 200
    assert put.json()["document"] == DOC and isinstance(put.json()["timestamp"], int)

    got = client.get(f"/syncs/progress/{DOC}", headers=phone).json()
    assert got["progress"] == "/body/DocFragment[5]/body/p[3]/text().0"
    assert got["percentage"] == 0.25
    assert got["device"] == "Kobo" and got["device_id"] == "A1"
    assert got["timestamp"] == put.json()["timestamp"]

    # Phone reads further; Kobo then sees the phone's position.
    client.put(
        "/syncs/progress",
        json={
            "document": DOC,
            "progress": 42,
            "percentage": 0.5,
            "device": "Phone",
            "device_id": "B2",
        },
        headers=phone,
    )
    got = client.get(f"/syncs/progress/{DOC}", headers=kobo).json()
    assert got["progress"] == "42" and got["percentage"] == 0.5 and got["device"] == "Phone"


def test_progress_is_per_user(client, owner):
    kobo = ko_headers("owner", make_sync_key(client, owner["csrf"], "kobo"))
    client.put(
        "/syncs/progress", json={"document": DOC, "progress": "p", "percentage": 0.9}, headers=kobo
    )
    # Create a second user and give them a key.
    r = post_form(
        client,
        "/admin/users",
        {
            "username": "reader",
            "role": "reader",
            "password": "reader password 123",
            "csrf_token": owner["csrf"],
        },
    )
    assert r.status_code == 200
    r = post_form(client, "/logout", {"csrf_token": owner["csrf"]})
    r = post_form(client, "/login", {"username": "reader", "password": "reader password 123"})
    assert r.status_code == 200
    from tests.conftest import csrf_of

    csrf = csrf_of(client.get("/account").text)
    reader_key = ko_headers("reader", make_sync_key(client, csrf, "kindle"))
    assert client.get(f"/syncs/progress/{DOC}", headers=reader_key).json() == {}


def test_revoked_key_stops_working(client, owner):
    secret = make_sync_key(client, owner["csrf"], "kobo")
    h = ko_headers("owner", secret)
    assert client.get("/users/auth", headers=h).status_code == 200
    page = client.get("/account").text
    import re

    key_id = re.search(r"/account/sync-keys/(\d+)/revoke", page).group(1)
    r = post_form(client, f"/account/sync-keys/{key_id}/revoke", {"csrf_token": owner["csrf"]})
    assert r.status_code == 200
    assert client.get("/users/auth", headers=h).status_code == 401
    assert (
        client.put(
            "/syncs/progress", json={"document": DOC, "progress": "p", "percentage": 0.1}, headers=h
        ).status_code
        == 401
    )


def test_validation_errors_use_koreader_shape(client, owner):
    h = ko_headers("owner", make_sync_key(client, owner["csrf"], "kobo"))
    r = client.put(
        "/syncs/progress", json={"document": DOC, "progress": "p", "percentage": 1.5}, headers=h
    )
    assert r.status_code == 400 and r.json() == {"message": "Invalid request"}
    r = client.put(
        "/syncs/progress",
        json={"document": "../etc", "progress": "p", "percentage": 0.5},
        headers=h,
    )
    assert r.status_code == 400
    assert client.get("/syncs/progress/not%20valid", headers=h).status_code == 400


def test_accepts_koreader_media_type(client, owner):
    h = ko_headers("owner", make_sync_key(client, owner["csrf"], "kobo"))
    h["accept"] = "application/vnd.koreader.v1+json"
    r = client.put(
        "/syncs/progress", json={"document": DOC, "progress": "p", "percentage": 0.1}, headers=h
    )
    assert r.status_code == 200
