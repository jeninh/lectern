"""Listening queue API: replay-safe mutations, ordering, privacy, and migration schema."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command
from tests.conftest import ORIGIN, login, make_user

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


def _mutate(client, csrf: str, version: int, key: str, **body):
    return client.post(
        "/api/listening-queue/mutations",
        json={"base_version": version, **body},
        headers={"origin": ORIGIN, "x-csrf-token": csrf, "idempotency-key": key},
    )


def test_listening_queue_versions_retries_duplicates_and_chapters(client, owner):
    first, track = _audio_work("First")
    second, _ = _audio_work("Second")
    one = _mutate(client, owner["csrf"], 0, "queue-add-1", operation="enqueue", work_id=first)
    assert one.status_code == 200
    # A lost response replay returns the same state rather than moving or duplicating the item.
    assert (
        _mutate(client, owner["csrf"], 0, "queue-add-1", operation="enqueue", work_id=first).json()
        == one.json()
    )
    two = _mutate(client, owner["csrf"], 1, "queue-add-2", operation="enqueue", work_id=second)
    assert two.status_code == 200
    moved = _mutate(client, owner["csrf"], 2, "queue-move-1", operation="enqueue", work_id=first)
    assert [x["work"]["id"] for x in moved.json()["items"]] == [second, first]
    chapter = _mutate(
        client,
        owner["csrf"],
        3,
        "queue-chapter-1",
        operation="enqueue",
        work_id=first,
        track_id=track,
        chapter_start_s=2,
        chapter_end_s=4,
    )
    assert chapter.status_code == 200
    queued = chapter.json()
    assert [
        (x["work"]["id"], x["chapter_start_s"], x["chapter_end_s"]) for x in queued["items"]
    ] == [
        (second, None, None),
        (first, None, None),
        (first, 2, 4),
    ]
    assert queued["version"] == 4


def test_offline_remove_clear_and_consume_rebase_without_lost_updates(client, owner):
    first, _ = _audio_work("One")
    second, _ = _audio_work("Two")
    third, _ = _audio_work("Three")
    q1 = _mutate(client, owner["csrf"], 0, "queue-one", operation="enqueue", work_id=first).json()
    q2 = _mutate(client, owner["csrf"], 1, "queue-two", operation="enqueue", work_id=second).json()
    first_id, second_id = [x["id"] for x in q2["items"]]
    # Another device changes the queue while this device is offline.
    q3 = _mutate(client, owner["csrf"], 2, "queue-three", operation="enqueue", work_id=third).json()
    # Stale consume/remove is rejected, then a replay can safely remove its original item.
    stale = _mutate(client, owner["csrf"], 2, "queue-consume", operation="remove", item_id=first_id)
    assert stale.status_code == 409
    consumed = _mutate(
        client, owner["csrf"], q3["version"], "queue-consume", operation="remove", item_id=first_id
    )
    assert [x["work"]["id"] for x in consumed.json()["items"]] == [second, third]
    # A clear from the old snapshot names only items it saw: it cannot resurrect or erase Three.
    stale_clear = _mutate(
        client,
        owner["csrf"],
        2,
        "queue-clear",
        operation="clear",
        item_ids=[first_id, second_id],
    )
    assert stale_clear.status_code == 409
    cleared = _mutate(
        client,
        owner["csrf"],
        consumed.json()["version"],
        "queue-clear",
        operation="clear",
        item_ids=[first_id, second_id],
    )
    assert [x["work"]["id"] for x in cleared.json()["items"]] == [third]
    assert q1["version"] == 1


def test_listening_queue_is_owner_scoped_and_stale_order_never_wins(client, owner):
    first, _ = _audio_work("One")
    second, _ = _audio_work("Two")
    q1 = _mutate(
        client, owner["csrf"], 0, "queue-order-1", operation="enqueue", work_id=first
    ).json()
    q2 = _mutate(
        client, owner["csrf"], 1, "queue-order-2", operation="enqueue", work_id=second
    ).json()
    ids = [x["id"] for x in q2["items"]]
    assert (
        _mutate(
            client, owner["csrf"], 1, "queue-order-stale", operation="reorder", item_ids=ids[::-1]
        ).status_code
        == 409
    )
    make_user(client, owner["csrf"], "listener", "member")
    member = login(client, "listener", "listener-correct horse")
    assert client.get("/api/listening-queue").json() == {"version": 0, "items": []}
    assert (
        _mutate(client, member, 0, "queue-other", operation="remove", item_id=ids[0]).status_code
        == 404
    )
    assert q1["version"] == 1


def test_listening_queue_migration_round_trip(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'queue.db'}")
    upgrade_to_head(engine)
    tables = sa.inspect(engine).get_table_names()
    assert {"listening_queue_items", "listening_queue_states"} <= set(tables)
    cfg = alembic_config(str(engine.url))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0043_reading_stats")
    assert "listening_queue_items" not in sa.inspect(engine).get_table_names()
    assert "listening_queue_states" not in sa.inspect(engine).get_table_names()
