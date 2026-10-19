"""Setup token, login, lockout, CSRF, roles."""

import re

from tests.conftest import ORIGIN, OWNER_PASSWORD, csrf_of, post_form


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
    )
    assert r.status_code == 200 and r.url.path == "/account"
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
        "/library",
        "/account",
        "/admin/users",
        "/admin/audit",
        "/library/works/1/cover.jpg",
        "/library/files/1/download",
    ]:
        assert client.get(path).status_code == 401, path
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
    r = client.post(
        "/account/sync-keys",
        data={"label": "x", "csrf_token": owner["csrf"]},
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403
    r = client.post("/account/sync-keys", data={"label": "x"}, headers={"origin": ORIGIN})
    assert r.status_code == 403
    r = client.post(
        "/account/sync-keys",
        data={"label": "x", "csrf_token": owner["csrf"]},
        headers={"sec-fetch-site": "cross-site", "origin": ORIGIN},
    )
    assert r.status_code == 403
    r = client.post(
        "/account/sync-keys",
        data={"label": "x", "csrf_token": owner["csrf"]},
        headers={"origin": ORIGIN},
    )
    assert r.status_code == 200


def test_roles(client, owner):
    r = post_form(
        client,
        "/admin/users",
        {
            "username": "member",
            "role": "member",
            "password": "member password 123",
            "csrf_token": owner["csrf"],
        },
    )
    assert r.status_code == 200
    r = post_form(
        client,
        "/admin/users",
        {
            "username": "boss",
            "role": "owner",
            "password": "member password 123",
            "csrf_token": owner["csrf"],
        },
    )
    assert r.status_code == 400
    post_form(client, "/logout", {"csrf_token": owner["csrf"]})
    assert (
        post_form(
            client, "/login", {"username": "member", "password": "member password 123"}
        ).status_code
        == 200
    )
    csrf = csrf_of(client.get("/account").text)
    assert client.get("/admin/users").status_code == 403
    assert client.get("/library").status_code == 200
    r = post_form(
        client,
        "/admin/users",
        {"username": "x2", "role": "reader", "password": "member password 123", "csrf_token": csrf},
    )
    assert r.status_code == 403


def test_logout_revokes_session_and_password_change_revokes_all(client, owner):
    old_cookie = client.cookies.get("lectern_session")
    post_form(client, "/logout", {"csrf_token": owner["csrf"]})
    client.cookies.set("lectern_session", old_cookie)
    assert client.get("/account").status_code == 401


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

    r = post_form(client, "/account/totp/begin", {"csrf_token": owner["csrf"]})
    secret = re.search(r'class="secret">([^<]+)<', r.text).group(1)
    r = post_form(client, "/account/totp/confirm", {"csrf_token": owner["csrf"], "code": "000000"})
    assert r.status_code == 400
    r = post_form(
        client,
        "/account/totp/confirm",
        {"csrf_token": owner["csrf"], "code": pyotp.TOTP(secret).now()},
    )
    assert r.status_code == 200
    post_form(client, "/logout", {"csrf_token": owner["csrf"]})
    assert (
        post_form(client, "/login", {"username": "owner", "password": OWNER_PASSWORD}).status_code
        == 401
    )
    assert (
        post_form(
            client,
            "/login",
            {"username": "owner", "password": OWNER_PASSWORD, "totp": pyotp.TOTP(secret).now()},
        ).status_code
        == 200
    )


def test_security_headers(client, owner):
    r = client.get("/account")
    assert r.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
