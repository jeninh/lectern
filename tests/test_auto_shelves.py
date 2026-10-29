"""Auto-shelves: the engine (lectern.auto_shelves) and what each person may see of it.

Expected children, counts, and visibility come from how each library is built here, never from
the code under test.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import select

from lectern import access, auto_shelves, filters
from lectern.db import make_engine, session_factory
from lectern.migrate import alembic_config, upgrade_to_head
from lectern.models import (
    AppPassword,
    DeviceFollow,
    InstanceSetting,
    Role,
    Shelf,
    ShelfAutoChild,
    ShelfGrant,
    ShelfItem,
    ShelfPermission,
    User,
    Work,
)


@pytest.fixture
def db(client):  # the app started: database migrated
    s = session_factory()()
    yield s
    s.close()


@pytest.fixture
def owner(db):
    return person(db, "owner", Role.OWNER)


def person(db, name: str, role: Role) -> User:
    u = User(username=name, role=role, webauthn_user_handle=os.urandom(32))
    db.add(u)
    db.flush()
    return u


def book(db, title: str, *authors: str, tags=None, lang=None, narrators=None) -> Work:
    w = Work(title=title, sort_title=title.lower(), author=authors[0] if authors else "",
             sort_author=(authors[0] if authors else "").lower(), dir_path=title,
             authors=[{"name": a} for a in authors] or None, tags=tags, language=lang,
             narrators=narrators)  # fmt: skip
    db.add(w)
    db.flush()
    return w


def look(db, user: User) -> dict[int, str]:
    """A fresh request: commit what the test did, then the shelves ``user`` may see."""
    db.commit()
    db.expire_all()
    return access.shelf_perms(db, user)[1]


def generated(db, group: Shelf) -> dict[str, Shelf]:
    """Every generated value shelf of ``group`` by name, dormant ones included."""
    db.expire_all()
    rows = db.scalars(
        select(Shelf)
        .join(ShelfAutoChild, ShelfAutoChild.shelf_id == Shelf.id)
        .where(ShelfAutoChild.group_id == group.id, ShelfAutoChild.kind == "value")
    )
    return {s.name: s for s in rows}


def shows(db, user: User, shelf: Shelf) -> set[int]:
    """Ids of the works ``shelf``'s rule shows ``user``."""
    ctx = filters.Context(db, user).lenient()
    return set(db.scalars(select(Work.id).where(filters.smart_predicate(ctx, shelf))))


def exact(key: str, value: str) -> str:
    return filters.format_query(filters.parse(f'{key}:="{value}"'))


def test_authors_group_makes_one_exact_child_per_author(db, owner):
    war = book(db, "War and Peace", "Leo Tolstoy")
    anna = book(db, "Anna Karenina", "Leo Tolstoy", "Ann")  # counts for both authors
    ann = book(db, "Ann's Book", "Ann")
    choc = book(db, "Chocolat", "Joanne Harris")
    five = book(db, "Five Quarters", "Joanne Harris")
    jane = book(db, "Jane Eyre", "Charlotte Brontë")
    villette = book(db, "Villette", "Charlotte Brontë")
    shirley = book(db, "Shirley", "Charlotte Bronte")
    book(db, "Lonely", "Solo Writer")  # one book: below the minimum of 2
    group = auto_shelves.create_group(db, owner, "Authors", "author")
    perms = look(db, owner)

    kids = generated(db, group)
    assert set(kids) == {"Leo Tolstoy", "Ann", "Joanne Harris", "Charlotte Brontë"}
    expected = {
        "Leo Tolstoy": {war.id, anna.id},
        "Ann": {anna.id, ann.id},  # not "Joanne Harris", though "joanne" contains "ann"
        "Joanne Harris": {choc.id, five.id},
        "Charlotte Brontë": {jane.id, villette.id, shirley.id},  # Bronte folds into Brontë
    }
    view = auto_shelves.view(db, owner)
    for name, works in expected.items():
        assert kids[name].rule["filter"] == exact("author", name)
        assert shows(db, owner, kids[name]) == works
        assert view.counts[kids[name].id] == len(works)
        assert kids[name].id in perms and kids[name].auto_kind == "value"
    # Buckets on by default for authors: group / letter / author.
    buckets = {s.name: s.id for s in db.scalars(select(Shelf).where(Shelf.parent_id == group.id))}
    assert set(buckets) == {"L", "A", "J", "C"}
    assert kids["Leo Tolstoy"].parent_id == buckets["L"]
    assert db.get(Shelf, buckets["L"]).auto_kind == "bucket" and group.auto_kind == "group"
    assert access.visible_path(kids["Ann"], *access.shelf_perms(db, owner)) == [
        group, db.get(Shelf, buckets["A"]), kids["Ann"]
    ]  # fmt: skip


def test_new_books_add_children_on_the_next_look_and_names_are_safe(db, owner):
    group = auto_shelves.create_group(db, owner, "Authors", "author", buckets=False)
    book(db, "One", "Solo Writer")
    look(db, owner)
    assert generated(db, group) == {}
    second = book(db, "Two", "Solo Writer")
    book(db, "Rock 1", "AC/DC")
    book(db, "Rock 2", "AC/DC")
    long = "x" * (filters.MAX_VALUE_CHARS + 1)
    book(db, "Long 1", long)
    book(db, "Long 2", long)
    book(db, "Bell 1", "Ding\x07Dong")
    book(db, "Bell 2", "Ding\x07Dong")
    look(db, owner)
    kids = generated(db, group)
    assert set(kids) == {"Solo Writer", "AC∕DC", "DingDong"}  # the long name is skipped
    assert second.id in shows(db, owner, kids["Solo Writer"])
    assert kids["AC∕DC"].rule["filter"] == exact("author", "AC/DC")
    assert len(shows(db, owner, kids["AC∕DC"])) == 2
    assert all(s.parent_id == group.id for s in kids.values())


def test_trashed_books_make_a_child_dormant_and_restoring_brings_back_the_same_shelf(db, owner):
    a, b = book(db, "A1", "Leo Tolstoy"), book(db, "A2", "Leo Tolstoy")
    c, d = book(db, "B1", "Ann"), book(db, "B2", "Ann")
    group = auto_shelves.create_group(db, owner, "Authors", "author")
    look(db, owner)
    leo, ann_id = generated(db, group)["Leo Tolstoy"], generated(db, group)["Ann"].id
    device = AppPassword(user_id=owner.id, label="Kindle", key_hash="k" * 64)
    db.add(device)
    db.flush()
    db.add(DeviceFollow(app_password_id=device.id, shelf_id=leo.id))

    now = datetime.now(UTC)
    a.trashed_at = now  # one Tolstoy left: below the minimum
    perms = look(db, owner)
    assert leo.id not in perms and access.shelf_permission(db, owner, leo) is None
    bucket_l = leo.parent_id
    assert bucket_l not in perms  # its only child is hidden
    b.trashed_at = c.trashed_at = d.trashed_at = now  # none left of either
    look(db, owner)
    kids = generated(db, group)
    assert "Ann" not in kids and db.get(Shelf, ann_id) is None  # nothing, nobody follows it
    assert kids["Leo Tolstoy"].id == leo.id  # followed: kept, dormant

    for w in (a, b, c, d):
        w.trashed_at = None
    perms = look(db, owner)
    kids = generated(db, group)
    assert kids["Leo Tolstoy"].id == leo.id and leo.id in perms
    assert shows(db, owner, kids["Leo Tolstoy"]) == {a.id, b.id}
    assert db.get(DeviceFollow, device.id).shelf_id == leo.id
    assert kids["Ann"].id != ann_id and kids["Ann"].id in perms


@pytest.fixture
def shared(db, owner):
    """A reader, rex, granted the Authors group and a "Shared" shelf: three of Tolstoy's four
    books and one of Ann's two are on it; Joanne Harris's books are on no shelf of his."""
    rex = person(db, "rex", Role.READER)
    books = {
        "leo": [book(db, f"Leo {i}", "Leo Tolstoy", tags=["Fantasy"]) for i in range(4)],
        "ann": [book(db, f"Ann {i}", "Ann") for i in range(2)],
        "jo": [book(db, f"Jo {i}", "Joanne Harris") for i in range(2)],
    }
    group = auto_shelves.create_group(db, owner, "Authors", "author")
    tags = auto_shelves.create_group(db, owner, "Tags", "tag", buckets=False)
    shelf = Shelf(owner_id=owner.id, name="Shared")
    db.add(shelf)
    db.flush()
    for i, w in enumerate([*books["leo"][:3], books["ann"][0]]):
        db.add(ShelfItem(shelf_id=shelf.id, work_id=w.id, position=i))
    for sid in (group.id, tags.id, shelf.id):
        db.add(ShelfGrant(shelf_id=sid, user_id=rex.id, permission=ShelfPermission.VIEW))
    look(db, owner)
    return {"rex": rex, "books": books, "group": group, "tags": tags, "shelf": shelf,
            "kids": generated(db, group), "tag_kids": generated(db, tags)}  # fmt: skip


def test_a_reader_sees_only_authors_of_books_they_may_see(db, owner, shared):
    rex, kids = shared["rex"], shared["kids"]
    leo, ann, jo = kids["Leo Tolstoy"], kids["Ann"], kids["Joanne Harris"]
    perms = look(db, rex)
    view = auto_shelves.view(db, rex)

    assert leo.id in perms and view.counts == {leo.id: 3, shared["tag_kids"]["Fantasy"].id: 3}
    assert auto_shelves.view(db, owner).counts[leo.id] == 4  # what rex sees is counted, not all
    # Ann: one of her books is visible to him, under the minimum. Joanne Harris: none.
    for hidden in (ann, jo):
        assert hidden.id not in perms and hidden.id in view.hidden
        assert access.shelf_permission(db, rex, hidden) is None
        assert access.shelf_permission(db, owner, hidden) == "owner"
    # Buckets whose every child is hidden from him are hidden too, and so are their names.
    assert leo.parent_id in perms
    assert ann.parent_id not in perms and jo.parent_id not in perms
    assert {s.id for s in db.scalars(select(Shelf).where(Shelf.parent_id == shared["group"].id))
            if s.id in perms} == {leo.parent_id}  # fmt: skip
    # A filter naming the hidden shelf fails as for a shelf that does not exist.
    ctx = filters.Context(db, rex)
    errors = []
    for text in ('shelf:"Authors/J/Joanne Harris"', 'shelf:"Authors/J/Nobody"', f"shelf:#{jo.id}"):
        with pytest.raises(filters.FilterError) as e:
            filters.predicate(ctx, filters.parse(text))
        errors.append(e.value.code)
    assert errors == ["unknown_shelf"] * 3
    filters.predicate(ctx, filters.parse(f"shelf:#{leo.id}"))  # the visible one is fine


def test_a_readers_view_follows_tag_edits_grants_and_shelf_items_at_once(db, owner, shared):
    rex, books = shared["rex"], shared["books"]
    leo, fantasy = shared["kids"]["Leo Tolstoy"], shared["tag_kids"]["Fantasy"]
    assert auto_shelves.view(db, rex).counts == {leo.id: 3, fantasy.id: 3}

    # All within one request: changed, flushed, not committed.
    w = db.get(Work, books["leo"][0].id)
    w.tags = ["History"]
    db.flush()
    assert auto_shelves.view(db, rex).counts == {leo.id: 3, fantasy.id: 2}
    db.execute(
        sa.delete(ShelfItem).where(
            ShelfItem.shelf_id == shared["shelf"].id, ShelfItem.work_id == books["leo"][1].id
        )
    )
    assert auto_shelves.view(db, rex).counts == {leo.id: 2}  # one Fantasy book: too few
    assert fantasy.id not in access.shelf_perms(db, rex)[1]
    db.execute(sa.delete(ShelfGrant).where(ShelfGrant.shelf_id == shared["shelf"].id))
    assert auto_shelves.view(db, rex).counts == {}
    assert leo.id not in access.shelf_perms(db, rex)[1]
    db.rollback()
    assert auto_shelves.view(db, rex).counts == {leo.id: 3, fantasy.id: 3}


def test_genre_and_language_children(db, owner):
    epic = book(db, "Epic", tags=["Epic"], lang="en-GB")
    other = book(db, "Other", lang="en")
    both = book(db, "Both", tags=["fantasy", "épic"], lang="EN-us")
    plain = book(db, "Plain", tags=["Fantasy"], lang="fr")
    genres = auto_shelves.create_group(db, owner, "Genres", "genre")
    langs = auto_shelves.create_group(db, owner, "Languages", "lang")
    look(db, owner)
    assert generated(db, genres) == {}  # no genre map yet
    db.add(InstanceSetting(key="genre_map", value={"Fantasy": ["Fantasy", "Epic"]}))
    look(db, owner)
    fantasy = generated(db, genres)["Fantasy"]
    assert fantasy.rule["filter"] == "tag:fantasy,epic"
    assert shows(db, owner, fantasy) == {epic.id, both.id, plain.id}
    assert auto_shelves.view(db, owner).counts[fantasy.id] == 3  # "Both" counted once
    kids = generated(db, langs)
    assert {k: shows(db, owner, s) for k, s in kids.items()} == {
        "en": {epic.id, other.id, both.id}, "fr": {plain.id}
    }  # fmt: skip


def test_depth_cap_counts_the_bucket_level(db, owner):
    parent = None
    for i in range(6):
        parent = Shelf(owner_id=owner.id, name=f"L{i}", parent=parent)
        db.add(parent)
    db.flush()
    level5 = parent.parent
    # Under a depth-5 shelf: group 6, bucket 7, child 8.
    auto_shelves.create_group(db, owner, "Authors", "author", level5)
    with pytest.raises(ValueError, match="too_deep"):
        auto_shelves.create_group(db, owner, "Authors", "author", parent)
    auto_shelves.create_group(db, owner, "Authors", "author", parent, buckets=False)


def test_migration_0041_round_trips_and_keeps_the_sibling_name_index(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    cfg = alembic_config(str(engine.url))

    def state():
        with engine.connect() as conn:
            names = set(sa.inspect(conn).get_table_names())
            sql = conn.execute(
                sa.text("SELECT sql FROM sqlite_master WHERE name = 'uq_shelf_sibling_name'")
            ).scalar()
            idx = {i["name"] for i in sa.inspect(conn).get_indexes("works")}
        return {"shelf_auto_groups", "shelf_auto_children"} <= names, sql, idx

    upgrade_to_head(engine)
    tables, sibling, idx = state()
    assert tables and "ix_works_updated_at" in idx and "coalesce(parent_id, 0)" in sibling
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0029_playback_speed")
    tables, after, idx = state()
    assert not tables and "ix_works_updated_at" not in idx and after == sibling
    upgrade_to_head(engine)
    assert state() == (True, sibling, state()[2])
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, username, display_name, role, "
                             "webauthn_user_handle, totp_enabled, is_active, created_at, "
                             "show_technical_details, auto_file_imports, data_saver) VALUES "
                             "(1, 'o', '', 'OWNER', x'00', 0, 1, '2026-01-01', 0, 0, 0)"))  # fmt: skip
        insert = sa.text(
            "INSERT INTO shelves (owner_id, name, created_at, updated_at) "
            "VALUES (1, 'Same', '2026-01-01', '2026-01-01')"
        )
        conn.execute(insert)
        with pytest.raises(sa.exc.IntegrityError):
            with conn.begin_nested():
                conn.execute(insert)
