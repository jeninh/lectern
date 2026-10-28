"""Private per-book ratings, notes, and read-throughs (lectern.reviews, issue #84).

The household: the owner, member mia, and reader rex, who sees Alpha and Bravo (the "Shared"
shelf) but not Hidden. Expected dates and sets come from what each test wrote.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic import command
from tests.conftest import OWNER_PASSWORD, api_delete, api_post, make_user, put_json
from tests.test_mcp import MCP, Person, ingest

from lectern.db import make_engine, session_factory
from lectern.migrate import alembic_config, upgrade_to_head
from lectern.models import BookFile, BookReview, ReadingProgress, ReadingState, ReadThrough, User


@pytest.fixture
def household(client, owner, app, settings):
    """Owner, member mia, and reader rex; Alpha and Bravo on "Shared" (granted to rex), and
    Hidden."""
    ids = ingest(settings, "Alpha", "Bravo", "Hidden")
    csrf = owner["csrf"]
    mia = make_user(client, csrf, "mia", "member")
    rex = make_user(client, csrf, "rex", "reader")
    shelf = api_post(client, "/api/shelves", csrf, {"name": "Shared"}).json()["id"]
    for t in ("Alpha", "Bravo"):
        assert api_post(client, f"/api/shelves/{shelf}/items", csrf, {"work_id": ids[t]}).is_success
    r = put_json(client, f"/api/shelves/{shelf}/grants/{rex['id']}", csrf, {"permission": "view"})
    assert r.status_code == 200, r.text
    return {
        "ids": ids,
        "owner": Person(app, "owner", OWNER_PASSWORD),
        "mia": Person(app, "mia", mia["password"]),
        "rex": Person(app, "rex", rex["password"]),
    }


def review(p, work_id: int, **body):
    r = put_json(p.http, f"/api/works/{work_id}/review", p.csrf, body)
    assert r.status_code == 200, r.text
    return r.json()


def found(p, f: str, **params) -> list[int]:
    r = p.http.get("/api/library", params={"f": f, **params})
    assert r.status_code == 200, (f, r.text)
    return [w["id"] for w in r.json()["works"]]


def uid(name: str) -> int:
    with session_factory()() as db:
        return db.scalar(sa.select(User.id).where(User.username == name))


def read_to(name: str, work_id: int, pct: float) -> None:
    """A KOReader position for ``name`` on the work's EPUB (the reading-state hook runs)."""
    with session_factory()() as db:
        doc = db.scalar(sa.select(BookFile.partial_md5).where(BookFile.work_id == work_id))
        row = db.scalar(
            sa.select(ReadingProgress).where(
                ReadingProgress.user_id == uid(name), ReadingProgress.document == doc
            )
        )
        if row is None:
            db.add(ReadingProgress(user_id=uid(name), document=doc, progress="x",
                                   percentage=pct, timestamp=1))  # fmt: skip
        else:
            row.percentage = pct
        db.commit()


def set_state(name: str, work_id: int, started: str | None, finished: str | None) -> None:
    """Pin the derived dates (what reading_state recorded), so expectations are fixed days."""
    d = lambda s: s and datetime.fromisoformat(s).replace(tzinfo=UTC)  # noqa: E731
    with session_factory()() as db:
        s = db.get(ReadingState, (uid(name), work_id))
        s.started_at, s.finished_at = d(started), d(finished)
        db.commit()


def test_rating_notes_and_dates_round_trip(household):
    owner, alpha = household["owner"], household["ids"]["Alpha"]
    empty = owner.http.get(f"/api/works/{alpha}/review")
    assert empty.headers["cache-control"] == "no-store"
    assert empty.json() | {"updated_at": None} == {
        "work_id": alpha, "rating": None, "notes": "", "read_throughs": [], "corrected": False,
        "finished": False, "dismissed": False, "prompt": False, "updated_at": None,
    }  # fmt: skip
    # Finishing it in KOReader gives a derived read-through and offers "Rate it?".
    read_to("owner", alpha, 0.3)
    read_to("owner", alpha, 1.0)
    set_state("owner", alpha, "2026-01-05 21:00", "2026-02-01 23:30")
    got = owner.http.get(f"/api/works/{alpha}/review").json()
    assert got["read_throughs"] == [
        {"id": None, "started_on": "2026-01-05", "finished_on": "2026-02-01", "derived": True}
    ]
    assert got["finished"] and got["prompt"] and not got["corrected"]
    # Only what is sent changes.
    got = review(owner, alpha, rating=4.5, notes="# Loved it\n\n*Slow* start.")
    assert (got["rating"], got["prompt"]) == (4.5, False)
    got = review(owner, alpha, read_throughs=[
        {"started_on": "2025-12-30", "finished_on": "2026-02-03"},
        {"started_on": "2019-06-01", "finished_on": "2019-06-20"},
    ])  # fmt: skip
    assert got["rating"] == 4.5 and got["notes"].startswith("# Loved it")
    assert [(r["started_on"], r["finished_on"], r["derived"]) for r in got["read_throughs"]] == [
        ("2019-06-01", "2019-06-20", False),
        ("2025-12-30", "2026-02-03", False),
    ]
    assert got["corrected"]
    assert review(owner, alpha, rating=None)["rating"] is None
    # An empty list goes back to the derived dates; DELETE forgets everything.
    assert review(owner, alpha, read_throughs=[])["read_throughs"][0]["derived"] is True
    r = api_delete(owner.http, f"/api/works/{alpha}/review", owner.csrf)
    assert r.json()["notes"] == "" and r.json()["read_throughs"][0]["derived"] is True


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"rating": 4.3}, "bad_rating"),
        ({"rating": 0}, "bad_rating"),
        ({"rating": 5.5}, "bad_rating"),
        (
            {"read_throughs": [{"started_on": "2026-03-02", "finished_on": "2026-03-01"}]},
            "bad_order",
        ),  # fmt: skip
        ({"read_throughs": [{}]}, "empty_read"),
        ({"read_throughs": [{"finished_on": "2999-01-01"}]}, "future_date"),
        ({"read_throughs": [{"finished_on": "2026-02-30"}]}, "bad_date"),
    ],
)
def test_bad_input_is_refused_and_changes_nothing(household, body, code):
    owner, alpha = household["owner"], household["ids"]["Alpha"]
    review(owner, alpha, rating=3, read_throughs=[{"finished_on": "2024-05-01"}])
    r = put_json(owner.http, f"/api/works/{alpha}/review", owner.csrf, {**body, "notes": "x"})
    assert r.status_code == 422 and r.json()["detail"] == code, r.text
    got = owner.http.get(f"/api/works/{alpha}/review").json()
    assert (got["rating"], got["notes"], got["read_throughs"][0]["finished_on"]) == (
        3, "", "2024-05-01",
    )  # fmt: skip


def test_one_persons_review_is_never_anothers(household):
    owner, mia, rex = household["owner"], household["mia"], household["rex"]
    alpha, hidden = household["ids"]["Alpha"], household["ids"]["Hidden"]
    review(owner, alpha, rating=5, notes="owner's secret",
           read_throughs=[{"finished_on": "2025-03-01"}])  # fmt: skip
    review(owner, hidden, rating=1)
    for p in (mia, rex):
        got = p.http.get(f"/api/works/{alpha}/review").json()
        assert (got["rating"], got["notes"], got["read_throughs"]) == (None, "", [])
        assert found(p, "rating:5") == []
        assert found(p, "finished:2025") == []
        assert p.http.get("/api/reviews/finished", params={"year": 2025}).json()["finished"] == []
    # Rex's own rating of the same book is his; the owner's is unchanged.
    review(rex, alpha, rating=2, notes="meh")
    assert found(rex, "rating:2") == [alpha] and found(owner, "rating:2") == []
    assert owner.http.get(f"/api/works/{alpha}/review").json()["notes"] == "owner's secret"
    # A book you cannot see has no review page, for reading or writing.
    assert rex.http.get(f"/api/works/{hidden}/review").status_code == 404
    r = put_json(rex.http, f"/api/works/{hidden}/review", rex.csrf, {"rating": 3})
    assert r.status_code == 404
    # No CSRF token, no write.
    r = rex.http.put(f"/api/works/{alpha}/review", json={"rating": 5})
    assert r.status_code in (401, 403)


def test_filters_and_sorts_use_ratings_and_corrected_dates(household):
    owner, ids = household["owner"], household["ids"]
    alpha, bravo, hidden = ids["Alpha"], ids["Bravo"], ids["Hidden"]
    review(owner, alpha, rating=4.5)
    review(owner, bravo, rating=2)
    assert found(owner, "rating:4.5") == [alpha]
    assert found(owner, "rating:>=4") == [alpha]
    assert found(owner, "rating:1..3") == [bravo]
    assert set(found(owner, "-rating:>=4")) == {bravo, hidden}  # unrated included
    assert found(owner, "", sort="rating") == [alpha, bravo, hidden]  # unrated last
    assert found(owner, "", sort="rating", order="asc") == [bravo, alpha, hidden]
    # Derived dates until corrected: Bravo finished in 2026 per KOReader...
    read_to("owner", bravo, 1.0)
    set_state("owner", bravo, "2026-01-02", "2026-01-20")
    assert found(owner, "finished:2026") == [bravo]
    # ... then the person says they read it in 2023 and re-read it in 2024.
    review(owner, bravo, read_throughs=[
        {"started_on": "2023-02-01", "finished_on": "2023-03-15"},
        {"started_on": "2024-07-01", "finished_on": "2024-07-09"},
    ])  # fmt: skip
    assert found(owner, "finished:2026") == []
    assert found(owner, "finished:2023-03") == [bravo]
    assert found(owner, "finished:2024") == [bravo]
    assert found(owner, "started:2023-02-01") == [bravo]
    assert found(owner, "finished:2023-03-14") == []
    # Read before Lectern: no reading state at all.
    review(owner, hidden, read_throughs=[{"finished_on": "2024-01-31"}])
    assert set(found(owner, "finished:2024")) == {bravo, hidden}
    assert found(owner, "started:2024") == [bravo]
    # Sorting by finished uses the latest read-through: Bravo's re-read in July.
    assert found(owner, "", sort="finished")[:2] == [bravo, hidden]
    # A smart shelf of your favourites shows each viewer their own.
    r = api_post(owner.http, "/api/shelves", owner.csrf, {"name": "Best", "filter": "rating:>=4"})
    assert r.status_code == 201, r.text
    assert found(owner, "", shelf=r.json()["id"]) == [alpha]


def test_reaching_the_end_closes_an_open_read_through(household):
    owner, alpha = household["owner"], household["ids"]["Alpha"]
    read_to("owner", alpha, 1.0)  # first finish
    read_to("owner", alpha, 0.1)  # re-reading
    review(owner, alpha, read_throughs=[
        {"started_on": "2025-01-01", "finished_on": "2025-01-20"},
        {"started_on": "2026-01-03"},
    ])  # fmt: skip
    read_to("owner", alpha, 0.5)
    got = owner.http.get(f"/api/works/{alpha}/review").json()["read_throughs"]
    assert got[1]["finished_on"] is None
    read_to("owner", alpha, 0.99)
    today = datetime.now(UTC).date().isoformat()
    got = owner.http.get(f"/api/works/{alpha}/review").json()["read_throughs"]
    assert [(r["started_on"], r["finished_on"]) for r in got] == [
        ("2025-01-01", "2025-01-20"),
        ("2026-01-03", today),
    ]
    # Already finished: syncing the end again changes nothing.
    review(owner, alpha, read_throughs=[{"started_on": "2026-01-03"}])
    read_to("owner", alpha, 1.0)
    got = owner.http.get(f"/api/works/{alpha}/review").json()["read_throughs"]
    assert got[0]["finished_on"] is None


def test_the_rate_it_prompt_can_be_dismissed(household):
    owner, alpha = household["owner"], household["ids"]["Alpha"]
    read_to("owner", alpha, 1.0)
    assert owner.http.get(f"/api/works/{alpha}/review").json()["prompt"] is True
    r = api_post(owner.http, f"/api/works/{alpha}/review/dismiss", owner.csrf)
    assert r.status_code == 200 and r.json()["prompt"] is False
    assert owner.http.get(f"/api/works/{alpha}/review").json()["prompt"] is False
    # A manually added finish counts as finished too.
    bravo = household["ids"]["Bravo"]
    assert review(owner, bravo, read_throughs=[{"finished_on": "2020-01-01"}])["prompt"] is True


def test_year_of_finished_books_uses_corrected_dates_and_ratings(household):
    owner, ids = household["owner"], household["ids"]
    alpha, bravo, hidden = ids["Alpha"], ids["Bravo"], ids["Hidden"]
    # Alpha: finished in KOReader on 2025-05-02 (derived), rated 3.
    read_to("owner", alpha, 1.0)
    set_state("owner", alpha, "2025-04-01", "2025-05-02")
    review(owner, alpha, rating=3)
    # Bravo: KOReader says 2026, corrected to 2025-01-10, read again in 2025-11; rated 5.
    read_to("owner", bravo, 1.0)
    set_state("owner", bravo, "2026-01-01", "2026-01-05")
    review(owner, bravo, rating=5, read_throughs=[
        {"started_on": "2025-01-01", "finished_on": "2025-01-10"},
        {"started_on": "2025-11-01", "finished_on": "2025-11-30"},
    ])  # fmt: skip
    # Hidden: still being read in 2025, not finished.
    review(owner, hidden, read_throughs=[{"started_on": "2025-12-01"}])
    got = owner.http.get("/api/reviews/finished", params={"year": 2025}).json()
    assert [(b["work_id"], b["finished_on"], b["rating"]) for b in got["finished"]] == [
        (bravo, "2025-01-10", 5.0),
        (alpha, "2025-05-02", 3.0),
        (bravo, "2025-11-30", 5.0),
    ]
    assert got["count"] == 3
    assert [b["work_id"] for b in got["top_rated"]] == [bravo, bravo, alpha]
    assert owner.http.get("/api/reviews/finished", params={"year": 2026}).json()["count"] == 0
    # Rex's year is his own, and only books he can see would count.
    rex = household["rex"]
    assert rex.http.get("/api/reviews/finished", params={"year": 2025}).json()["count"] == 0


def test_mcp_needs_the_reviews_scope(app, household):
    owner, alpha = household["owner"], household["ids"]["Alpha"]
    review(owner, alpha, rating=4, notes="for me", read_throughs=[{"finished_on": "2025-02-02"}])
    reader = MCP(app, owner.key("read", label="read only"))
    ok, err = reader.call("search_library", query="rating:4")
    assert not ok and "reviews scope" in err
    ok, err = reader.call("search_library", sort="rating")
    assert not ok and "needs reviews scope" in err
    ok, err = reader.call("get_review", book_id=alpha)
    assert not ok and "needs reviews scope" in err
    # A saved smart shelf on rating matches nothing for a key without the scope.
    shelf = api_post(owner.http, "/api/shelves", owner.csrf, {"name": "Top", "filter": "rating:4"})
    assert shelf.status_code == 201, shelf.text
    assert reader.ok("search_library", query='shelf:"Top"')["books"] == []
    full = MCP(app, owner.key("read", "reviews", label="with reviews"))
    assert [b["id"] for b in full.ok("search_library", query="rating:4")["books"]] == [alpha]
    assert [b["id"] for b in full.ok("search_library", query='shelf:"Top"')["books"]] == [alpha]
    got = full.ok("get_review", book_id=alpha)
    assert (got["rating"], got["notes"]) == (4.0, "for me")
    assert full.ok("list_finished", year=2025)["books"][0]["work_id"] == alpha
    # Someone else's key only ever sees its own person's reviews.
    rex = household["rex"]
    rex_mcp = MCP(app, rex.key("read", "reviews", label="rex"))
    assert rex_mcp.ok("get_review", book_id=alpha)["rating"] is None
    assert rex_mcp.ok("search_library", query="rating:4")["books"] == []
    assert rex_mcp.ok("list_finished", year=2025)["books"] == []


def test_a_key_without_the_reviews_scope_never_sees_hand_entered_dates(app, household):
    owner, ids = household["owner"], household["ids"]
    alpha, bravo = ids["Alpha"], ids["Bravo"]
    # Lectern recorded both: Alpha finished 2026-03-10, Bravo 2026-01-20. The owner then says
    # Alpha was really read in June 2019.
    for w in (alpha, bravo):
        read_to("owner", w, 1.0)
    set_state("owner", alpha, "2026-03-01", "2026-03-10")
    set_state("owner", bravo, "2026-01-02", "2026-01-20")
    review(owner, alpha, read_throughs=[{"started_on": "2019-06-02", "finished_on": "2019-06-21"}])

    def ids_of(mcp, **args) -> list[int]:
        return [b["id"] for b in mcp.ok("search_library", **args)["books"]]

    reader = MCP(app, owner.key("read", label="read only"))
    assert ids_of(reader, query="finished:2019-06-21") == []
    assert ids_of(reader, query="started:2019-06-02") == []
    assert ids_of(reader, query="finished:2026-03") == [alpha]  # what Lectern recorded
    assert ids_of(reader, query="finished:2026", sort="finished") == [alpha, bravo]
    shelf = api_post(
        owner.http, "/api/shelves", owner.csrf, {"name": "Old", "filter": "finished:2019"}
    )
    assert shelf.status_code == 201, shelf.text
    assert ids_of(reader, query='shelf:"Old"') == []
    # With the scope (a key may hold all five), the corrected dates.
    full = MCP(app, owner.key("upload", "file", "read", "organize", "reviews", label="all"))
    assert ids_of(full, query="finished:2019-06-21") == [alpha]
    assert ids_of(full, query="started:2019-06-02") == [alpha]
    assert ids_of(full, query="finished:2026-03") == []
    assert ids_of(full, sort="finished")[:2] == [bravo, alpha]
    assert ids_of(full, query='shelf:"Old"') == [alpha]


def test_merging_keeps_reviews(household):
    owner, alpha, bravo = household["owner"], household["ids"]["Alpha"], household["ids"]["Bravo"]
    review(
        owner, alpha, rating=4, notes="from alpha", read_throughs=[{"finished_on": "2024-01-01"}]
    )
    review(owner, bravo, rating=2, read_throughs=[{"finished_on": "2025-01-01"}])
    review(household["rex"], alpha, notes="rex on alpha")
    r = api_post(owner.http, f"/api/works/{alpha}/merge", owner.csrf, {"target_work_id": bravo})
    assert r.status_code == 200, r.text
    got = owner.http.get(f"/api/works/{bravo}/review").json()
    assert (got["rating"], got["notes"]) == (2, "from alpha")  # the kept book's rating wins
    assert [x["finished_on"] for x in got["read_throughs"]] == ["2024-01-01", "2025-01-01"]
    assert household["rex"].http.get(f"/api/works/{bravo}/review").json()["notes"] == "rex on alpha"


def test_deleting_a_person_deletes_their_reviews(household):
    owner, alpha = household["owner"], household["ids"]["Alpha"]
    review(household["mia"], alpha, rating=3, read_throughs=[{"finished_on": "2024-01-01"}])
    with session_factory()() as db:
        db.execute(sa.delete(User).where(User.username == "mia"))
        db.commit()
        assert db.scalar(sa.select(sa.func.count()).select_from(BookReview)) == 0
        assert db.scalar(sa.select(sa.func.count()).select_from(ReadThrough)) == 0
    assert owner.http.get(f"/api/works/{alpha}/review").json()["rating"] is None


def test_migration_0030_up_and_down(tmp_path):
    from tests.conftest import schema_diff

    engine = make_engine(f"sqlite:///{tmp_path / 'm30.db'}")
    cfg = alembic_config(str(engine.url))
    upgrade_to_head(engine)
    with engine.connect() as conn:
        assert schema_diff(conn) == []
        conn.execute(sa.text(
            "INSERT INTO users (id, username, display_name, role, webauthn_user_handle,"
            " totp_enabled, is_active, created_at, show_technical_details, auto_file_imports,"
            " data_saver) VALUES (1, 'o', 'O', 'OWNER', x'00', 0, 1, '2026-01-01', 0, 0, 0)"
        ))  # fmt: skip
        conn.execute(sa.text(
            "INSERT INTO works (id, title, sort_title, author, sort_author, dir_path, has_cover,"
            " created_at, updated_at, version, readalong_enabled, data_saver_enabled,"
            " cover_seed) VALUES (1, 'T', 't', 'A', 'a', 'A/T', 0, '2026-01-01', '2026-01-01',"
            " 1, 1, 1, 0), (2, 'U', 'u', 'A', 'a', 'A/U', 0, '2026-01-01', '2026-01-01', 1, 1, 1,"
            " 0)"
        ))  # fmt: skip
        conn.execute(sa.text(
            "INSERT INTO book_reviews (user_id, work_id, rating, notes, updated_at)"
            " VALUES (1, 1, 9, 'n', '2026-01-01')"
        ))  # fmt: skip
        # Work 2 has no review yet, so only the CHECK can refuse these.
        for bad in (
            "INSERT INTO book_reviews (user_id, work_id, rating, notes, updated_at)"
            " VALUES (1, 2, 11, '', '2026-01-01')",
            "INSERT INTO book_reviews (user_id, work_id, rating, notes, updated_at)"
            " VALUES (1, 2, 0, '', '2026-01-01')",
            "INSERT INTO read_throughs (user_id, work_id, created_at) VALUES (1, 1, '2026-01-01')",
            "INSERT INTO read_throughs (user_id, work_id, started_at, finished_at, created_at)"
            " VALUES (1, 1, '2026-02-01', '2026-01-01', '2026-01-01')",
        ):
            with pytest.raises(sa.exc.IntegrityError, match="CHECK constraint failed"):
                conn.execute(sa.text(bad))
        conn.commit()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0029_playback_speed")
    tables = sa.inspect(engine).get_table_names()
    assert "book_reviews" not in tables and "read_throughs" not in tables
    upgrade_to_head(engine)
    with engine.connect() as conn:
        assert schema_diff(conn) == []
