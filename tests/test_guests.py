"""Guest accounts (issue #77, docs/guests.md): read-only readers who see only the shelves they
were given, on every surface, and nothing of the owner's."""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from lxml import etree
from sqlalchemy import select
from tests.conftest import (
    ORIGIN,
    OWNER_PASSWORD,
    api_post,
    drain_inbox,
    login,
    make_epub,
    post_form,
    put_json,
)

from lectern import ai_keys, guests, search
from lectern.db import session_factory
from lectern.models import (
    AppPassword,
    AuditLog,
    BookFile,
    GuestProfile,
    Invite,
    ReadingState,
    Role,
    Session,
    ShelfGrant,
    ShelfPermission,
    SyncKey,
    User,
    Work,
    utcnow,
)
from lectern.security.tokens import sha256_hex

PASSWORD = "a long enough guest password"
# A word only the books the guest must not see contain, so a leak is unambiguous.
SECRET_WORD = "zanzibarquokka"


def _accept(client: TestClient, token: str, username: str = "cousin"):
    return post_form(
        client,
        f"/invite/{token}",
        {"username": username, "password": PASSWORD, "password_confirm": PASSWORD},
        follow_redirects=False,
    )


def _csrf(client: TestClient) -> str:
    return client.get("/api/me").json()["csrf_token"]


def _basic(username: str, secret: str) -> dict[str, str]:
    return {"authorization": "Basic " + base64.b64encode(f"{username}:{secret}".encode()).decode()}


@pytest.fixture
def world(app, client, owner, settings):
    """Owner library: Public (with sub-shelf Kids), Secret, and a book on no shelf; plus a guest
    with VIEW on Public only, signed in on ``gc``."""
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    titles = {
        "Public Book": "A public sentence.",
        "Kids Book": "A kids sentence.",
        "Secret Book": f"The {SECRET_WORD} sentence.",
        "Loose Book": f"A loose {SECRET_WORD} sentence.",
    }
    for i, (title, text) in enumerate(titles.items()):
        make_epub(
            settings.inbox_path / f"{i}.epub",
            title=title,
            authors=["Ann"],
            cover=True,
            chapters=[f"<p>{text}</p>"],
        )
    drain_inbox(settings)
    csrf = owner["csrf"]
    works = {w["title"]: w["id"] for w in client.get("/api/library").json()["works"]}

    def shelf(name: str, parent: int | None = None, *works_on: str) -> int:
        r = api_post(client, "/api/shelves", csrf, {"name": name, "parent_id": parent})
        assert r.status_code == 201, r.text
        for t in works_on:
            assert (
                api_post(
                    client, f"/api/shelves/{r.json()['id']}/items", csrf, {"work_id": works[t]}
                ).status_code
                == 201
            )
        return r.json()["id"]

    public = shelf("Public", None, "Public Book")
    kids = shelf("Kids", public, "Kids Book")
    secret = shelf("Secret", None, "Secret Book")
    invite = api_post(
        client,
        "/api/invites",
        csrf,
        {"role": "reader", "note": "Cousin", "guest": {"shelf_ids": [public]}},
    )
    assert invite.status_code == 201, invite.text
    gc = TestClient(app)
    r = _accept(gc, invite.json()["token"])
    assert r.status_code == 303, r.text
    with session_factory()() as db:
        uid = db.scalar(select(User.id).where(User.username == "cousin"))
    return SimpleNamespace(
        owner=client,
        csrf=csrf,
        gc=gc,
        gcsrf=_csrf(gc),
        works=works,
        public=public,
        kids=kids,
        secret=secret,
        uid=uid,
        settings=settings,
    )


def _error_body(r) -> object:
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text


# --- invite, account, grants -----------------------------------------------------------------


def test_guest_invite_creates_a_view_only_reader_on_the_chosen_shelves(world):
    me = world.gc.get("/api/me").json()
    assert me["user"]["role"] == "reader"
    assert me["user"]["guest"] == {"expires_at": None, "expired": False, "allow_opds": True}
    with session_factory()() as db:
        grants = db.scalars(select(ShelfGrant).where(ShelfGrant.user_id == world.uid)).all()
        assert [(g.shelf_id, g.permission) for g in grants] == [
            (world.public, ShelfPermission.VIEW)
        ]
        kinds = {a.action for a in db.scalars(select(AuditLog))}
    assert {"invite.create", "invite.accept", "guest.create", "login.success"} <= kinds | {
        "login.success"
    }
    assert world.gc.get("/api/shelves").json()["can_create"] is False
    listed = world.owner.get("/api/admin/guests").json()["guests"]
    assert [g["username"] for g in listed] == ["cousin"]
    assert listed[0]["shelves"] == [{"id": world.public, "name": "Public"}]


def test_guest_invite_validation(world):
    def invite(**guest):
        return api_post(
            world.owner,
            "/api/invites",
            world.csrf,
            {"role": guest.pop("role", "reader"), "guest": {"shelf_ids": [world.public], **guest}},
        )

    assert invite(role="member").status_code == 422
    assert invite(shelf_ids=[]).status_code == 422
    assert invite(shelf_ids=[99999]).status_code == 422
    assert invite(expires_at=(utcnow() - timedelta(days=1)).isoformat()).status_code == 422
    assert invite(opinion="x").status_code == 422
    smart = api_post(
        world.owner, "/api/shelves", world.csrf, {"name": "Smart", "filter": "author:Ann"}
    ).json()["id"]
    assert invite(shelf_ids=[smart]).status_code == 409
    # The invite page names no shelf, and says what a guest gets.
    ok = invite()
    page = TestClient(world.gc.app).get(f"/invite/{ok.json()['token']}")
    assert "nothing can be changed" in page.text and "Public" not in page.text


def test_a_guest_never_holds_edit_and_the_owner_cannot_grant_it(world):
    r = put_json(
        world.owner,
        f"/api/shelves/{world.public}/grants/{world.uid}",
        world.csrf,
        {"permission": "edit"},
    )
    assert r.status_code == 422 and r.json()["detail"] == "guest_view_only"
    smart = api_post(
        world.owner, "/api/shelves", world.csrf, {"name": "Smart", "filter": "author:Ann"}
    ).json()["id"]
    r = put_json(
        world.owner, f"/api/shelves/{smart}/grants/{world.uid}", world.csrf, {"permission": "view"}
    )
    assert r.status_code == 409
    # Even an EDIT grant forced into the database (a bug, a restore) confers nothing.
    with session_factory()() as db:
        g = db.get(ShelfGrant, (world.public, world.uid))
        g.permission = ShelfPermission.EDIT
        db.commit()
    assert world.gc.get(f"/api/shelves/{world.public}").json()["permission"] == "view"
    r = api_post(
        world.gc,
        f"/api/shelves/{world.public}/items",
        world.gcsrf,
        {"work_id": world.works["Public Book"]},
    )
    assert r.status_code == 403


def test_a_guest_cannot_be_made_a_member(world):
    r = api_post(world.owner, f"/api/admin/users/{world.uid}/role", world.csrf, {"role": "member"})
    assert r.status_code == 409
    # And a guest row whose role was changed anyway still sees only its grants.
    with session_factory()() as db:
        db.get(User, world.uid).role = Role.MEMBER
        db.commit()
    titles = {w["title"] for w in world.gc.get("/api/library").json()["works"]}
    assert titles == {"Public Book", "Kids Book"}


# --- visibility on every surface -------------------------------------------------------------


def test_library_work_cover_and_file_routes_show_only_granted_shelves(world):
    gc, w = world.gc, world.works
    lib = gc.get("/api/library").json()
    assert {x["title"] for x in lib["works"]} == {"Public Book", "Kids Book"}  # nested inherited
    assert lib["total"] == 2
    assert gc.get("/api/me").json()["counts"]["works"] == 2
    detail = gc.get(f"/api/works/{w['Kids Book']}")
    assert detail.status_code == 200
    assert gc.get(f"/library/works/{w['Kids Book']}/cover.jpg").status_code == 200
    assert gc.get(f"/library/works/{w['Kids Book']}/book.epub").status_code == 200
    assert gc.get(f"/api/works/{w['Kids Book']}/text/0").status_code == 200

    with session_factory()() as db:
        secret_file = db.scalar(select(BookFile.id).where(BookFile.work_id == w["Secret Book"]))
    missing = {
        "detail": gc.get("/api/works/999999"),
        "cover": gc.get("/library/works/999999/cover.jpg"),
        "view": gc.get("/library/files/999999/view"),
        "text": gc.get("/api/works/999999/text/0"),
        "pages": gc.get("/api/files/999999/pages"),
    }
    for hidden in ("Secret Book", "Loose Book"):
        wid = w[hidden]
        got = {
            "detail": gc.get(f"/api/works/{wid}"),
            "cover": gc.get(f"/library/works/{wid}/cover.jpg"),
            "text": gc.get(f"/api/works/{wid}/text/0"),
            "tracks": gc.get(f"/api/works/{wid}/tracks"),
            "progress": gc.get(f"/api/works/{wid}/progress"),
            "rewind": gc.get(f"/api/works/{wid}/rewind-point", params={"t": 0}),
            "epub": gc.get(f"/library/works/{wid}/book.epub"),
            "sentences": gc.get(f"/api/works/{wid}/sentences"),
            "read-aloud": gc.get(f"/api/works/{wid}/read-aloud"),
        }
        for name, r in got.items():
            assert r.status_code == 404, (hidden, name, r.status_code, r.text[:200])
            assert SECRET_WORD not in r.text and hidden not in r.text
    for name, r in missing.items():
        assert r.status_code == 404, (name, r.status_code)
    r = gc.get(f"/library/files/{secret_file}/view")
    assert r.status_code == 404 and r.text == missing["view"].text  # no existence oracle
    assert gc.get(f"/library/files/{secret_file}/download").status_code == 404
    assert gc.get(f"/api/files/{secret_file}/pages").status_code == 404
    assert gc.get(f"/api/files/{secret_file}/text").status_code == 404
    r = put_json(
        gc,
        f"/api/works/{w['Secret Book']}/progress",
        world.gcsrf,
        {"source": "web", "sentence_n": 0},
    )
    assert r.status_code == 404
    r = api_post(gc, f"/api/works/{w['Secret Book']}/listen", world.gcsrf, {})
    assert r.status_code == 404


def test_facets_filters_and_counts_cover_only_visible_works(world):
    gc = world.gc
    titles = lambda **p: {  # noqa: E731
        x["title"] for x in gc.get("/api/library", params=p).json()["works"]
    }
    assert titles(q="Secret") == set() and titles(q="Loose") == set()
    assert titles(f='title:"Secret Book"') == set()
    hidden = gc.get("/api/library", params={"f": "shelf:Secret"})  # a hidden shelf is unknown
    assert hidden.status_code == 422 and "works" not in hidden.json()
    assert gc.get("/api/library", params={"shelf": world.secret}).status_code == 404
    assert gc.get("/api/library", params={"unshelved": True}).json()["total"] == 0
    facets = gc.get("/api/library/facets", params={"field": "author"}).json()["values"]
    assert facets == [{"value": "Ann", "count": 2}]  # not 4
    assert gc.get("/api/library/facets", params={"field": "by"}).status_code == 403
    count = gc.get("/api/library/count").json()
    assert count["total"] == 2


def test_shelves_hide_other_shelves_owners_and_counts(world):
    gc = world.gc
    listed = gc.get("/api/shelves").json()["shelves"]
    assert [s["name"] for s in listed] == ["Public", "Kids"]
    for s in listed:
        assert s["owner"] is None and s["owner_id"] is None and s["grant_count"] == 0
        assert s["permission"] == "view"
    assert listed[0]["work_count"] == 1 and listed[0]["shelf_count"] == 1
    assert gc.get(f"/api/shelves/{world.secret}").status_code == 404
    assert gc.get("/api/shelves/999999").status_code == 404
    one = gc.get(f"/api/shelves/{world.public}").json()
    assert [s["name"] for s in one["shelves"]] == ["Kids"]
    assert {x["title"] for x in one["works"]} == {"Public Book"}
    # Nobody learns of the owner's shelves from the guest's own responses.
    assert "Secret" not in gc.get("/api/shelves").text
    me = gc.get("/api/me").json()
    assert me["counts"]["users"] == 0 and me["inbox_path"] == "" and me["library_settings"] == {}


def test_search_snippets_and_opds_search_hide_unshared_text(world):
    search.reconcile(session_factory(), world.settings, budget_s=None)
    owner_hits = world.owner.get("/api/search/text", params={"q": SECRET_WORD}).json()
    assert owner_hits["results"], owner_hits  # the index does hold it
    r = world.gc.get("/api/search/text", params={"q": SECRET_WORD})
    assert r.status_code == 200 and r.json()["results"] == [] and SECRET_WORD not in r.text
    r = world.gc.get("/api/search/text", params={"q": "kids sentence"})
    assert [x["title"] for x in r.json()["results"]] == ["Kids Book"]


def test_opds_for_guests_shows_only_granted_shelves_and_no_personal_lists(world):
    pw = api_post(world.gc, "/api/devices/app-passwords", world.gcsrf, {"label": "Reader"})
    assert pw.status_code == 201, pw.text
    auth = _basic("cousin", pw.json()["secret"])
    gc = TestClient(world.gc.app)

    def feed(path: str):
        r = gc.get(path, headers=auth)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
        return etree.fromstring(r.content)

    ns = {"a": "http://www.w3.org/2005/Atom"}
    titles = lambda path: sorted(  # noqa: E731
        t.text for t in feed(path).xpath("//a:entry/a:title", namespaces=ns)
    )
    assert titles("/opds/all") == ["Kids Book", "Public Book"]
    assert titles("/opds/recent") == ["Kids Book", "Public Book"]
    assert titles("/opds/search?q=book") == ["Kids Book", "Public Book"]
    assert titles("/opds/search?q=Secret") == []
    assert titles("/opds/shelves") == ["Public"]
    assert titles(f"/opds/shelves/{world.public}") == ["Books on Public", "Kids"]
    assert titles("/opds/shelves/none") == []
    assert titles("/opds/next-in-series") == [] and titles("/opds/continue") == []
    assert titles("/opds/up-next") == ["Kids Book", "Public Book"]
    root = titles("/opds")
    assert "Want to read" not in root and "Queued for this device" not in root
    assert gc.get(f"/opds/shelves/{world.secret}", headers=auth).status_code == 404
    assert gc.get(f"/opds/shelves/{world.secret}/books", headers=auth).status_code == 404
    for path in ("/opds/queue", "/opds/want-to-read", "/koplugin/status"):
        r = gc.get(path, headers=auth)
        assert r.status_code == 403 and r.json()["detail"] == "guest_forbidden", path
    wid = world.works["Secret Book"]
    assert gc.get(f"/opds/works/{wid}/cover.jpg", headers=auth).status_code == 404
    assert gc.get(f"/library/works/{wid}/cover.jpg", headers=auth).status_code == 404
    shared = world.gc.get(f"/api/works/{world.works['Public Book']}").json()["files"][0]["id"]
    assert gc.get(f"/opds/files/{shared}/download", headers=auth).status_code == 200
    with session_factory()() as db:
        hidden = db.scalar(select(BookFile.id).where(BookFile.work_id == wid))
    assert gc.get(f"/opds/files/{hidden}/download", headers=auth).status_code == 404
    assert gc.get(f"/library/files/{hidden}/download", headers=auth).status_code == 404
    # Downloads by a guest are in the security log, marked as a guest's.
    rows = world.owner.get("/api/admin/audit").json()["rows"]
    assert any(r["action"] == "download" and r["actor"] == "cousin" and r["guest"] for r in rows)


def test_next_in_series_for_a_guest_never_mentions_hidden_books(world):
    with session_factory()() as db:
        for title, idx in (("Public Book", 1.0), ("Kids Book", 3.0), ("Secret Book", 2.0)):
            w = db.scalar(select(Work).where(Work.title == title))
            w.series, w.series_index = "Saga", idx
        now = utcnow()
        db.add(
            ReadingState(
                user_id=world.uid,
                work_id=world.works["Public Book"],
                fraction=0.5,
                last_read_at=now,
                started_at=now,
            )
        )
        db.commit()
    items = world.gc.get("/api/next-in-series").json()["items"]
    assert [i["title"] for i in items] == ["Kids Book"]  # not the hidden #2 in between
    assert world.gc.get("/api/want-to-read").status_code == 403
    assert api_post(world.gc, "/api/want-to-read", world.gcsrf, {"work_id": 1}).status_code == 403


# --- deny by default -------------------------------------------------------------------------

PUBLIC_ROUTES = {  # reachable without a browser session on purpose (own credentials or none)
    "/healthz", "/setup", "/login", "/login/passkey/options", "/login/passkey/verify", "/logout",
    "/invite/{token}", "/koplugin/bootstrap", "/koplugin/pair", "/koplugin/hello",
    "/users/create", "/users/auth", "/syncs/progress", "/syncs/progress/{document}",
    "/koplugin/annotations", "/api/worker/enroll", "/worker/install.sh", "/app/sw.js",
    "/app/manifest.webmanifest", "/app/shell.html", "/app/pwa/{name}", "/library", "/account",
    "/admin/users", "/admin/audit", "/mcp", "/static",
}  # fmt: skip


def _routes(app):
    def walk(routes, prefix=""):
        for r in routes:
            if hasattr(r, "original_router"):
                yield from walk(r.original_router.routes, prefix + r.include_context.prefix)
            elif getattr(r, "methods", None) and hasattr(r, "path"):
                yield prefix + r.path, sorted(r.methods - {"HEAD", "OPTIONS"})

    return list(walk(app.routes))


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]*\}", lambda m: "x" if ":path" in m.group(0) else "1", path)


def test_every_registered_route_is_closed_to_a_guest_unless_listed(app, world):
    """The inventory behind deny-by-default: a route that is not on the allowlist must answer
    a guest 403 ``guest_forbidden`` (or be one of the credential-less/own-credential routes). A
    new route that skips authentication or the shared dependencies fails here."""
    allowed = guests.SESSION_ROUTES | guests.DOWNLOAD_ROUTES
    seen = set()
    headers = {"origin": ORIGIN, "x-csrf-token": world.gcsrf}
    for path, methods in _routes(app):
        for method in methods:
            seen.add((method, path))
            if (
                path in PUBLIC_ROUTES
                or path.startswith(("/api/worker/", "/koplugin/"))
                or (path.startswith(("/opds", "/library/")) and (method, path) not in allowed)
            ):
                continue
            r = world.gc.request(
                method, _fill(path), headers=headers, json={}, follow_redirects=False
            )
            denied = (
                r.status_code == 403
                and r.headers.get("content-type", "").startswith("application/json")
                and r.json().get("detail") == "guest_forbidden"
            )
            if (method, path) in allowed:
                assert not denied, (method, path)
            else:
                assert denied, (method, path, r.status_code, r.text[:120])
    for key in allowed | guests.BASIC_ROUTES:
        assert key in seen, f"{key} is allowlisted but no such route exists"


def test_download_routes_close_when_the_owner_turns_off_downloads(world):
    fid = world.gc.get(f"/api/works/{world.works['Public Book']}").json()["files"][0]["id"]
    assert world.gc.get(f"/library/files/{fid}/download").status_code == 200
    r = world.owner.patch(
        f"/api/admin/guests/{world.uid}",
        json={"allow_opds": False},
        headers={"origin": ORIGIN, "x-csrf-token": world.csrf},
    )
    assert r.status_code == 200 and r.json()["guest"]["allow_opds"] is False
    for method, path in (
        ("GET", f"/library/files/{fid}/download"),
        ("GET", "/api/devices"),
        ("POST", "/api/devices/app-passwords"),
    ):
        r = world.gc.request(
            method, path, json={"label": "x"} if method == "POST" else None,
            headers={"origin": ORIGIN, "x-csrf-token": world.gcsrf},
        )  # fmt: skip
        assert r.status_code == 403 and r.json()["detail"] == "guest_forbidden", path
    # Reading in the browser still works: the reader streams the file.
    assert world.gc.get(f"/library/works/{world.works['Public Book']}/book.epub").status_code == 200


def test_owner_only_and_member_routes_refuse_a_guest_with_the_guest_error(world):
    for method, path in (
        ("GET", "/api/admin/users"),
        ("GET", "/api/admin/guests"),
        ("GET", "/api/invites"),
        ("GET", "/api/annotations"),
        ("GET", "/api/stats"),
        ("GET", "/api/vocabulary"),
        ("GET", "/api/quotes"),
        ("GET", "/api/activity"),
        ("GET", "/api/reviews/finished"),
        ("GET", "/api/ai-keys"),
        ("GET", "/api/devices/dashboard"),
        ("GET", "/api/notifications"),
        ("POST", "/api/books/upload"),
    ):
        r = world.gc.request(
            method, path, headers={"origin": ORIGIN, "x-csrf-token": world.gcsrf}, json={}
        )
        assert r.status_code == 403 and r.json()["detail"] == "guest_forbidden", (method, path)
    for method, path in (
        ("POST", "/api/devices/sync-keys"),
        ("POST", "/api/ai-keys"),
        ("POST", "/api/devices"),
        ("PATCH", f"/api/works/{world.works['Public Book']}"),
        ("POST", f"/api/works/{world.works['Public Book']}/trash"),
        ("POST", "/api/shelves"),
        ("PUT", f"/api/shelves/{world.public}/grants/{world.uid}"),
    ):
        r = world.gc.request(
            method, path, headers={"origin": ORIGIN, "x-csrf-token": world.gcsrf}, json={}
        )
        assert r.status_code == 403 and r.json()["detail"] == "guest_forbidden", (method, path)


def test_sync_keys_ai_keys_and_pairing_are_dead_for_guests(world, settings):
    from tests.conftest import ko_headers

    with session_factory()() as db:
        user = db.get(User, world.uid)
        md5 = hashlib.md5(b"secret", usedforsecurity=False).hexdigest()
        db.add(SyncKey(user_id=user.id, label="k", key_hash=sha256_hex(md5)))
        key, secret = ai_keys.new_key(user, "mcp", ["read"])
        db.add(key)
        db.commit()
    gc = TestClient(world.gc.app)
    r = gc.get("/users/auth", headers=ko_headers("cousin", "secret"))
    assert r.status_code == 401
    r = gc.post(
        "/mcp",
        headers={
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "authorization": f"Bearer {secret}",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r.status_code == 401


# --- expiry and revocation -------------------------------------------------------------------


def _expire(uid: int, when=None) -> None:
    with session_factory()() as db:
        db.get(GuestProfile, uid).expires_at = when or utcnow() - timedelta(seconds=1)
        db.commit()


def test_expiry_ends_sessions_app_passwords_and_sign_in(world):
    pw = api_post(world.gc, "/api/devices/app-passwords", world.gcsrf, {"label": "Reader"}).json()
    opds = TestClient(world.gc.app)
    assert opds.get("/opds/all", headers=_basic("cousin", pw["secret"])).status_code == 200
    assert world.gc.get("/api/me").status_code == 200
    _expire(world.uid)
    assert world.gc.get("/api/me").status_code == 401
    assert world.gc.get("/api/library").status_code == 401
    assert opds.get("/opds/all", headers=_basic("cousin", pw["secret"])).status_code == 401
    fresh = TestClient(world.gc.app)
    r = post_form(fresh, "/login", {"username": "cousin", "password": PASSWORD, "totp": ""},
                  follow_redirects=False)  # fmt: skip
    assert r.status_code == 401
    # The rows get revoked too, the next time the owner looks.
    assert len(world.owner.get("/api/admin/guests").json()["guests"]) == 1
    with session_factory()() as db:
        assert all(
            s.revoked for s in db.scalars(select(Session).where(Session.user_id == world.uid))
        )
        assert all(
            p.revoked_at
            for p in db.scalars(select(AppPassword).where(AppPassword.user_id == world.uid))
        )
    # Extending the end date does not bring the revoked credentials back, but a new sign-in works.
    r = world.owner.patch(
        f"/api/admin/guests/{world.uid}",
        json={"expires_at": (utcnow() + timedelta(days=3)).isoformat()},
        headers={"origin": ORIGIN, "x-csrf-token": world.csrf},
    )
    assert r.status_code == 200 and r.json()["guest"]["expired"] is False
    assert opds.get("/opds/all", headers=_basic("cousin", pw["secret"])).status_code == 401
    assert world.gc.get("/api/me").status_code == 401
    login(fresh, "cousin", PASSWORD)
    assert fresh.get("/api/library").status_code == 200


def test_invite_for_a_guest_whose_end_date_passed_is_dead(world):
    soon = utcnow() + timedelta(seconds=1)
    inv = api_post(
        world.owner, "/api/invites", world.csrf,
        {"role": "reader", "guest": {"shelf_ids": [world.public], "expires_at": soon.isoformat()}},
    ).json()  # fmt: skip
    with session_factory()() as db:
        row = db.get(Invite, inv["id"])
        row.guest = {**row.guest, "expires_at": (utcnow() - timedelta(minutes=1)).isoformat()}
        db.commit()
    assert _accept(TestClient(world.gc.app), inv["token"], "late").status_code == 404


def test_revoke_and_edit_shelves_take_effect_on_the_next_request(world):
    headers = {"origin": ORIGIN, "x-csrf-token": world.csrf}
    # Narrow to the sub-shelf only: the parent's book disappears.
    r = world.owner.patch(
        f"/api/admin/guests/{world.uid}", json={"shelf_ids": [world.kids]}, headers=headers
    )
    assert r.status_code == 200
    assert {w["title"] for w in world.gc.get("/api/library").json()["works"]} == {"Kids Book"}
    assert [s["name"] for s in world.gc.get("/api/shelves").json()["shelves"]] == ["Kids"]
    r = world.owner.patch(
        f"/api/admin/guests/{world.uid}",
        json={"shelf_ids": [world.secret, 424242]},
        headers=headers,
    )
    assert r.status_code == 422
    r = api_post(world.owner, f"/api/admin/guests/{world.uid}/revoke", world.csrf)
    assert r.status_code == 200 and r.json()["guest"]["is_active"] is False
    assert world.gc.get("/api/me").status_code == 401
    with session_factory()() as db:
        assert db.scalars(select(ShelfGrant).where(ShelfGrant.user_id == world.uid)).all() == []
    assert world.owner.get("/api/admin/guests/1").status_code in (404, 405)
    assert api_post(world.owner, "/api/admin/guests/99999/revoke", world.csrf).status_code == 404


def test_only_the_owner_manages_guests(world, app, client):
    csrf = login(client, "owner", OWNER_PASSWORD)
    from tests.conftest import make_user

    make_user(client, csrf, "mia", "member")
    mia = TestClient(app)
    login(mia, "mia", "mia-correct horse")
    assert mia.get("/api/admin/guests").status_code == 403
    assert api_post(mia, f"/api/admin/guests/{world.uid}/revoke", _csrf(mia)).status_code == 403
    # A member's own pages know nothing of the guest.
    assert "cousin" not in mia.get("/api/shelves").text
    assert Role.MEMBER.value == "member"


def test_guests_are_refused_on_the_lan_listener(world, app):
    pw = api_post(world.gc, "/api/devices/app-passwords", world.gcsrf, {"label": "R"}).json()
    app.state.settings.lan_port = 8081
    try:
        lan = TestClient(app, base_url="http://testserver:8081")
        r = lan.get("/opds/all", headers=_basic("cousin", pw["secret"]))
        assert r.status_code == 403
        main = TestClient(app, base_url="http://testserver:8080")
        assert main.get("/opds/all", headers=_basic("cousin", pw["secret"])).status_code == 200
    finally:
        app.state.settings.lan_port = 0
