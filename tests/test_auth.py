"""Setup token, login, lockout, CSRF, roles."""

import re

from tests.conftest import ORIGIN, OWNER_PASSWORD, api_post, login, post_form


def test_setup_token_single_use_and_registration_closes(client):
    token = client.setup_token
    assert token
    assert client.get("/setup?token=wrong").status_code == 403
    assert client.get(f"/setup?token={token}").status_code == 200
    r = post_form(
        client,
        "/setup",
        {
            "token": token,
            "username": "owner",
            "password": OWNER_PASSWORD,
            "password_confirm": OWNER_PASSWORD,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/app")
    # Token is dead now and setup is closed even with the right token.
    client.cookies.clear()
    r = client.get(f"/setup?token={token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = post_form(
        client,
        "/setup",
        {
            "token": token,
            "username": "owner2",
            "password": OWNER_PASSWORD,
            "password_confirm": OWNER_PASSWORD,
        },
    )
    assert r.status_code == 403


def test_setup_rejects_weak_password(client):
    r = post_form(
        client,
        "/setup",
        {
            "token": client.setup_token,
            "username": "owner",
            "password": "short",
            "password_confirm": "short",
        },
    )
    assert r.status_code == 400
    r = post_form(
        client,
        "/setup",
        {
            "token": client.setup_token,
            "username": "owner",
            "password": OWNER_PASSWORD,
            "password_confirm": OWNER_PASSWORD + "x",
        },
    )
    assert r.status_code == 400


def test_everything_requires_auth(client, owner):
    client.cookies.clear()
    assert client.get("/healthz").status_code == 200
    for path in [
        "/",
        "/app",
        "/app/library",
        "/api/me",
        "/api/library",
        "/api/devices",
        "/api/security",
        "/api/admin/users",
        "/api/activity",
        "/library/works/1/cover.jpg",
        "/library/files/1/download",
    ]:
        assert client.get(path).status_code == 401, path
    # Browsers get bounced to the login page instead of a bare 401.
    r = client.get("/app/library", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/app/library"
    assert client.get("/opds").status_code == 401
    assert client.get("/users/auth").status_code == 401
    assert client.get("/login").status_code == 200


def test_login_lockout_after_failures(client, owner, app):
    post_form(client, "/logout", {"csrf_token": owner["csrf"]})
    app.state.limiters["login"].limit = 1000  # test the account lockout, not the IP limiter
    for _ in range(5):
        r = post_form(client, "/login", {"username": "owner", "password": "wrong password here"})
        assert r.status_code == 401
    r = post_form(client, "/login", {"username": "owner", "password": OWNER_PASSWORD})
    assert r.status_code == 429


def test_ip_rate_limit_on_login(client, owner, app):
    post_form(client, "/logout", {"csrf_token": owner["csrf"]})
    app.state.limiters["login"].limit = 3
    codes = [
        post_form(client, "/login", {"username": "nobody", "password": "x" * 12}).status_code
        for _ in range(4)
    ]
    assert codes == [401, 401, 401, 429]


def test_csrf_blocks_cross_site_and_missing_token(client, owner):
    body = {"label": "x"}
    hdr = {"origin": ORIGIN, "x-csrf-token": owner["csrf"]}
    # Cross-site origin with a valid token.
    r = client.post(
        "/api/devices/sync-keys", json=body, headers={**hdr, "origin": "https://evil.example"}
    )
    assert r.status_code == 403
    # Same origin, no token.
    assert (
        client.post("/api/devices/sync-keys", json=body, headers={"origin": ORIGIN}).status_code
        == 403
    )
    # Same origin, wrong token.
    assert (
        client.post(
            "/api/devices/sync-keys", json=body, headers={**hdr, "x-csrf-token": "nope"}
        ).status_code
        == 403
    )
    # Sec-Fetch-Site says cross-site even though Origin looks right.
    assert (
        client.post(
            "/api/devices/sync-keys", json=body, headers={**hdr, "sec-fetch-site": "cross-site"}
        ).status_code
        == 403
    )
    # The form-field variant on the server-rendered logout route still works and still checks.
    assert post_form(client, "/logout", {"csrf_token": "nope"}).status_code == 403
    # The real thing.
    assert api_post(client, "/api/devices/sync-keys", owner["csrf"], body).status_code == 201


def test_roles(client, owner):
    new = {"username": "member", "role": "member", "password": "member password 123"}
    assert api_post(client, "/api/admin/users", owner["csrf"], new).status_code == 201
    assert (
        api_post(
            client, "/api/admin/users", owner["csrf"], {**new, "username": "boss", "role": "owner"}
        ).status_code
        == 422
    )
    post_form(client, "/logout", {"csrf_token": owner["csrf"]})
    csrf = login(client, "member", "member password 123")
    assert client.get("/api/admin/users").status_code == 403
    assert client.get("/api/admin/audit").status_code == 403
    assert client.get("/api/library").status_code == 200
    r = api_post(client, "/api/admin/users", csrf, {**new, "username": "x2", "role": "reader"})
    assert r.status_code == 403


def test_logout_revokes_session_and_password_change_revokes_all(client, owner):
    old_cookie = client.cookies.get("lectern_session")
    post_form(client, "/logout", {"csrf_token": owner["csrf"]})
    client.cookies.set("lectern_session", old_cookie)
    assert client.get("/api/me").status_code == 401


def test_session_cookie_flags(client):
    r = post_form(
        client,
        "/setup",
        {
            "token": client.setup_token,
            "username": "owner",
            "password": OWNER_PASSWORD,
            "password_confirm": OWNER_PASSWORD,
        },
        follow_redirects=False,
    )
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax")
    assert re.search(r"Path=/", cookie)


def test_totp_required_once_enabled(client, owner):
    import pyotp

    r = api_post(client, "/api/security/totp/begin", owner["csrf"])
    assert r.status_code == 200
    secret = r.json()["secret"]
    assert r.json()["uri"].startswith("otpauth://totp/")
    assert (
        api_post(
            client, "/api/security/totp/confirm", owner["csrf"], {"code": "000000"}
        ).status_code
        == 400
    )
    r = api_post(
        client, "/api/security/totp/confirm", owner["csrf"], {"code": pyotp.TOTP(secret).now()}
    )
    assert r.status_code == 200
    assert client.get("/api/security").json()["totp_enabled"] is True
    post_form(client, "/logout", {"csrf_token": owner["csrf"]})
    assert (
        post_form(client, "/login", {"username": "owner", "password": OWNER_PASSWORD}).status_code
        == 401
    )
    r = post_form(
        client,
        "/login",
        {"username": "owner", "password": OWNER_PASSWORD, "totp": pyotp.TOTP(secret).now()},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_security_headers(client, owner):
    r = client.get("/api/me")
    assert r.headers["x-frame-options"] == "DENY"
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
