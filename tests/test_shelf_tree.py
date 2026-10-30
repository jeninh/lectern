"""Nested shelves: the Library as folders (lectern.access, routes.api.shelves).

Expected trees, counts, and memberships come from how each library is built here, never from
the code under test.
"""

from __future__ import annotations

import base64
import re

import pytest
import sqlalchemy as sa
from alembic import command
from tests.conftest import (
    ORIGIN,
    OWNER_PASSWORD,
    api_delete,
    api_post,
    drain_inbox,
    login,
    make_app_password,
    make_epub,
    make_user,
    put_json,
)

from lectern.db import make_engine, session_factory
from lectern.migrate import alembic_config, upgrade_to_head
from lectern.models import Role, ShelfAutoChild, User, Work

NAV = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ_KIND = "application/atom+xml;profile=opds-catalog;kind=acquisition"
TITLES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]


def patch(client, url: str, csrf: str, json: dict):
    return client.patch(url, json=json, headers={"origin": ORIGIN, "x-csrf-token": csrf})


@pytest.fixture
def lib(client, owner, settings):
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    for i, t in enumerate(TITLES):
        make_epub(settings.inbox_path / f"{t}.epub", title=t, authors=[f"A {t}"], padding=i * 9)
    drain_inbox(settings)
    ids = {w["title"]: w["id"] for w in client.get("/api/library").json()["works"]}
    assert sorted(ids) == TITLES
    return {"ids": ids, "csrf": owner["csrf"]}


def mk(client, csrf, name, parent=None, status=201):
    r = api_post(client, "/api/shelves", csrf, {"name": name, "parent_id": parent})
    assert r.status_code == status, r.text
    return r.json().get("id") if status == 201 else r.json()


def put_on(client, csrf, shelf, *work_ids):
    for w in work_ids:
        assert api_post(client, f"/api/shelves/{shelf}/items", csrf, {"work_id": w}).is_success


def tree(client) -> dict[str, dict]:
    return {"/".join(s["path"]): s for s in client.get("/api/shelves").json()["shelves"]}


def test_nesting_names_cycles_and_depth(client, lib):
    csrf = lib["csrf"]
    a = mk(client, csrf, "A")
    b = mk(client, csrf, "B", a)
    c = mk(client, csrf, "C", b)
    # Siblings need distinct names; the same name elsewhere in the tree is fine.
    assert mk(client, csrf, "B", a, status=409)["detail"] == "duplicate_name"
    other = mk(client, csrf, "B")
    assert mk(client, csrf, "x/y", status=422)
    # Listed depth-first, children in name order, each with its path.
    assert list(tree(client)) == ["A", "A/B", "A/B/C", "B"]
    assert tree(client)["A/B/C"]["parent_id"] == b

    # A shelf never moves into itself or below itself.
    for target in (a, b, c):
        r = patch(client, f"/api/shelves/{a}", csrf, {"parent_id": target})
        assert r.status_code == 409 and r.json()["detail"] == "shelf_cycle", (target, r.text)
    # Renaming onto a sibling's name is refused; moving next to a same-named shelf too.
    assert patch(client, f"/api/shelves/{other}", csrf, {"name": "A"}).status_code == 409
    assert patch(client, f"/api/shelves/{other}", csrf, {"parent_id": a}).status_code == 409
    r = patch(client, f"/api/shelves/{c}", csrf, {"parent_id": None, "name": "C2"})
    assert r.status_code == 200 and r.json()["parent_id"] is None and r.json()["path"] == ["C2"]

    # Depth: a top-level shelf is depth 1; 8 levels fit, a 9th does not.
    chain = [mk(client, csrf, "L1")]
    for depth in range(2, 9):
        chain.append(mk(client, csrf, f"L{depth}", chain[-1]))
    assert mk(client, csrf, "L9", chain[-1], status=409)["detail"] == "too_deep"
    # Moving a two-level subtree (A/B) under depth 7 would make depth 9; under depth 6, 8.
    r = patch(client, f"/api/shelves/{a}", csrf, {"parent_id": chain[6]})
    assert r.status_code == 409 and r.json()["detail"] == "too_deep"
    r = patch(client, f"/api/shelves/{a}", csrf, {"parent_id": chain[5]})
    assert r.status_code == 200, r.text
    assert tree(client)["L1/L2/L3/L4/L5/L6/A/B"]["id"] == b


def test_grants_cover_subtrees_without_naming_what_is_hidden(client, lib, settings):
    """Family/{Kids/{Picture}, Adults}; rex (reader) is granted Kids. Rex sees Kids as a
    top-level shelf, Picture inside it, their books, and nothing about Family or Adults."""
    csrf, ids = lib["csrf"], lib["ids"]
    rex = make_user(client, csrf, "rex", "reader")
    mia = make_user(client, csrf, "mia", "member")
    family = mk(client, csrf, "Family")
    kids = mk(client, csrf, "Kids", family)
    picture = mk(client, csrf, "Picture", kids)
    adults = mk(client, csrf, "Adults", family)
    put_on(client, csrf, family, ids["Alpha"])
    put_on(client, csrf, kids, ids["Bravo"])
    put_on(client, csrf, picture, ids["Charlie"], ids["Delta"])
    put_on(client, csrf, adults, ids["Delta"], ids["Echo"])
    r = put_json(client, f"/api/shelves/{kids}/grants/{rex['id']}", csrf, {"permission": "view"})
    assert r.status_code == 200
    pw = make_app_password(client, csrf, "Kobo")  # the owner's; rex gets his own below

    login(client, "rex", rex["password"])
    seen = tree(client)
    assert list(seen) == ["Kids", "Kids/Picture"]
    assert seen["Kids"]["parent_id"] is None  # Family exists, but not for rex
    assert (seen["Kids"]["work_count"], seen["Kids"]["shelf_count"]) == (1, 1)
    assert seen["Kids/Picture"]["work_count"] == 2
    detail = client.get(f"/api/shelves/{picture}").json()
    assert detail["ancestors"] == [{"id": kids, "name": "Kids"}]
    assert [w["title"] for w in detail["works"]] == ["Charlie", "Delta"]
    assert client.get(f"/api/shelves/{kids}").json()["ancestors"] == []
    for hidden in (family, adults):
        assert client.get(f"/api/shelves/{hidden}").status_code == 404
        assert client.get(f"/api/library?shelf={hidden}").status_code == 404
        assert client.get(f"/api/library/count?shelf={hidden}").status_code == 404
    works = {w["title"]: w for w in client.get("/api/library").json()["works"]}
    assert sorted(works) == ["Bravo", "Charlie", "Delta"]  # inherited down, not up/sideways
    assert works["Delta"]["shelf_ids"] == [picture]  # its Adults membership stays hidden
    assert client.get("/api/library/count?unshelved=true").json()["total"] == 0
    rex_csrf = client.get("/api/me").json()["csrf_token"]
    assert mk(client, rex_csrf, "Mine", kids, status=403)
    names = repr(client.get("/api/shelves").json()) + repr(client.get("/api/library").json())
    assert "Family" not in names and "Adults" not in names

    # OPDS: the same tree, from rex's top level.
    rex_pw = api_post(client, "/api/devices/app-passwords", rex_csrf, {"label": "Kindle"})
    basic = base64.b64encode(f"rex:{rex_pw.json()['secret']}".encode()).decode()
    opds = {"authorization": f"Basic {basic}"}
    root = client.get("/opds/shelves", headers=opds).text
    assert ">Kids<" in root and "Family" not in root and "Not on a shelf" not in root
    assert 'href="/opds/shelves/' + str(kids) + '"' in root  # has sub-shelves: navigation
    nav = client.get(f"/opds/shelves/{kids}", headers=opds).text
    assert f'href="/opds/shelves/{picture}/books"' in nav  # a leaf opens on its books
    assert "Books on Kids" in nav and 'rel="up" href="/opds/shelves"' in nav
    books = client.get(f"/opds/shelves/{picture}/books", headers=opds).text
    assert "<title>Kids / Picture</title>" in books and "Family" not in books
    assert books.count("<entry>") == 2 and "Echo" not in books
    assert client.get(f"/opds/shelves/{family}", headers=opds).status_code == 404
    assert client.get(f"/opds/shelves/{adults}/books", headers=opds).status_code == 404

    # A member with edit on Kids may make a sub-shelf there, but not manage Kids, and does
    # not see Family at all.
    login(client, "owner", OWNER_PASSWORD)
    csrf = client.get("/api/me").json()["csrf_token"]
    put_json(client, f"/api/shelves/{kids}/grants/{mia['id']}", csrf, {"permission": "edit"})
    login(client, "mia", mia["password"])
    mia_csrf = client.get("/api/me").json()["csrf_token"]
    assert list(tree(client)) == ["Kids", "Kids/Picture"]
    made = mk(client, mia_csrf, "Comics", kids)
    assert tree(client)["Kids/Comics"]["permission"] == "owner"
    assert patch(client, f"/api/shelves/{kids}", mia_csrf, {"name": "Mine"}).status_code == 403
    assert mk(client, mia_csrf, "Sneaky", family, status=404)
    # Moving her shelf to the top level is fine; into a hidden shelf is 404.
    assert patch(client, f"/api/shelves/{made}", mia_csrf, {"parent_id": adults}).status_code == 404
    # Her own top-level shelf is invisible to rex until it sits inside Kids.
    top = mk(client, mia_csrf, "Mia's")
    login(client, "rex", rex["password"])
    assert "Mia's" not in tree(client)
    mia_csrf = login(client, "mia", mia["password"])
    assert patch(client, f"/api/shelves/{top}", mia_csrf, {"parent_id": kids}).status_code == 200
    login(client, "rex", rex["password"])
    assert "Kids/Mia's" in tree(client)

    # Revoking the grant on Kids hides the whole subtree again.
    login(client, "owner", OWNER_PASSWORD)
    csrf = client.get("/api/me").json()["csrf_token"]
    assert api_delete(client, f"/api/shelves/{kids}/grants/{rex['id']}", csrf).status_code == 204
    login(client, "rex", rex["password"])
    assert tree(client) == {} and client.get("/api/library").json()["works"] == []
    assert pw


def test_moving_books_keeps_other_memberships_and_root_means_no_shelf(client, lib):
    csrf, ids = lib["csrf"], lib["ids"]
    a, b, c = mk(client, csrf, "A"), mk(client, csrf, "B"), mk(client, csrf, "C")
    put_on(client, csrf, a, ids["Alpha"], ids["Bravo"])
    put_on(client, csrf, c, ids["Alpha"])

    def shelves_of() -> dict[str, list[int]]:
        return {
            w["title"]: w["shelf_ids"]
            for w in client.get("/api/library").json()["works"]
            if w["title"] in ("Alpha", "Bravo", "Charlie")
        }

    def bulk(**body):
        r = api_post(client, "/api/works/bulk", csrf, {"action": "move_to_shelf", **body})
        assert r.status_code == 200, r.text
        assert all(x["ok"] for x in r.json()["results"]), r.json()
        return r.json()

    # A → B: off A, onto B, and Alpha stays on C (a book can be on several shelves).
    moved = bulk(work_ids=[ids["Alpha"], ids["Bravo"]], from_shelf_id=a, to_shelf_id=b)
    assert shelves_of() == {"Alpha": sorted([b, c]), "Bravo": [b], "Charlie": []}
    # From the root onto B: nothing to leave.
    bulk(work_ids=[ids["Charlie"]], from_shelf_id=None, to_shelf_id=b)
    assert shelves_of()["Charlie"] == [b]
    # To the root: off every shelf, so it shows at the top of the Library.
    unshelved = client.get("/api/library?unshelved=true").json()["works"]
    assert "Alpha" not in {w["title"] for w in unshelved}
    bulk(work_ids=[ids["Alpha"]], from_shelf_id=b, to_shelf_id=None)
    assert shelves_of()["Alpha"] == []
    unshelved = {w["title"] for w in client.get("/api/library?unshelved=true").json()["works"]}
    assert unshelved == {"Alpha", "Delta", "Echo", "Foxtrot"}
    assert client.get("/api/library/count?unshelved=true").json()["total"] == 4
    # Undo of the first move puts both back where they were.
    r = api_post(client, f"/api/works/bulk/{moved['batch_id']}/undo", csrf)
    assert r.status_code == 200, r.text
    assert any(not x["ok"] for x in r.json()["results"])  # Alpha changed since: refused
    assert shelves_of()["Bravo"] == [a]


def test_deleting_a_shelf_with_things_in_it(client, lib):
    csrf, ids = lib["csrf"], lib["ids"]
    top = mk(client, csrf, "Top")
    mid = mk(client, csrf, "Mid", top)
    leaf = mk(client, csrf, "Leaf", mid)
    clash = mk(client, csrf, "Leaf", top)  # Top already has a "Leaf"
    put_on(client, csrf, top, ids["Alpha"])
    put_on(client, csrf, mid, ids["Bravo"], ids["Alpha"])
    put_on(client, csrf, leaf, ids["Charlie"])

    r = api_delete(client, f"/api/shelves/{mid}", csrf)
    assert r.status_code == 409 and r.json()["detail"] == "shelf_not_empty"
    assert (r.json()["sub_shelves"], r.json()["direct_works"], r.json()["works"]) == (1, 2, 3)

    # Move up: Mid's sub-shelf and works land in Top; the clashing name gets a suffix.
    assert api_delete(client, f"/api/shelves/{mid}?contents=move_up", csrf).status_code == 204
    seen = tree(client)
    assert sorted(seen) == ["Top", "Top/Leaf", "Top/Leaf (2)"]
    assert seen["Top/Leaf (2)"]["id"] == leaf and seen["Top/Leaf"]["id"] == clash
    on_top = [w["title"] for w in client.get(f"/api/shelves/{top}").json()["works"]]
    assert on_top == ["Alpha", "Bravo"]  # Alpha was on both: once

    # Trash: everything in or below goes to the Trash (one Undo batch), the shelves go away.
    r = api_delete(client, f"/api/shelves/{top}?contents=trash", csrf)
    assert r.status_code == 200 and r.json()["deleted"], r.text
    assert sorted(x["work_id"] for x in r.json()["results"]) == sorted(
        ids[t] for t in ("Alpha", "Bravo", "Charlie")
    )
    assert tree(client) == {}
    left = {w["title"] for w in client.get("/api/library").json()["works"]}
    assert left == {"Delta", "Echo", "Foxtrot"}
    trashed = {t["work"]["title"] for t in client.get("/api/trash").json()["works"]}
    assert {"Alpha", "Bravo", "Charlie"} <= trashed
    r = api_post(client, f"/api/works/bulk/{r.json()['batch_id']}/undo", csrf)
    assert r.status_code == 200 and all(x["ok"] for x in r.json()["results"])
    assert len(client.get("/api/library").json()["works"]) == 6

    # An empty shelf just goes; a top-level one with works: they leave it (root).
    empty = mk(client, csrf, "Empty")
    assert api_delete(client, f"/api/shelves/{empty}", csrf).status_code == 204
    solo = mk(client, csrf, "Solo")
    inner = mk(client, csrf, "Inner", solo)
    put_on(client, csrf, solo, ids["Delta"])
    put_on(client, csrf, inner, ids["Echo"])
    assert api_delete(client, f"/api/shelves/{solo}?contents=move_up", csrf).status_code == 204
    delta = next(w for w in client.get("/api/library").json()["works"] if w["title"] == "Delta")
    assert delta["shelf_ids"] == []
    # Its sub-shelf is now top-level, with its book still on it.
    assert tree(client)["Inner"]["id"] == inner and tree(client)["Inner"]["work_count"] == 1


def insert_as_of(conn, model, **values) -> None:
    """Insert a row into the table as the database has it now (an older revision): the given
    values, and the model's Python defaults for the other columns that exist."""
    table = sa.Table(model.__tablename__, sa.MetaData(), autoload_with=conn)
    row = {}
    for col in table.columns:
        current = model.__table__.c.get(col.name)
        if col.name in values:
            row[col.name] = values[col.name]
        elif current is not None and current.default is not None:
            arg = current.default.arg
            row[col.name] = arg(None) if callable(arg) else arg
    conn.execute(table.insert().values(**row))


def test_existing_shelves_become_top_level_and_downgrade_keeps_them(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    cfg = alembic_config(str(engine.url))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0013")
    with engine.begin() as conn:  # rows as of 0013: later migrations add columns to both
        for uid, name in ((1, "owner"), (2, "mia")):
            insert_as_of(conn, User, id=uid, username=name, role=Role.OWNER.name,
                         webauthn_user_handle=b"h" * 32)  # fmt: skip
        insert_as_of(conn, Work, id=1, title="T", sort_title="t", author="A", sort_author="a",
                     dir_path="t")  # fmt: skip
    with engine.begin() as conn:
        for sid, owner, name in ((1, 1, "Kids"), (2, 1, "Classics"), (3, 2, "Kids")):
            conn.execute(
                sa.text(
                    "INSERT INTO shelves (id, owner_id, name, created_at, updated_at) "
                    "VALUES (:i, :o, :n, '2026-01-01', '2026-01-01')"
                ),
                {"i": sid, "o": owner, "n": name},
            )
        conn.execute(
            sa.text(
                "INSERT INTO shelf_items (shelf_id, work_id, position, added_at) "
                "VALUES (1, 1, 0, '2026-01-01')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO shelf_grants (shelf_id, user_id, permission, created_at) "
                "VALUES (1, 2, 'view', '2026-01-01')"
            )
        )

    def children():
        with engine.connect() as conn:
            items = conn.execute(sa.text("SELECT shelf_id, work_id FROM shelf_items")).all()
            grants = conn.execute(sa.text("SELECT shelf_id, user_id FROM shelf_grants")).all()
        items, grants = sorted(items), sorted(grants)
        return items, grants

    # Rebuilding shelves must not cascade away its memberships and grants. upgrade_to_head
    # migrates on an engine with foreign_keys=ON, exactly as the app does at startup.
    upgrade_to_head(engine)
    assert children() == ([(1, 1)], [(1, 2)])
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT id, owner_id, name, parent_id FROM shelves")).all()
    assert sorted(rows) == [(1, 1, "Kids", None), (2, 1, "Classics", None), (3, 2, "Kids", None)]
    with engine.begin() as conn:
        # Nest, with a name the old per-owner uniqueness would refuse.
        conn.execute(
            sa.text(
                "INSERT INTO shelves (id, owner_id, name, parent_id, created_at, updated_at) "
                "VALUES (4, 1, 'Kids', 2, '2026-01-01', '2026-01-01')"
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            with conn.begin_nested():
                conn.execute(
                    sa.text(
                        "INSERT INTO shelves (owner_id, name, created_at, updated_at) "
                        "VALUES (1, 'Kids', '2026-01-01', '2026-01-01')"
                    )
                )
        # A top-level shelf already named what the downgrade would first rename shelf 4 to,
        # and a book and a grant on the nested shelf, which must survive the downgrade too.
        conn.execute(
            sa.text(
                "INSERT INTO shelves (id, owner_id, name, created_at, updated_at) "
                "VALUES (5, 1, 'Kids (4)', '2026-01-01', '2026-01-01')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO shelf_items (shelf_id, work_id, position, added_at) "
                "VALUES (4, 1, 0, '2026-01-01')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO shelf_grants (shelf_id, user_id, permission, created_at) "
                "VALUES (4, 2, 'edit', '2026-01-01')"
            )
        )
    with engine.begin() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1  # as the app migrates
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0013")
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT id, owner_id, name FROM shelves")).all()
    names = dict((sid, name) for sid, _, name in rows)
    assert names[1] == names[3] == "Kids" and names[2] == "Classics" and names[5] == "Kids (4)"
    assert names[4].startswith("Kids (") and names[4] not in ("Kids", "Kids (4)")
    assert "parent_id" not in {c["name"] for c in sa.inspect(engine).get_columns("shelves")}
    assert children() == ([(1, 1), (4, 1)], [(1, 2), (4, 2)])


def race(client, csrf, monkeypatch, check: str, *requests):
    """Send ``requests`` (``(method, url, json)``) at once, each pausing inside the shelf
    route's ``check`` until all have reached it: without serialization every request then
    validates against the same old tree before any writes. Responses in request order."""
    import threading

    from lectern.routes.api import shelves as shelves_api

    gate = threading.Barrier(len(requests))
    real = getattr(shelves_api, check)

    def after_all_arrive(*args):
        try:
            gate.wait(timeout=1)  # serialized: the first one waits alone, then breaks it
        except threading.BrokenBarrierError:
            pass
        return real(*args)

    monkeypatch.setattr(shelves_api, check, after_all_arrive)
    headers = {"origin": ORIGIN, "x-csrf-token": csrf}
    out: list = [None] * len(requests)

    def send(i, method, url, body):
        out[i] = client.request(method, url, json=body, headers=headers)

    threads = [threading.Thread(target=send, args=(i, *r)) for i, r in enumerate(requests)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    monkeypatch.setattr(shelves_api, check, real)
    return out


def depths(client) -> dict[str, int]:
    return {p: len(s["path"]) for p, s in tree(client).items()}


def test_concurrent_reciprocal_moves_cannot_make_a_cycle(client, lib, monkeypatch):
    """A into B and B into A at the same moment: each is legal against the old tree, so
    exactly one may win and the other must see the cycle."""
    csrf = lib["csrf"]
    a, b = mk(client, csrf, "A"), mk(client, csrf, "B")
    ra, rb = race(
        client, csrf, monkeypatch, "lineage",
        ("PATCH", f"/api/shelves/{a}", {"parent_id": b}),
        ("PATCH", f"/api/shelves/{b}", {"parent_id": a}),
    )  # fmt: skip
    assert sorted([ra.status_code, rb.status_code]) == [200, 409], (ra.text, rb.text)
    assert (ra if ra.status_code == 409 else rb).json()["detail"] == "shelf_cycle"
    assert sorted(tree(client)) in (["A", "A/B"], ["B", "B/A"])  # both reachable from the top


def chain(client, csrf, prefix: str, n: int) -> list[int]:
    ids = [mk(client, csrf, f"{prefix}1")]
    for i in range(2, n + 1):
        ids.append(mk(client, csrf, f"{prefix}{i}", ids[-1]))
    return ids


def test_concurrent_moves_cannot_exceed_the_depth_cap(client, lib, monkeypatch):
    """Chains X, Y, Z, each 4 deep. Y under X4 (4 + 4) and Z under Y4 (4 + 4) are each legal
    alone; together they would nest 12 deep, so the second must be refused."""
    csrf = lib["csrf"]
    x, y, z = (chain(client, csrf, p, 4) for p in "XYZ")
    ry, rz = race(
        client, csrf, monkeypatch, "depth_of",
        ("PATCH", f"/api/shelves/{y[0]}", {"parent_id": x[3]}),
        ("PATCH", f"/api/shelves/{z[0]}", {"parent_id": y[3]}),
    )  # fmt: skip
    assert sorted([ry.status_code, rz.status_code]) == [200, 409], (ry.text, rz.text)
    assert (ry if ry.status_code == 409 else rz).json()["detail"] == "too_deep"
    assert max(depths(client).values()) == 8
    assert len(tree(client)) == 12  # nothing stranded


def test_concurrent_create_and_move_cannot_exceed_the_depth_cap(client, lib, monkeypatch):
    """X is 6 deep; Y/Y2 is 2. Moving Y under X6 (6 + 2) and creating Y2/New (2 + 1) are
    each legal alone; together New would be 9 deep."""
    csrf = lib["csrf"]
    x, y = chain(client, csrf, "X", 6), chain(client, csrf, "Y", 2)
    rm, rc = race(
        client, csrf, monkeypatch, "depth_of",
        ("PATCH", f"/api/shelves/{y[0]}", {"parent_id": x[5]}),
        ("POST", "/api/shelves", {"name": "New", "parent_id": y[1]}),
    )  # fmt: skip
    assert sorted([rm.status_code, rc.status_code]) in ([200, 409], [201, 409]), (rm.text, rc.text)
    assert (rm if rm.status_code == 409 else rc).json()["detail"] == "too_deep"
    # Whichever ran first wins: Y moved under X6 (8 deep, no New), or New made in Y2 and the
    # move refused (X stays 6 deep).
    if rm.status_code == 200:
        assert max(depths(client).values()) == 8 and "New" not in str(list(tree(client)))
    else:
        assert depths(client)["Y1/Y2/New"] == 3 and max(depths(client).values()) == 6


def test_moving_someone_elses_sub_shelf_out_of_view_is_204(client, lib):
    """Mia owns Family; Max (edit there) made Family/Sub. Mia manages Sub through Family, so
    she may move it to the top level, after which it is Max's alone and she cannot see it."""
    csrf = lib["csrf"]
    mia = make_user(client, csrf, "mia", "member")
    max_ = make_user(client, csrf, "max", "member")
    mia_csrf = login(client, "mia", mia["password"])
    family = mk(client, mia_csrf, "Family")
    r = put_json(
        client, f"/api/shelves/{family}/grants/{max_['id']}", mia_csrf, {"permission": "edit"}
    )
    assert r.status_code == 200, r.text
    max_csrf = login(client, "max", max_["password"])
    sub = mk(client, max_csrf, "Sub", family)
    mia_csrf = login(client, "mia", mia["password"])
    assert tree(client)["Family/Sub"]["permission"] == "owner"
    r = patch(client, f"/api/shelves/{sub}", mia_csrf, {"parent_id": None})
    assert r.status_code == 204, r.text
    assert list(tree(client)) == ["Family"]
    login(client, "max", max_["password"])
    assert tree(client)["Sub"]["id"] == sub


def test_move_up_under_a_hidden_parent_goes_to_the_callers_top_level(client, lib):
    """Max made Family/Sub/Inner with an edit grant on Mia's Family, then lost the grant: he
    sees Sub at his top level. Deleting it with move_up must not 403 (that would reveal a
    parent he cannot see); Inner lands at his top level."""
    csrf = lib["csrf"]
    mia = make_user(client, csrf, "mia", "member")
    max_ = make_user(client, csrf, "max", "member")
    mia_csrf = login(client, "mia", mia["password"])
    family = mk(client, mia_csrf, "Family")
    grant = f"/api/shelves/{family}/grants/{max_['id']}"
    assert put_json(client, grant, mia_csrf, {"permission": "edit"}).status_code == 200
    max_csrf = login(client, "max", max_["password"])
    sub = mk(client, max_csrf, "Sub", family)
    inner = mk(client, max_csrf, "Inner", sub)
    mia_csrf = login(client, "mia", mia["password"])
    assert api_delete(client, grant, mia_csrf).status_code == 204
    max_csrf = login(client, "max", max_["password"])
    assert list(tree(client)) == ["Sub", "Sub/Inner"]
    r = api_delete(client, f"/api/shelves/{sub}?contents=move_up", max_csrf)
    assert r.status_code == 204, r.text
    assert tree(client)["Inner"]["id"] == inner


# --- auto-shelves (#73): the API and the guards on generated shelves ------------------------


def make_group(client, csrf, name="Authors", field="author", parent=None, status=201, **auto):
    body = {"name": name, "parent_id": parent, "auto": {"field": field, "min_books": 1, **auto}}
    r = api_post(client, "/api/shelves", csrf, body)
    assert r.status_code == status, r.text
    return r.json()


def add_books(author: str, n: int, *, tags=None) -> None:
    """``n`` more books by ``author``, straight into the database."""
    with session_factory()() as db:
        for i in range(n):
            title = f"{author} {i}"
            db.add(Work(title=title, sort_title=title.lower(), author=author,
                        sort_author=author.lower(), dir_path=title, tags=tags,
                        authors=[{"name": author}]))  # fmt: skip
        db.commit()


def children_of(client, name: str) -> list[str]:
    """Names directly under the shelf at path ``name``, in listing order."""
    t = tree(client)
    return [s["name"] for s in t.values() if s["parent_id"] == t[name]["id"]]


def test_every_manual_edit_of_a_generated_shelf_is_409_auto_shelf(client, lib):
    csrf = lib["csrf"]
    group = make_group(client, csrf)
    t = tree(client)
    bucket, child = t["Authors/A"], t["Authors/A/A Alpha"]
    assert group["auto"] == {
        "kind": "group", "field": "author", "group_id": group["id"], "min_books": 1,
        "child_order": "name", "buckets": True, "sort": "title", "order": None,
    }  # fmt: skip
    assert bucket["auto"] == {"kind": "bucket", "field": "author", "group_id": group["id"]}
    assert child["auto"] == {"kind": "value", "field": "author", "group_id": group["id"]}
    assert child["smart"] and child["work_count"] == 1
    manual = mk(client, csrf, "Manual")
    member = make_user(client, csrf, "mia", "member")
    work = lib["ids"]["Alpha"]

    def refused(r):
        assert r.status_code == 409 and r.json() == {"detail": "auto_shelf"}, r.text

    for sid in (bucket["id"], child["id"]):
        refused(patch(client, f"/api/shelves/{sid}", csrf, {"name": "Mine"}))
        refused(patch(client, f"/api/shelves/{sid}", csrf, {"parent_id": None}))
        refused(patch(client, f"/api/shelves/{sid}", csrf, {"parent_id": manual}))
        refused(api_delete(client, f"/api/shelves/{sid}", csrf))
        refused(put_json(client, f"/api/shelves/{sid}/grants/{member['id']}", csrf,
                         {"permission": "view"}))  # fmt: skip
        refused(api_delete(client, f"/api/shelves/{sid}/grants/{member['id']}", csrf))
        refused(api_post(client, f"/api/shelves/{sid}/items", csrf, {"work_id": work}))
        refused(put_json(client, f"/api/shelves/{sid}/items", csrf, {"work_ids": [work]}))
        refused(api_delete(client, f"/api/shelves/{sid}/items/{work}", csrf))
        refused(api_post(client, "/api/shelves", csrf, {"name": "Sub", "parent_id": sid}))
    # The group is a folder of generated shelves too: nothing is put in it or made in it by
    # hand, and an ordinary shelf does not move into it. It can be renamed and shared.
    gid = group["id"]
    refused(api_post(client, f"/api/shelves/{gid}/items", csrf, {"work_id": work}))
    refused(api_post(client, "/api/shelves", csrf, {"name": "Sub", "parent_id": gid}))
    refused(patch(client, f"/api/shelves/{manual}", csrf, {"parent_id": gid}))
    refused(patch(client, f"/api/shelves/{manual}", csrf, {"parent_id": bucket["id"]}))
    assert (
        patch(client, f"/api/shelves/{gid}", csrf, {"name": "Writers"}).json()["name"] == "Writers"
    )
    r = put_json(client, f"/api/shelves/{gid}/grants/{member['id']}", csrf, {"permission": "view"})
    assert r.status_code == 200


def test_auto_and_filter_are_exclusive_and_settings_are_checked(client, lib):
    csrf = lib["csrf"]
    both = {"name": "X", "filter": "tag:a", "auto": {"field": "author"}}
    r = api_post(client, "/api/shelves", csrf, both)
    assert r.status_code == 422 and r.json()["detail"] == "auto_and_filter"
    for bad in ({"field": "nope"}, {"field": "author", "min_books": 0},
                {"field": "author", "min_books": 51}, {"field": "author", "child_order": "x"},
                {"field": "author", "sort": "nope"}, {"field": "author", "extra": 1}, {}):  # fmt: skip
        r = api_post(client, "/api/shelves", csrf, {"name": "X", "auto": bad})
        assert r.status_code == 422, (bad, r.text)
    assert "X" not in tree(client)
    # Defaults come from the field: authors need two books and sit in A–Z buckets.
    g = make_group(client, csrf, "Defaults", min_books=None)
    assert g["auto"]["min_books"] == 2 and g["auto"]["buckets"] is True


def test_patching_a_group_changes_its_settings_but_never_its_field(client, lib):
    csrf = lib["csrf"]
    add_books("Zed", 3)
    add_books("Yan", 2)
    g = make_group(client, csrf)
    gid = g["id"]
    assert children_of(client, "Authors") == ["A", "Y", "Z"]  # buckets
    r = patch(client, f"/api/shelves/{gid}", csrf, {"auto": {"buckets": False}})
    assert r.status_code == 200 and r.json()["auto"]["buckets"] is False
    assert children_of(client, "Authors") == [
        "A Alpha", "A Bravo", "A Charlie", "A Delta", "A Echo", "A Foxtrot", "Yan", "Zed"
    ]  # fmt: skip
    r = patch(client, f"/api/shelves/{gid}", csrf, {"auto": {"child_order": "count"}})
    assert children_of(client, "Authors")[:3] == ["Zed", "Yan", "A Alpha"]
    r = patch(client, f"/api/shelves/{gid}", csrf,
              {"auto": {"min_books": 2, "sort": "added", "order": "desc"}})  # fmt: skip
    assert r.json()["auto"] | {} == {**g["auto"], "buckets": False, "child_order": "count",
                                    "min_books": 2, "sort": "added", "order": "desc"}  # fmt: skip
    assert children_of(client, "Authors") == ["Zed", "Yan"]  # the rest fell below two books
    kid = tree(client)["Authors/Zed"]
    assert kid["filter"] == "author:=Zed" and kid["sort"] == "added" and kid["order"] == "desc"
    r = patch(client, f"/api/shelves/{gid}", csrf, {"auto": {"order": None}})
    assert r.json()["auto"]["order"] is None
    # field is fixed; naming it again is harmless; an ordinary shelf has no settings.
    r = patch(client, f"/api/shelves/{gid}", csrf, {"auto": {"field": "tag"}})
    assert r.status_code == 409 and r.json()["detail"] == "auto_field_immutable"
    assert (
        patch(client, f"/api/shelves/{gid}", csrf, {"auto": {"field": "author"}}).status_code == 200
    )
    plain = mk(client, csrf, "Plain")
    r = patch(client, f"/api/shelves/{plain}", csrf, {"auto": {"min_books": 3}})
    assert r.status_code == 409 and r.json()["detail"] == "not_auto_group"
    # Settings are for managers: a member granted the group sees the field, not the settings.
    mia = make_user(client, csrf, "mia", "member")
    put_json(client, f"/api/shelves/{gid}/grants/{mia['id']}", csrf, {"permission": "edit"})
    mc = login(client, "mia", mia["password"])
    seen = {s["id"]: s for s in client.get("/api/shelves").json()["shelves"]}[gid]
    assert seen["auto"] == {"kind": "group", "field": "author", "group_id": gid}
    assert patch(client, f"/api/shelves/{gid}", mc, {"auto": {"min_books": 5}}).status_code == 403


def test_deleting_a_group_needs_no_contents_and_takes_its_shelves(client, lib):
    csrf = lib["csrf"]
    g = make_group(client, csrf)
    other = make_group(client, csrf, "Tags", "tag", buckets=False)
    with session_factory()() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(ShelfAutoChild)) > 6
    r = api_delete(client, f"/api/shelves/{g['id']}", csrf)
    assert r.status_code == 204, r.text
    assert sorted(tree(client)) == ["Tags"]  # the other group is untouched
    with session_factory()() as db:
        kids = db.scalars(sa.select(ShelfAutoChild.group_id)).all()
        assert g["id"] not in kids
        assert db.scalar(sa.select(sa.func.count()).select_from(sa.text("shelves"))) == 1
    assert api_delete(client, f"/api/shelves/{other['id']}", csrf).status_code == 204


def test_the_depth_cap_counts_the_bucket_level(client, lib):
    csrf = lib["csrf"]
    ids = chain(client, csrf, "L", 6)  # the last is at depth 6
    # Group at 7 with buckets would put its children at 9; without buckets, at 8.
    r = api_post(client, "/api/shelves", csrf, {"name": "Authors", "parent_id": ids[-1],
                                                "auto": {"field": "author"}})  # fmt: skip
    assert r.status_code == 409 and r.json()["detail"] == "too_deep"
    g = make_group(client, csrf, parent=ids[-1], buckets=False)
    # Buckets cannot be turned on there, nor the group moved or carried deeper than it fits.
    r = patch(client, f"/api/shelves/{g['id']}", csrf, {"auto": {"buckets": True}})
    assert r.status_code == 409 and r.json()["detail"] == "too_deep"
    # A group with no children yet is checked for the children it will get.
    top = make_group(client, csrf, "Top", min_books=50)  # buckets: children at group + 2
    r = patch(client, f"/api/shelves/{top['id']}", csrf, {"parent_id": ids[-1]})  # group at 7
    assert r.status_code == 409 and r.json()["detail"] == "too_deep"
    r = patch(client, f"/api/shelves/{top['id']}", csrf, {"parent_id": ids[-2]})  # group at 6
    assert r.status_code == 200, r.text
    holder = mk(client, csrf, "Holder")
    make_group(client, csrf, "Inside", parent=holder, min_books=50)  # holder + 1
    r = patch(client, f"/api/shelves/{holder}", csrf, {"parent_id": ids[-2]})  # group at 7
    assert r.status_code == 409 and r.json()["detail"] == "too_deep"
    assert patch(client, f"/api/shelves/{holder}", csrf, {"parent_id": ids[-3]}).status_code == 200


def test_genre_map_is_validated_and_owner_only_and_makes_genre_children(client, lib):
    csrf = lib["csrf"]
    add_books("Ann", 1, tags=["Fantasy"])
    add_books("Bob", 1, tags=["Epic", "épic"])
    add_books("Cy", 1, tags=["Poetry"])
    mia = make_user(client, csrf, "mia", "member")
    rex = make_user(client, csrf, "rex", "reader")
    g = make_group(client, csrf, "Genres", "genre")
    assert children_of(client, "Genres") == []  # no map yet
    got = client.get("/api/library-settings/genres").json()
    assert (
        got["genres"] == {}
        and got["can_change"]
        and (got["max_genres"], got["max_tags"]) == (40, 20)
    )
    r = put_json(client, "/api/library-settings/genres", csrf,
                 {"genres": {" Fantasy ": ["Fantasy", " Epic ", "epic", ""]}})  # fmt: skip
    assert r.status_code == 200 and r.json()["genres"] == {"Fantasy": ["Fantasy", "Epic", "epic"]}
    t = tree(client)
    assert list(k for k in t if k.startswith("Genres/")) == ["Genres/Fantasy"]  # Poetry: unmapped
    assert (
        t["Genres/Fantasy"]["filter"] == "tag:fantasy,epic"
        and t["Genres/Fantasy"]["work_count"] == 2
    )
    assert g["auto"]["field"] == "genre"
    # Limits: 40 genres of 20 tags pass, one more of either is 422.
    ok = {f"G{i}": [f"t{j}" for j in range(20)] for i in range(40)}
    assert put_json(client, "/api/library-settings/genres", csrf, {"genres": ok}).status_code == 200
    r = put_json(client, "/api/library-settings/genres", csrf, {"genres": {**ok, "G40": ["x"]}})
    assert r.status_code == 422 and r.json()["detail"] == "too_many_genres"
    r = put_json(
        client,
        "/api/library-settings/genres",
        csrf,
        {"genres": {"A": [f"t{j}" for j in range(21)]}},
    )
    assert r.status_code == 422 and r.json()["detail"] == "too_many_tags"
    for bad in ({"": ["a"]}, {"x" * 129: ["a"]}, {"A": ["y" * 129]}):
        r = put_json(client, "/api/library-settings/genres", csrf, {"genres": bad})
        assert r.status_code == 422 and r.json()["detail"] == "bad_genre", bad
    assert len(client.get("/api/library-settings/genres").json()["genres"]) == 40  # unchanged
    # Owner-only writes; members read; readers do neither.
    mc = login(client, "mia", mia["password"])
    assert put_json(client, "/api/library-settings/genres", mc, {"genres": {}}).status_code == 403
    r = client.get("/api/library-settings/genres").json()
    assert r["can_change"] is False and len(r["genres"]) == 40
    login(client, "rex", rex["password"])
    assert client.get("/api/library-settings/genres").status_code == 403


def test_a_groups_folder_lists_the_works_its_generated_shelves_show(client, lib):
    csrf = lib["csrf"]
    g = make_group(client, csrf)
    by_author = {w["title"]: w["id"] for w in client.get("/api/library").json()["works"]}
    t = tree(client)
    folder = client.get(f"/api/shelves/{g['id']}").json()
    assert [s["name"] for s in folder["shelves"]] == ["A"] and folder["works"] == []
    bucket = client.get(f"/api/shelves/{t['Authors/A']['id']}").json()
    assert [s["name"] for s in bucket["shelves"]] == [f"A {x}" for x in TITLES]

    def listed(shelf, **q):
        r = client.get("/api/library", params={"shelf": shelf, **q})
        assert r.status_code == 200, r.text
        return {w["title"] for w in r.json()["works"]}

    for sid in (g["id"], t["Authors/A"]["id"]):
        assert listed(sid) == set()  # nothing is on the folder itself
        assert listed(sid, subtree="true") == set(TITLES)
        assert listed(sid, subtree="true", f="title:Alpha,Echo") == {"Alpha", "Echo"}
    assert listed(t["Authors/A/A Delta"]["id"]) == {"Delta"}
    assert set(by_author) == set(TITLES)


def count_queries(client, url: str) -> int:
    """SQL statements one GET of ``url`` runs, counted on the engine."""
    engine = session_factory().kw["bind"]
    seen: list[str] = []

    def count(conn, cursor, statement, *rest):
        seen.append(statement)

    sa.event.listen(engine, "before_cursor_execute", count)
    try:
        assert client.get(url).status_code == 200
    finally:
        sa.event.remove(engine, "before_cursor_execute", count)
    return len(seen)


def test_list_shelves_runs_a_constant_number_of_queries(client, lib):
    csrf = lib["csrf"]
    make_group(client, csrf, min_books=2)
    make_group(client, csrf, "Tags", "tag", min_books=2, child_order="count")
    add_books("Small", 2, tags=["t"])
    client.get("/api/shelves")  # the first look generates the shelves
    small = count_queries(client, "/api/shelves")
    n_small = len(client.get("/api/shelves").json()["shelves"])
    with session_factory()() as db:
        for a in range(500):  # 2,000 works by 500 authors, 4 each, 10 tags between them
            for i in range(4):
                title = f"Book {a}-{i}"
                db.add(Work(title=title, sort_title=title.lower(), author=f"Author {a:03}",
                            sort_author=f"author {a:03}", dir_path=title, tags=[f"tag{a % 10}"],
                            authors=[{"name": f"Author {a:03}"}]))  # fmt: skip
        db.commit()
    client.get("/api/shelves")
    big = count_queries(client, "/api/shelves")
    shelves = client.get("/api/shelves").json()["shelves"]
    assert len(shelves) - n_small >= 500  # one shelf per author, and the tags
    assert sum(1 for s in shelves if s["name"].startswith("Author ")) == 500
    print(f"list_shelves queries: {small} with 7 works, {big} with 2,000 works and 500 authors")
    assert big == small


def test_opds_auto_shelves_are_navigation_down_to_acquisition_feeds(client, lib):
    csrf, ids = lib["csrf"], lib["ids"]
    auto = {"field": "author", "min_books": 1}
    group = api_post(client, "/api/shelves", csrf, {"name": "Authors", "auto": auto}).json()["id"]
    tags = {"field": "tag", "buckets": False}
    empty = api_post(client, "/api/shelves", csrf, {"name": "Tags", "auto": tags}).json()["id"]
    mine = mk(client, csrf, "Mine")
    put_on(client, csrf, mine, ids["Alpha"], ids["Bravo"])
    rex = make_user(client, csrf, "rex", "reader")
    for sid in (group, mine):
        r = put_json(client, f"/api/shelves/{sid}/grants/{rex['id']}", csrf, {"permission": "view"})
        assert r.status_code == 200, r.text

    def basic(user: str, pw: str) -> dict:
        return {"authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}

    def subsections(text: str) -> list[tuple[str, str, str]]:
        """(title, href, type) of each entry's link."""
        return re.findall(
            r"<entry><title>(.*?)</title>.*?<link [^>]*href=\"(.*?)\" type=\"(.*?)\"", text
        )

    owner_h = basic("owner", make_app_password(client, csrf, "Kobo"))
    root = client.get("/opds/shelves", headers=owner_h).text
    # A group is a navigation feed with no books of its own, so no "Books on" entry.
    assert f'href="/opds/shelves/{group}" type="{NAV}"' in root
    assert "Auto-generated from author fields · 1 shelf" in root
    nav = client.get(f"/opds/shelves/{group}", headers=owner_h).text
    assert "Books on" not in nav
    [(name, href, kind)] = subsections(nav)
    assert (name, kind) == ("A", NAV) and "Auto-generated · 6 shelves" in nav
    leaves = subsections(client.get(href, headers=owner_h).text)
    assert [n for n, _, _ in leaves] == [f"A {t}" for t in TITLES]
    assert {(h.endswith("/books"), k) for _, h, k in leaves} == {(True, ACQ_KIND)}
    # A group with nothing generated yet is still navigation, with no book entry.
    assert f'href="/opds/shelves/{empty}" type="{NAV}"' in root
    assert "Books on" not in client.get(f"/opds/shelves/{empty}", headers=owner_h).text

    # rex sees only the authors of the two books he may see; the rest are absent and 404.
    login(client, "rex", rex["password"])
    rex_csrf = client.get("/api/me").json()["csrf_token"]
    rex_h = basic("rex", make_app_password(client, rex_csrf, "Kindle"))
    rex_root = client.get("/opds/shelves", headers=rex_h).text
    rex_nav = client.get(f"/opds/shelves/{group}", headers=rex_h).text
    [(_, bucket, _)] = subsections(rex_nav)
    rex_leaves = client.get(bucket, headers=rex_h).text
    assert [n for n, _, _ in subsections(rex_leaves)] == ["A Alpha", "A Bravo"]
    for text in (rex_root, rex_nav, rex_leaves):
        assert not any(f"A {t}" in text for t in ("Charlie", "Delta", "Echo", "Foxtrot"))
    for name, href, _ in leaves:
        want = 200 if name in ("A Alpha", "A Bravo") else 404
        assert client.get(href, headers=rex_h).status_code == want, name
        assert client.get(href.removesuffix("/books"), headers=rex_h).status_code == want, name
