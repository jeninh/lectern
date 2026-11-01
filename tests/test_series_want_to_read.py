"""Series normalisation, Next in series, the Want to read queue, visibility, OPDS, schema."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command
from lxml import etree
from tests.conftest import ORIGIN, api_delete, login, make_app_password, make_user, put_json

from lectern.db import make_engine, session_factory
from lectern.migrate import alembic_config, upgrade_to_head
from lectern.models import (
    ReadingState,
    Shelf,
    ShelfGrant,
    ShelfItem,
    ShelfPermission,
    User,
    Work,
    utcnow,
)
from lectern.series import series_key


def _work(title: str, series: str | None = None, index: float | None = None) -> int:
    with session_factory()() as db:
        w = Work(
            title=title,
            sort_title=title.lower(),
            author="A",
            sort_author="a",
            dir_path=f"A/{title}",
            series=series,
            series_index=index,
        )
        db.add(w)
        db.commit()
        return w.id


def _state(username: str, work_id: int, fraction: float) -> None:
    with session_factory()() as db:
        uid = db.scalar(sa.select(User.id).where(User.username == username))
        now = utcnow()
        db.add(
            ReadingState(
                user_id=uid,
                work_id=work_id,
                fraction=fraction,
                last_read_at=now,
                started_at=now if fraction > 0 else None,
                finished_at=now if fraction >= 0.985 else None,
            )
        )
        db.commit()


def _titles(client, path: str) -> list[str]:
    r = client.get(path)
    assert r.status_code == 200, r.text
    return [w["title"] for w in r.json()["items"]]


def test_series_key_groups_case_accents_punctuation_the_and_series_suffix():
    names = ["The Expanse", "expanse", "Éxpanse", "Expanse Series", "Expanse, The", "THE EXPANSE!"]
    assert len({series_key(n) for n in names}) == 1
    assert series_key("The Expanse") != series_key("Expanse Origins")
    assert series_key("The") == "the" and series_key(None) == ""


def test_next_in_series_is_automatic_ordered_and_skips_unstarted_series(client, owner):
    s1 = _work("Book 1", "The Stormlight Archive", 1)
    s3 = _work("Book 3", "Stormlight Archive Series", 3)
    s2 = _work("Book 2", "stormlight archive", 2)
    _work("Standalone")
    o1 = _work("Other 1", "Other", 1)
    _work("Other 2", "Other", 2)
    assert _titles(client, "/api/next-in-series") == []
    _state("owner", o1, 0.0)  # opened but not started: the series is not started
    assert _titles(client, "/api/next-in-series") == []
    _state("owner", s1, 1.0)
    assert _titles(client, "/api/next-in-series") == ["Book 2"]
    _state("owner", s2, 0.4)  # reading #2 counts as started; the next unread is #3
    assert _titles(client, "/api/next-in-series") == ["Book 3"]
    _state("owner", s3, 0.1)
    assert _titles(client, "/api/next-in-series") == []
    # never auto-populates the manual queue
    assert _titles(client, "/api/want-to-read") == []


def test_next_in_series_never_reveals_hidden_books(client, owner):
    one, two, three = (_work(f"Vol {i}", "Hidden Saga", i) for i in (1, 2, 3))
    with session_factory()() as db:
        reader = make_user(client, owner["csrf"], "reader", "reader")
        shelf = Shelf(owner_id=1, name="Shared")
        db.add(shelf)
        db.flush()
        db.add_all(
            [
                ShelfItem(shelf_id=shelf.id, work_id=w, position=i)
                for i, w in enumerate([one, three])
            ]
        )
        db.add(ShelfGrant(shelf_id=shelf.id, user_id=reader["id"], permission=ShelfPermission.VIEW))
        db.commit()
    csrf = login(client, "reader", reader["password"])
    _state("reader", one, 1.0)
    assert _titles(client, "/api/next-in-series") == ["Vol 3"]  # #2 is hidden: no gap, no hint
    # a hidden-only series never starts for them either
    assert (
        client.post(
            "/api/want-to-read",
            json={"work_id": two},
            headers={"origin": ORIGIN, "x-csrf-token": csrf},
        ).status_code
        == 404
    )


def test_want_to_read_add_dedupe_reorder_remove_and_privacy(client, owner):
    a, b, c = (_work(t) for t in "ABC")
    csrf = owner["csrf"]
    hdr = {"origin": ORIGIN, "x-csrf-token": csrf}
    for w in (a, b, c, a):  # re-adding keeps the place
        assert client.post("/api/want-to-read", json={"work_id": w}, headers=hdr).status_code == 200
    assert _titles(client, "/api/want-to-read") == ["A", "B", "C"]
    r = put_json(client, "/api/want-to-read/order", csrf, {"work_ids": [c, a, b]})
    assert [w["title"] for w in r.json()["items"]] == ["C", "A", "B"]
    assert (
        put_json(client, "/api/want-to-read/order", csrf, {"work_ids": [c, a]}).status_code == 409
    )
    assert (
        put_json(client, "/api/want-to-read/order", csrf, {"work_ids": [c, a, b, 99]}).status_code
        == 409
    )
    assert client.post("/api/want-to-read", json={"work_id": 999}, headers=hdr).status_code == 404
    r = api_delete(client, f"/api/want-to-read/{a}", csrf)
    assert [w["title"] for w in r.json()["items"]] == ["C", "B"]
    make_user(client, csrf, "other", "member")
    login(client, "other", "other-correct horse")
    assert _titles(client, "/api/want-to-read") == []


def test_opds_feeds_for_both_surfaces(client, owner):
    s1 = _work("Vol 1", "Saga", 1)
    _work("Vol 2", "Saga", 2)
    w = _work("Queued")
    _state("owner", s1, 1.0)
    client.post(
        "/api/want-to-read",
        json={"work_id": w},
        headers={"origin": ORIGIN, "x-csrf-token": owner["csrf"]},
    )
    pw = make_app_password(client, owner["csrf"], "Kindle")
    auth = {"Authorization": pw} if pw.startswith("Basic") else None

    def titles(path: str) -> list[str]:
        r = client.get(path, auth=("owner", pw)) if auth is None else client.get(path, headers=auth)
        assert r.status_code == 200, r.text
        root = etree.fromstring(r.content)
        return root.xpath(
            "//a:entry/a:title/text()", namespaces={"a": "http://www.w3.org/2005/Atom"}
        )

    assert titles("/opds/next-in-series") == ["Vol 2"]
    assert titles("/opds/want-to-read") == ["Queued"]
    assert titles("/opds")[1:4] == ["Up next", "Next in series", "Want to read"]


def test_migration_chain_and_round_trip(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'w.db'}")
    upgrade_to_head(engine)
    cfg = alembic_config(str(engine.url))
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == ["0047_library_perf_indexes"]
    assert script.get_revision("0045_guest_accounts").down_revision == "0040_series_want_to_read"
    assert script.get_revision("0040_series_want_to_read").down_revision == "0044_listening_queue"
    assert "want_to_read_items" in sa.inspect(engine).get_table_names()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0044_listening_queue")
    assert "want_to_read_items" not in sa.inspect(engine).get_table_names()
