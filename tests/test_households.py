"""Household sharing: invites, roles, shelves, grants, and reader visibility on every route."""

from __future__ import annotations

import base64
from datetime import timedelta

from tests.conftest import (
    OWNER_PASSWORD,
    api_delete,
    api_post,
    drain_inbox,
    ko_headers,
    login,
    make_app_password,
    make_epub,
    make_sync_key,
    make_user,
    post_form,
    put_json,
)

from lectern.db import session_factory
from lectern.models import Invite, utcnow


def _invite(client, csrf, role="reader", **kw):
    r = api_post(client, "/api/invites", csrf, {"role": role, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _accept(client, token, username, password="a long enough password", **extra):
    return post_form(
        client,
        f"/invite/{token}",
        {"username": username, "password": password, "password_confirm": password, **extra},
        follow_redirects=False,
    )


def test_invite_lifecycle(client, owner, settings):
    csrf = owner["csrf"]
    inv = _invite(client, csrf, "reader", note="Grandma", expires_in_hours=24)
    assert inv["url"] == f"{settings.base_url}/invite/{inv['token']}" and inv["state"] == "pending"
    assert api_post(client, "/api/invites", csrf, {"role": "owner"}).status_code == 422
    assert (
        api_post(
            client, "/api/invites", csrf, {"role": "member", "expires_in_hours": 721}
        ).status_code
        == 422
    )
    listed = client.get("/api/invites").json()["invites"]
    assert "token" not in listed[0] and listed[0]["note"] == "Grandma"

    client.cookies.clear()
    page = client.get(f"/invite/{inv['token']}")
    assert (
        page.status_code == 200 and "reader" in page.text and "shelves shared with you" in page.text
    )
    r = _accept(client, inv["token"], "Bad Name")
    assert r.status_code == 400 and "lowercase" in r.text
    r = _accept(client, inv["token"], "gran", password="short")
    assert r.status_code == 400
    r = _accept(client, inv["token"], "gran")
    assert r.status_code == 303 and r.headers["location"] == "/app?welcome=1"
    me = client.get("/api/me").json()["user"]
    assert me["username"] == "gran" and me["role"] == "reader"
    # One use only; the used link looks exactly like an unknown one.
    client.cookies.clear()
    assert client.get(f"/invite/{inv['token']}").status_code == 404
    assert _accept(client, inv["token"], "gran2").status_code == 404
    assert client.get("/invite/not-a-token").status_code == 404

    csrf = login(client, "owner", OWNER_PASSWORD)
    states = {i["note"]: i for i in client.get("/api/invites").json()["invites"]}
    assert states["Grandma"]["state"] == "used" and states["Grandma"]["used_by"] == "gran"
    titles = [n["title"] for n in client.get("/api/notifications").json()["items"]]
    assert "gran joined as a reader" in titles

    revoked = _invite(client, csrf, "member", note="Revoked")
    assert api_delete(client, f"/api/invites/{revoked['id']}", csrf).status_code == 204
    expired = _invite(client, csrf, "member", note="Expired")
    with session_factory()() as db:
        db.get(Invite, expired["id"]).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    states = {i["note"]: i["state"] for i in client.get("/api/invites").json()["invites"]}
    assert states["Revoked"] == "revoked" and states["Expired"] == "expired"
    client.cookies.clear()
    for token in (revoked["token"], expired["token"]):
        assert client.get(f"/invite/{token}").status_code == 404
        assert _accept(client, token, "sneaky").status_code == 404

    # Only the owner manages invites.
    csrf = login(client, "owner", OWNER_PASSWORD)
    make_user(client, csrf, "mia", "member")
    mia = login(client, "mia", "mia-correct horse")
    assert api_post(client, "/api/invites", mia, {"role": "reader"}).status_code == 403
    assert client.get("/api/invites").status_code == 403


def test_member_invite_sees_whole_library(client, owner, settings):
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    make_epub(settings.inbox_path / "a.epub", title="Alpha")
    drain_inbox(settings)
    inv = _invite(client, owner["csrf"], "member")
    client.cookies.clear()
    assert _accept(client, inv["token"], "kid").status_code == 303
    assert [w["title"] for w in client.get("/api/library").json()["works"]] == ["Alpha"]


def test_shelves_grants_and_reader_visibility(client, owner, settings):
    csrf = owner["csrf"]
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    for i, t in enumerate(("Shared One", "Shared Two", "Private")):
        make_epub(
            settings.inbox_path / f"{i}.epub", title=t, authors=["Ann"], cover=True, padding=i * 50
        )
    drain_inbox(settings)
    ids = {w["title"]: w["id"] for w in client.get("/api/library").json()["works"]}
    reader = make_user(client, csrf, "rex", "reader")
    make_user(client, csrf, "mia", "member")

    r = api_post(client, "/api/shelves", csrf, {"name": "Kids"})
    assert r.status_code == 201 and r.json()["permission"] == "owner"
    shelf = r.json()["id"]
    assert api_post(client, "/api/shelves", csrf, {"name": "Kids"}).status_code == 409
    r = put_json(client, f"/api/shelves/{shelf}/items", csrf,
                 {"work_ids": [ids["Shared Two"], ids["Shared One"]]})  # fmt: skip
    assert r.status_code == 200
    detail = client.get(f"/api/shelves/{shelf}").json()
    assert [w["title"] for w in detail["works"]] == ["Shared Two", "Shared One"]  # manual order
    assert client.get(f"/api/library?shelf={shelf}").json()["total"] == 2

    # Before a grant the reader sees nothing at all.
    rex = login(client, "rex", reader["password"])
    assert client.get("/api/library").json()["works"] == []
    assert client.get("/api/shelves").json() == {"shelves": [], "can_create": False}
    assert client.get(f"/api/shelves/{shelf}").status_code == 404
    assert client.get(f"/api/works/{ids['Shared One']}").status_code == 404
    assert api_post(client, "/api/shelves", rex, {"name": "Mine"}).status_code == 403
    ko_key = make_sync_key(client, rex, "Rex Kobo")
    rex_pw = make_app_password(client, rex, "Rex OPDS")

    csrf = login(client, "owner", OWNER_PASSWORD)
    r = put_json(
        client, f"/api/shelves/{shelf}/grants/{reader['id']}", csrf, {"permission": "view"}
    )
    assert r.status_code == 200
    grants = client.get(f"/api/shelves/{shelf}/grants").json()["grants"]
    assert grants[0]["user"]["username"] == "rex" and grants[0]["permission"] == "view"
    priv = ids["Private"]
    file_id = client.get(f"/api/works/{priv}").json()["files"][0]["id"]
    shared_file = client.get(f"/api/works/{ids['Shared One']}").json()["files"][0]["id"]

    # The reader now sees exactly the shelf, everywhere.
    rex = login(client, "rex", reader["password"])
    assert sorted(w["title"] for w in client.get("/api/library").json()["works"]) == [
        "Shared One",
        "Shared Two",
    ]
    assert client.get("/api/me").json()["counts"]["works"] == 2
    (s,) = client.get("/api/shelves").json()["shelves"]
    assert s["permission"] == "view" and s["work_count"] == 2
    assert client.get(f"/api/works/{ids['Shared One']}").status_code == 200
    hidden = [
        f"/api/works/{priv}",
        f"/library/works/{priv}/cover.jpg",
        f"/library/files/{file_id}/download",
        f"/library/works/{priv}/book.epub",
        f"/api/works/{priv}/progress",
        f"/api/works/{priv}/tracks",
        f"/api/works/{priv}/sentences?from=0",
    ]
    for url in hidden:
        assert client.get(url).status_code == 404, url
    assert put_json(client, f"/api/works/{priv}/progress", rex,
                    {"source": "web", "spine_index": 0, "char": 0}).status_code == 404  # fmt: skip
    assert client.get(f"/library/files/{shared_file}/download").status_code == 200
    # View-only: no editing, no grant management.
    assert (
        api_post(
            client, f"/api/shelves/{shelf}/items", rex, {"work_id": ids["Shared One"]}
        ).status_code
        == 403
    )
    assert client.get(f"/api/shelves/{shelf}/grants").status_code == 403

    # OPDS and KOReader sync honour the same rule.
    basic = {"authorization": "Basic " + base64.b64encode(f"rex:{rex_pw}".encode()).decode()}
    client.cookies.clear()
    feed = client.get("/opds/all", headers=basic).text
    assert "Shared One" in feed and "Private" not in feed
    assert client.get(f"/opds/files/{file_id}/download", headers=basic).status_code == 404
    assert client.get(f"/opds/works/{priv}/cover.jpg", headers=basic).status_code == 404
    assert client.get(f"/opds/files/{shared_file}/download", headers=basic).status_code == 200
    assert "<totalResults>0<" in client.get("/opds/search?q=Private", headers=basic).text.replace(
        "opensearch:", ""
    )
    assert client.get("/users/auth", headers=ko_headers("rex", ko_key)).status_code == 200

    # An edit grant lets the reader add works they can see, but not others.
    csrf = login(client, "owner", OWNER_PASSWORD)
    put_json(client, f"/api/shelves/{shelf}/grants/{reader['id']}", csrf, {"permission": "edit"})
    other = api_post(client, "/api/shelves", csrf, {"name": "Grown-ups"}).json()["id"]
    api_post(client, f"/api/shelves/{other}/items", csrf, {"work_id": priv})
    rex = login(client, "rex", reader["password"])
    assert (
        api_delete(client, f"/api/shelves/{shelf}/items/{ids['Shared Two']}", rex).status_code
        == 204
    )
    assert (
        api_post(client, f"/api/shelves/{shelf}/items", rex, {"work_id": priv}).status_code == 404
    )
    assert client.get(f"/api/shelves/{other}").status_code == 404
    assert [w["title"] for w in client.get("/api/library").json()["works"]] == ["Shared One"]
    assert client.request("PATCH", f"/api/shelves/{shelf}", json={"name": "x"},
                          headers={"origin": "http://testserver", "x-csrf-token": rex}).status_code == 403  # fmt: skip

    # Members see the whole library and only their own or granted shelves.
    mia = login(client, "mia", "mia-correct horse")
    assert len(client.get("/api/library").json()["works"]) == 3
    assert client.get("/api/shelves").json()["shelves"] == []
    mine = api_post(client, "/api/shelves", mia, {"name": "Kids"}).json()  # names are per owner
    assert mine["permission"] == "owner"
    assert client.get(f"/api/shelves/{shelf}").status_code == 404

    # Revoking the grant hides everything again; deleting a shelf never deletes works.
    csrf = login(client, "owner", OWNER_PASSWORD)
    assert len(client.get("/api/shelves").json()["shelves"]) == 3  # owner sees all
    assert (
        api_delete(client, f"/api/shelves/{shelf}/grants/{reader['id']}", csrf).status_code == 204
    )
    # A shelf with works on it asks what to do with them first.
    r = api_delete(client, f"/api/shelves/{shelf}", csrf)
    assert r.status_code == 409 and r.json()["detail"] == "shelf_not_empty"
    assert api_delete(client, f"/api/shelves/{shelf}?contents=move_up", csrf).status_code == 204
    assert len(client.get("/api/library").json()["works"]) == 3
    login(client, "rex", reader["password"])
    assert client.get("/api/library").json()["works"] == []


def test_role_change_takes_effect_on_next_request(client, owner, settings):
    csrf = owner["csrf"]
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    make_epub(settings.inbox_path / "a.epub", title="Alpha")
    drain_inbox(settings)
    mia = make_user(client, csrf, "mia", "member")
    r = api_post(client, f"/api/admin/users/{mia['id']}/role", csrf, {"role": "reader"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "reader"
    me = client.get("/api/me").json()["user"]
    assert (
        api_post(client, f"/api/admin/users/{me['id']}/role", csrf, {"role": "reader"}).status_code
        == 400
    )
    login(client, "mia", mia["password"])
    assert client.get("/api/library").json()["works"] == []


def test_invite_claim_is_atomic(client, owner, monkeypatch):
    """Two accepts that both passed the validity check: only the first creates an account."""
    from lectern.routes import invites

    inv = _invite(client, owner["csrf"], "reader")
    client.cookies.clear()
    with session_factory()() as db:
        stale = db.query(Invite).one()  # loaded while still unused
        db.expunge(stale)
    assert _accept(client, inv["token"], "first").status_code == 303
    client.cookies.clear()
    monkeypatch.setattr(invites, "_usable", lambda db, token: db.merge(stale, load=False))
    r = _accept(client, inv["token"], "second")
    assert r.status_code == 404
    from lectern.models import User

    with session_factory()() as db:
        users = {u.username: u.id for u in db.query(User)}
        assert db.query(Invite).one().used_by == users["first"]
    assert "second" not in users


def test_invite_username_race_is_a_clean_400(client, owner, monkeypatch):
    """A username taken between the form check and the insert answers 400 and keeps the link."""
    from lectern.models import Role, User
    from lectern.routes import invites
    from lectern.security.passwords import hash_password

    inv = _invite(client, owner["csrf"], "member")
    client.cookies.clear()

    def racing_hash(password):
        with session_factory()() as db:
            db.add(User(username="zoe", display_name="zoe", role=Role.MEMBER,
                        password_hash="x", webauthn_user_handle=b"z" * 32))  # fmt: skip
            db.commit()
        return hash_password(password)

    monkeypatch.setattr(invites, "hash_password", racing_hash)
    r = _accept(client, inv["token"], "zoe")
    assert r.status_code == 400 and "That username is taken." in r.text
    monkeypatch.setattr(invites, "hash_password", hash_password)
    with session_factory()() as db:
        assert db.query(Invite).one().used_at is None
    assert _accept(client, inv["token"], "zoe2").status_code == 303


def test_only_owners_raise_device_caps(client, owner, settings):
    csrf = owner["csrf"]
    make_user(client, csrf, "mia", "member")
    make_user(client, csrf, "rex", "reader")
    settings.download_device_cap_per_hour = 60

    def set_cap(who_csrf, cap):
        (pw,) = client.get("/api/devices").json()["app_passwords"]
        return api_post(
            client, f"/api/devices/app-passwords/{pw['id']}/download-cap", who_csrf, {"cap": cap}
        )

    for name in ("mia", "rex"):
        c = login(client, name, f"{name}-correct horse")
        make_app_password(client, c, "Kobo")
        assert set_cap(c, 61).status_code == 403  # above the server default
        assert set_cap(c, 0).status_code == 403  # 0 = unlimited
        assert set_cap(c, 10).status_code == 200
        assert set_cap(c, 11).status_code == 403  # raising their own lowered cap
        assert set_cap(c, None).status_code == 403  # back to the default 60 is a raise
        r = set_cap(c, 10)
        assert r.status_code == 200 and r.json()["app_password"]["download_cap_per_hour"] == 10
        assert set_cap(c, 3).json()["app_password"]["download_cap_per_hour"] == 3

    csrf = login(client, "owner", OWNER_PASSWORD)
    make_app_password(client, csrf, "Kobo")
    for cap in (1, 500, 0, None):
        assert set_cap(csrf, cap).status_code == 200


def test_activity_sync_rows_follow_visibility(client, owner, settings):
    from lectern.models import BookFile, ReadingProgress, User

    csrf = owner["csrf"]
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    make_epub(settings.inbox_path / "a.epub", title="Granted Book")
    drain_inbox(settings)
    work = client.get("/api/library").json()["works"][0]
    reader = make_user(client, csrf, "rex", "reader")
    shelf = api_post(client, "/api/shelves", csrf, {"name": "Rex"}).json()["id"]
    api_post(client, f"/api/shelves/{shelf}/items", csrf, {"work_id": work["id"]})
    put_json(client, f"/api/shelves/{shelf}/grants/{reader['id']}", csrf, {"permission": "view"})
    with session_factory()() as db:
        doc = db.query(BookFile).one().partial_md5
        rex_id = db.query(User).filter_by(username="rex").one().id
        db.add(ReadingProgress(user_id=rex_id, document=doc, progress="/body/p[1]",
                               percentage=0.4, device="Kobo", timestamp=1))  # fmt: skip
        db.commit()

    def sync_events():
        events = client.get("/api/activity").json()["events"]
        return [e for e in events if e["kind"] == "sync"]

    login(client, "rex", reader["password"])
    assert [e["target"] for e in sync_events()] == ["Granted Book"]
    csrf = login(client, "owner", OWNER_PASSWORD)
    assert api_delete(client, f"/api/shelves/{shelf}/items/{work['id']}", csrf).status_code == 204
    login(client, "rex", reader["password"])
    assert sync_events() == []


def test_reader_grants_and_reader_edits_cannot_widen_other_readers(client, owner, settings):
    csrf = owner["csrf"]
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    for i, t in enumerate(("Kids Book", "Rex Only")):
        make_epub(settings.inbox_path / f"{i}.epub", title=t, padding=i * 50)
    drain_inbox(settings)
    ids = {w["title"]: w["id"] for w in client.get("/api/library").json()["works"]}
    rex = make_user(client, csrf, "rex", "reader")
    ann = make_user(client, csrf, "ann", "reader")
    make_user(client, csrf, "mia", "member")

    # A member can share their shelf with members but not with readers.
    mia = login(client, "mia", "mia-correct horse")
    mine = api_post(client, "/api/shelves", mia, {"name": "Mia's"}).json()["id"]
    r = put_json(client, f"/api/shelves/{mine}/grants/{rex['id']}", mia, {"permission": "view"})
    assert r.status_code == 403 and r.json()["detail"] == "only_owner_grants_readers"

    csrf = login(client, "owner", OWNER_PASSWORD)
    kids = api_post(client, "/api/shelves", csrf, {"name": "Kids"}).json()["id"]
    private = api_post(client, "/api/shelves", csrf, {"name": "Rex"}).json()["id"]
    api_post(client, f"/api/shelves/{kids}/items", csrf, {"work_id": ids["Kids Book"]})
    api_post(client, f"/api/shelves/{private}/items", csrf, {"work_id": ids["Rex Only"]})
    put_json(client, f"/api/shelves/{kids}/grants/{rex['id']}", csrf, {"permission": "edit"})
    put_json(client, f"/api/shelves/{kids}/grants/{ann['id']}", csrf, {"permission": "view"})
    put_json(client, f"/api/shelves/{private}/grants/{rex['id']}", csrf, {"permission": "view"})

    rex_csrf = login(client, "rex", rex["password"])
    r = api_post(client, f"/api/shelves/{kids}/items", rex_csrf, {"work_id": ids["Rex Only"]})
    assert r.status_code == 403 and r.json()["detail"] == "shelf_shared_with_readers"
    r = put_json(client, f"/api/shelves/{kids}/items", rex_csrf,
                 {"work_ids": [ids["Kids Book"], ids["Rex Only"]]})  # fmt: skip
    assert r.status_code == 403
    # Reordering or removing never widens anyone's view.
    r = put_json(client, f"/api/shelves/{kids}/items", rex_csrf, {"work_ids": [ids["Kids Book"]]})
    assert r.status_code == 200
    ann_csrf = login(client, "ann", ann["password"])
    assert [w["title"] for w in client.get("/api/library").json()["works"]] == ["Kids Book"]
    del ann_csrf

    # Once no other reader can see the shelf, the edit grant works as before.
    csrf = login(client, "owner", OWNER_PASSWORD)
    api_delete(client, f"/api/shelves/{kids}/grants/{ann['id']}", csrf)
    rex_csrf = login(client, "rex", rex["password"])
    r = api_post(client, f"/api/shelves/{kids}/items", rex_csrf, {"work_id": ids["Rex Only"]})
    assert r.status_code == 201
