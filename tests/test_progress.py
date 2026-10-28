"""Text-only sentence lists, the canonical progress API, and cross-device hand-off data."""

from __future__ import annotations

import json
import shutil

import pytest
from tests.conftest import (
    MAGI,
    OWNER_PASSWORD,
    api_post,
    drain_inbox,
    ko_headers,
    login,
    make_sync_key,
    make_user,
    needs_ffmpeg,
    put_json,
)

from lectern.db import session_factory
from lectern.ingest.hashing import partial_md5
from lectern.ingest.sentences import epub_sentences, replace_sentences, segment
from lectern.models import ReadingProgress, WorkProgress


def test_segmenter_matches_the_hand_checked_fixture_map():
    expected = json.loads((MAGI / "sentence_map.json").read_text())["sentences"]
    mine = {
        (s["spine_index"], s["char_start"], s["char_end"])
        for s in epub_sentences(MAGI / "the-gift-of-the-magi.epub")
    }
    for s in expected:
        assert (s["spine_index"], s["char_start"], s["char_end"]) in mine, s["text"]


def test_segmenter_edge_cases():
    text = "Dr. Watson met J. R. R. Tolkien. “Really?” he asked! Fine… Ok"
    spans = [text[a:b] for a, b in segment(text)]
    assert spans == ["Dr. Watson met J. R. R. Tolkien.", "“Really?” he asked!", "Fine…", "Ok"]
    long = " ".join(["word"] * 400)  # no punctuation: capped at word boundaries
    parts = segment(long)
    assert all(b - a <= 600 for a, b in parts) and parts[0][0] == 0 and parts[-1][1] == len(long)
    assert all(long[a] != " " and long[b - 1] != " " for a, b in parts)
    assert segment("") == []


@pytest.fixture
def magi_work(client, owner, settings):
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    shutil.copy(MAGI / "the-gift-of-the-magi.epub", settings.inbox_path / "magi.epub")
    drain_inbox(settings)
    return client.get("/api/library").json()["works"][0]


def test_sentences_are_built_at_ingest(client, magi_work):
    wid = magi_work["id"]
    r = client.get(f"/api/works/{wid}/sentences", params={"from": 0, "to": 3}).json()
    assert r["count"] > 200 and r["alignment"] is None
    assert [s["n"] for s in r["sentences"]] == [0, 1, 2]
    assert all(not s["matched"] and s["audio_start_s"] is None for s in r["sentences"])
    r = client.get(f"/api/works/{wid}/sentences", params={"spine": 2, "char": 40}).json()
    first = r["sentences"][0]
    assert (first["spine_index"], first["char_start"], first["char_end"]) == (2, 35, 48)
    assert len(r["sentences"]) == 101
    r = client.get(f"/api/works/{wid}/sentences", params={"t": 3.0, "around": 2}).json()
    assert r["sentences"] == []  # nothing aligned yet
    assert client.get(f"/api/works/{wid}/sentences").status_code == 400


def test_progress_put_get_last_write_wins_and_is_per_user(client, owner, magi_work):
    wid = magi_work["id"]
    csrf = owner["csrf"]
    empty = client.get(f"/api/works/{wid}/progress").json()
    assert empty["position"] is None and empty["source"] is None and empty["koreader"] is None

    r = put_json(client, f"/api/works/{wid}/progress", csrf,
                 {"source": "web", "device": "Firefox on macOS", "spine_index": 2, "char": 40,
                  "raw": "epubcfi(/6/6!/4/2/1:40)"})  # fmt: skip
    assert r.status_code == 200, r.text
    p = r.json()
    # Char 40 of spine item 2 is 5 characters into "That was all." (35-48).
    hit = client.get(f"/api/works/{wid}/sentences", params={"spine": 2, "char": 40}).json()
    n = hit["sentences"][0]["n"]
    assert p["position"] == {"sentence_n": n, "offset": 5}
    assert p["text"] == {"spine_index": 2, "char": 40}
    assert p["source"] == "web" and p["device"] == "Firefox on macOS"
    assert p["raw"] == "epubcfi(/6/6!/4/2/1:40)" and p["received_at"].endswith("Z")
    assert p["audio_s"] is None and 0 < p["percent"] < 1

    flat = client.get(f"/api/works/{wid}/sentences", params={"spine": 2, "char": 721}).json()
    m = flat["sentences"][0]["n"]
    r = put_json(client, f"/api/works/{wid}/progress", csrf,
                 {"source": "web", "device": "Phone", "sentence_n": m, "offset": 999})  # fmt: skip
    p = r.json()
    # "A furnished flat at $8 per week." (721-753): the offset is clamped to its length.
    assert p["position"] == {"sentence_n": m, "offset": 32} and p["raw"] == f"{m}:999"
    assert p["device"] == "Phone" and p["text"] == {"spine_index": 2, "char": 753}

    bad = [
        {"source": "koreader", "device": "x", "sentence_n": 1},
        {"source": "web", "sentence_n": 1, "spine_index": 2, "char": 0},
        {"source": "web"},
        {"source": "web", "sentence_n": 10**6},
    ]
    for body in bad:
        assert put_json(client, f"/api/works/{wid}/progress", csrf, body).status_code == 422, body
    # No audio yet: an audio position is a conflict, not a silent guess.
    r = put_json(client, f"/api/works/{wid}/progress", csrf, {"source": "audio", "audio_s": 3})
    assert r.status_code == 409 and r.json()["detail"] == "no_audio"

    # Another user's position is independent.
    make_user(client, csrf, "mia", "member")
    mia = login(client, "mia", "mia-correct horse")
    assert client.get(f"/api/works/{wid}/progress").json()["position"] is None
    put_json(client, f"/api/works/{wid}/progress", mia, {"source": "web", "sentence_n": 3})
    login(client, "owner", OWNER_PASSWORD)
    assert client.get(f"/api/works/{wid}/progress").json()["position"]["sentence_n"] == m
    cont = client.get("/api/continue").json()["items"]
    assert cont[0]["id"] == wid and cont[0]["latest"] == "canonical"
    assert cont[0]["position"]["device"] == "Phone" and cont[0]["progress"] is None


def test_koreader_progress_is_reported_for_handoff_and_left_untouched(
    client, owner, magi_work, settings
):
    wid = magi_work["id"]
    csrf = owner["csrf"]
    put_json(
        client,
        f"/api/works/{wid}/progress",
        csrf,
        {"source": "web", "device": "Laptop", "sentence_n": 2},
    )
    key = make_sync_key(client, csrf, "Kindle")
    epub = settings.library_dir / magi_work["dir_path"] / magi_work["files"][0]["rel_path"]
    doc = partial_md5(epub)
    xp = "/body/DocFragment[3]/body/p[4]/text().12"
    r = client.put("/syncs/progress", headers=ko_headers("owner", key),
                   json={"document": doc, "progress": xp, "percentage": 0.5, "device": "Kindle"})  # fmt: skip
    assert r.status_code == 200
    p = client.get(f"/api/works/{wid}/progress").json()
    k = p["koreader"]
    assert k["device"] == "Kindle" and k["raw"] == xp and k["estimated"] is True
    assert k["text"]["spine_index"] == 2 and k["percent"] == 0.5
    assert k["received_at"] >= p["received_at"]
    # The canonical position did not move: hand-off is the client's (and the reader's) choice.
    assert p["position"]["sentence_n"] == 2 and p["source"] == "web"
    cont = client.get("/api/continue").json()["items"][0]
    assert cont["latest"] == "koreader" and cont["progress"]["device"] == "Kindle"
    # Writing on the web never touches KOReader's own record.
    put_json(client, f"/api/works/{wid}/progress", csrf, {"source": "web", "sentence_n": 50})
    with session_factory()() as db:
        (row,) = db.query(ReadingProgress).all()
        assert row.progress == xp and row.percentage == 0.5
    got = client.get(f"/syncs/progress/{doc}", headers=ko_headers("owner", key)).json()
    assert got["progress"] == xp


def test_replacing_sentences_reprojects_positions(client, owner, magi_work):
    wid = magi_work["id"]
    put_json(
        client,
        f"/api/works/{wid}/progress",
        owner["csrf"],
        {"source": "web", "spine_index": 2, "char": 300},
    )
    before = client.get(f"/api/works/{wid}/progress").json()
    fixture = json.loads((MAGI / "sentence_map.json").read_text())["sentences"]
    with session_factory()() as db:
        replace_sentences(db, wid, fixture)  # what alignment (milestone 3) will do
        db.commit()
        (row,) = db.query(WorkProgress).all()
    after = client.get(f"/api/works/{wid}/progress").json()
    assert after["count"] == 15 and before["count"] != 15
    # Char 300 lies in fixture sentence 4 (285-314): same text position, new numbering.
    assert after["position"] == {"sentence_n": 4, "offset": 15} and row.sentence_n == 4
    assert after["text"] == before["text"] == {"spine_index": 2, "char": 300}
    # Now aligned: audio is exact, not estimated.
    assert after["audio_s"] == pytest.approx(20.98 + (22.68 - 20.98) * 15 / 29, abs=0.01)
    assert after["estimated"] is False


@needs_ffmpeg
def test_audio_and_text_positions_meet_on_one_work(client, owner, magi_work, settings):
    wid = magi_work["id"]
    csrf = owner["csrf"]
    shutil.copy(MAGI / "excerpt.mp3", settings.inbox_path / "excerpt.mp3")
    drain_inbox(settings)
    audio_work = next(w for w in client.get("/api/library").json()["works"] if w["id"] != wid)
    # The listener had started on the separate audio work; their place survives the merge.
    r = put_json(client, f"/api/works/{audio_work['id']}/progress", csrf,
                 {"source": "audio", "device": "Phone", "audio_s": 30.5})  # fmt: skip
    assert r.status_code == 200 and r.json()["audio_s"] == 30.5 and r.json()["position"] is None
    assert (
        api_post(
            client, f"/api/works/{audio_work['id']}/merge", csrf, {"target_work_id": wid}
        ).status_code
        == 200
    )
    p = client.get(f"/api/works/{wid}/progress").json()
    assert p["source"] == "audio" and p["audio_s"] == 30.5 and p["estimated"] is True
    total = p["total_duration_s"]
    assert total == pytest.approx(77.17, abs=0.2) and p["text"] is not None
    # Before alignment, text <-> audio is by share of the book, and it round-trips: reading on
    # from the projected text position puts the player back at about the same second.
    r = put_json(client, f"/api/works/{wid}/progress", csrf,
                 {"source": "web", "spine_index": p["text"]["spine_index"], "char": p["text"]["char"]})  # fmt: skip
    back = r.json()
    assert back["estimated"] is True and back["audio_s"] == pytest.approx(30.5, abs=0.5)
    r = put_json(client, f"/api/works/{wid}/progress", csrf, {"source": "audio", "audio_s": 9999})
    assert r.json()["audio_s"] == pytest.approx(total, abs=0.01)  # clamped to the timeline


def test_listening_speed_is_kept_per_user_and_work(client, owner, magi_work, settings):
    """Issue #71: each book keeps the speed last used for it, next to the canonical position,
    for the person who set it and only on books they can see."""
    magi = magi_work["id"]
    csrf = owner["csrf"]
    shutil.copy(MAGI / "excerpt.mp3", settings.inbox_path / "excerpt.mp3")
    drain_inbox(settings)
    audio = next(w["id"] for w in client.get("/api/library").json()["works"] if w["id"] != magi)

    def speed(wid):
        return client.get(f"/api/works/{wid}/progress").json()["speed"]

    assert speed(audio) is None  # never played: the app uses the person's default
    # Book A at 1.5x from the player, book B at 1x from read-along's listen mode.
    r = put_json(client, f"/api/works/{audio}/progress", csrf,
                 {"source": "audio", "device": "Phone", "audio_s": 12, "speed": 1.5})  # fmt: skip
    assert r.status_code == 200 and r.json()["speed"] == 1.5
    put_json(client, f"/api/works/{magi}/progress", csrf,
             {"source": "audio", "device": "Laptop", "sentence_n": 4, "speed": 1})  # fmt: skip
    assert (speed(audio), speed(magi)) == (1.5, 1)
    # Positions saved without a speed (the web reader, an older app) leave it alone.
    put_json(client, f"/api/works/{audio}/progress", csrf, {"source": "audio", "audio_s": 20})
    put_json(client, f"/api/works/{magi}/progress", csrf, {"source": "web", "sentence_n": 9})
    assert (speed(audio), speed(magi)) == (1.5, 1)
    for bad in (0.2, 5, "fast"):
        body = {"source": "audio", "audio_s": 20, "speed": bad}
        assert put_json(client, f"/api/works/{audio}/progress", csrf, body).status_code == 422
    assert speed(audio) == 1.5

    # A replay from the offline queue carries its speed; one that loses to a newer position
    # from another device changes neither.
    def replay(recorded, value):
        body = {"source": "audio", "audio_s": 30, "speed": value,
                "offline": {"recorded_at": recorded, "sent_at": "2099-01-01T00:00:00Z"}}  # fmt: skip
        return put_json(client, f"/api/works/{audio}/progress", csrf, body)

    assert replay("2000-01-01T00:00:00Z", 3).status_code == 409
    assert speed(audio) == 1.5
    assert replay("2099-01-01T00:00:00Z", 1.75).status_code == 200
    assert speed(audio) == 1.75

    # Per user: another member starts with none and sets their own.
    make_user(client, csrf, "mia", "member")
    mia = login(client, "mia", "mia-correct horse")
    assert speed(audio) is None
    put_json(client, f"/api/works/{audio}/progress", mia,
             {"source": "audio", "audio_s": 5, "speed": 2})  # fmt: skip
    assert speed(audio) == 2
    # A reader who cannot see the book can neither read nor set a speed for it.
    owner_csrf = login(client, "owner", OWNER_PASSWORD)
    assert speed(audio) == 1.75
    make_user(client, owner_csrf, "rex", "reader")
    rex = login(client, "rex", "rex-correct horse")
    assert client.get(f"/api/works/{audio}/progress").status_code == 404
    r = put_json(client, f"/api/works/{audio}/progress", rex,
                 {"source": "audio", "audio_s": 5, "speed": 3})  # fmt: skip
    assert r.status_code == 404
    login(client, "owner", OWNER_PASSWORD)
    assert speed(audio) == 1.75
    with session_factory()() as db:
        assert {(p.work_id, p.speed) for p in db.query(WorkProgress)} == {
            (audio, 1.75),
            (magi, 1.0),
            (audio, 2.0),
        }
