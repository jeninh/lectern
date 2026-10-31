"""Listening queue API: private ownership, ordering, duplicate semantics, and schema."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command
from tests.conftest import ORIGIN, api_post, login, make_user

from lectern.db import make_engine, session_factory
from lectern.migrate import alembic_config, upgrade_to_head
from lectern.models import AudioTrack, Work


def _audio_work(title: str) -> tuple[int, int]:
    with session_factory()() as db:
        work = Work(
            title=title,
            sort_title=title,
            author="Author",
            sort_author="author",
            dir_path=f"A/{title}",
        )
        db.add(work)
        db.flush()
        track = AudioTrack(
            work_id=work.id,
            position=0,
            rel_path="one.mp3",
            media_type="audio/mpeg",
            codec="mp3",
            duration_s=10,
            offset_s=0,
            size=1,
            sha256=(str(work.id) * 64)[:64],
        )
        db.add(track)
        db.commit()
        return work.id, track.id


def test_listening_queue_moves_duplicates_to_end_and_is_owner_scoped(client, owner):
    first, track = _audio_work("First")
    second, _ = _audio_work("Second")

    def add(body):
        return api_post(client, "/api/listening-queue/items", owner["csrf"], body)

    assert add({"work_id": first}).status_code == 201
    assert add({"work_id": second}).status_code == 201
    # A duplicate has one row and deliberately becomes the last item.
    assert add({"work_id": first}).status_code == 201
    data = client.get("/api/listening-queue").json()
    assert [x["work"]["id"] for x in data["items"]] == [second, first]
    assert add({"work_id": first, "track_id": track}).status_code == 201
    data = client.get("/api/listening-queue").json()
    assert [(x["work"]["id"], x["track_id"]) for x in data["items"]] == [
        (second, None),
        (first, None),
        (first, track),
    ]
    assert (
        api_post(
            client,
            "/api/listening-queue/items",
            owner["csrf"],
            {"work_id": first, "track_id": 99999},
        ).status_code
        == 422
    )
    make_user(client, owner["csrf"], "listener", "member")
    member = login(client, "listener", "listener-correct horse")
    assert client.get("/api/listening-queue").json() == {"items": []}
    # IDs belonging to another user are indistinguishable from unknown ids.
    item_id = data["items"][0]["id"]
    r = client.delete(
        f"/api/listening-queue/items/{item_id}", headers={"origin": ORIGIN, "x-csrf-token": member}
    )
    assert r.status_code == 404


def test_listening_queue_reorder_rejects_stale_list_and_clear(client, owner):
    first, _ = _audio_work("One")
    second, _ = _audio_work("Two")
    for work_id in (first, second):
        assert (
            api_post(
                client, "/api/listening-queue/items", owner["csrf"], {"work_id": work_id}
            ).status_code
            == 201
        )
    items = client.get("/api/listening-queue").json()["items"]
    r = client.put(
        "/api/listening-queue/order",
        json={"item_ids": [items[0]["id"]]},
        headers={"origin": ORIGIN, "x-csrf-token": owner["csrf"]},
    )
    assert r.status_code == 409
    r = client.put(
        "/api/listening-queue/order",
        json={"item_ids": [items[1]["id"], items[0]["id"]]},
        headers={"origin": ORIGIN, "x-csrf-token": owner["csrf"]},
    )
    assert [x["work"]["id"] for x in r.json()["items"]] == [second, first]
    assert client.delete(
        "/api/listening-queue", headers={"origin": ORIGIN, "x-csrf-token": owner["csrf"]}
    ).json() == {"items": []}


def test_listening_queue_migration_round_trip(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'queue.db'}")
    upgrade_to_head(engine)
    assert "listening_queue_items" in sa.inspect(engine).get_table_names()
    cfg = alembic_config(str(engine.url))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0043_reading_stats")
    assert "listening_queue_items" not in sa.inspect(engine).get_table_names()
