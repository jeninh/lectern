"""Audiobook ingest (M4B and multi-file MP3), matching, range streaming, data-saver renditions,
playback auditing, and the sleep timer's rewind point."""

from __future__ import annotations

import base64
import shutil

import pytest
from tests.conftest import (
    MAGI,
    api_post,
    drain_inbox,
    make_app_password,
    make_epub,
    make_m4b,
    make_mp3,
    needs_ffmpeg,
)

from lectern.db import session_factory
from lectern.jobs import run_pending
from lectern.models import AudioTrack, MatchSuggestion

pytestmark = needs_ffmpeg


def _work(client, title):
    return next(w for w in client.get("/api/library").json()["works"] if w["title"] == title)


def test_multi_file_mp3_folder_becomes_one_timeline(client, owner, settings):
    folder = settings.inbox_path / "Some Book (MP3)"
    folder.mkdir(parents=True)
    # File names deliberately disagree with track numbers: tags decide the order.
    for name, track, secs in (("a.mp3", 3, 1.5), ("b.mp3", 1, 2.0), ("c.mp3", 2, 1.0)):
        make_mp3(folder / name, secs, title=f"Part {track}", album="Some Book (Unabridged)",
                 artist="Ann Writer", track=track, freq=300 + track * 50)  # fmt: skip
    drain_inbox(settings)
    assert not folder.exists()  # emptied folders are removed
    work = _work(client, "Some Book")
    assert work["author"] == "Ann Writer"
    assert work["audio"]["tracks"] == 3 and work["audio"]["duration_s"] == pytest.approx(
        4.5, abs=0.2
    )
    tr = client.get(f"/api/works/{work['id']}/tracks").json()
    assert [t["chapters"][0]["title"] for t in tr["tracks"]] == ["Part 1", "Part 2", "Part 3"]
    assert [t["position"] for t in tr["tracks"]] == [0, 1, 2]
    offsets = [t["offset_s"] for t in tr["tracks"]]
    durations = [t["duration_s"] for t in tr["tracks"]]
    assert offsets[0] == 0 and offsets[1] == pytest.approx(durations[0], abs=0.01)
    assert offsets[2] == pytest.approx(durations[0] + durations[1], abs=0.01)
    assert tr["total_duration_s"] == pytest.approx(sum(durations), abs=0.01)
    assert all(t["codec"] == "mp3" and t["media_type"] == "audio/mpeg" for t in tr["tracks"])
    assert tr["alignment"] is None and tr["tracks"][0]["variants"] == {"low": None}

    # A late file of the same book (e.g. a separate browser upload) slots into its place.
    make_mp3(settings.inbox_path / "late.mp3", 1.0, title="Part 0", album="Some Book",
             artist="Ann Writer", track=0, freq=900)  # fmt: skip
    # The same file twice is a duplicate.
    shutil.copy(folder.parent / "late.mp3", settings.inbox_path / "late-copy.mp3")
    drain_inbox(settings)
    tr = client.get(f"/api/works/{work['id']}/tracks").json()
    assert [t["chapters"][0]["title"] for t in tr["tracks"]] == [
        "Part 0",
        "Part 1",
        "Part 2",
        "Part 3",
    ]
    assert len(list((settings.inbox_path / "_rejected").glob("late*.mp3"))) == 1
    assert len(client.get("/api/library").json()["works"]) == 1


def test_m4b_chapters_isbn_match_and_cover_from_epub(client, owner, settings, tmp_path):
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    make_epub(settings.inbox_path / "dune.epub", title="Dune", authors=["Frank Herbert"],
              isbn="9780441172719", cover=True)  # fmt: skip
    drain_inbox(settings)
    # The narrator is in the artist tag and the title has edition noise, but the ISBN decides.
    make_m4b(settings.inbox_path / "dune.m4b", [("Opening", 0.0), ("Book One", 1.5), ("Book Two", 3.0)],
             4.0, title="DUNE [Audiobook]", artist="Scott Brick", comment="ISBN 978-0-441-17271-9")  # fmt: skip
    drain_inbox(settings)
    works = client.get("/api/library").json()["works"]
    assert len(works) == 1 and works[0]["title"] == "Dune"
    tr = client.get(f"/api/works/{works[0]['id']}/tracks").json()
    (track,) = tr["tracks"]
    assert track["codec"] == "aac" and track["media_type"] == "audio/mp4"
    assert [c["title"] for c in track["chapters"]] == ["Opening", "Book One", "Book Two"]
    assert [round(c["start_s"], 1) for c in track["chapters"]] == [0.0, 1.5, 3.0]
    # A second complete audiobook for the same work is refused, not interleaved.
    make_m4b(settings.inbox_path / "dune2.m4b", [("A", 0.0), ("B", 1.0)], 2.0,
             title="Dune", artist="Frank Herbert", comment="9780441172719")  # fmt: skip
    drain_inbox(settings)
    assert (settings.inbox_path / "_rejected" / "dune2.m4b").exists()


def test_uncertain_match_asks_and_merge_moves_the_audio(client, owner, settings):
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    make_epub(settings.inbox_path / "magi.epub", title="The Gift of the Magi", authors=["O. Henry"])
    drain_inbox(settings)
    # Same title, but the artist tag names the narrator: not certain -> a suggestion.
    make_mp3(settings.inbox_path / "magi.mp3", 1.0, title="The Gift of the Magi",
             album="The Gift of the Magi", artist="Betsie Bush")  # fmt: skip
    drain_inbox(settings)
    works = client.get("/api/library").json()["works"]
    assert len(works) == 2
    sugg = client.get("/api/suggestions").json()["suggestions"]
    assert len(sugg) == 1
    s = sugg[0]
    assert s["candidate"]["title"] == "The Gift of the Magi" and s["candidate"]["audio"] is None
    assert s["work"]["audio"]["tracks"] == 1 and "author" in s["reason"]

    r = api_post(client, f"/api/suggestions/{s['id']}/merge", owner["csrf"])
    assert r.status_code == 200
    works = client.get("/api/library").json()["works"]
    assert len(works) == 1 and works[0]["audio"]["tracks"] == 1 and works[0]["files"]
    target = works[0]
    tr = client.get(f"/api/works/{target['id']}/tracks").json()["tracks"]
    assert client.get(tr[0]["stream_url"]).status_code == 200  # file moved with the row
    assert client.get("/api/suggestions").json()["suggestions"] == []
    assert not (settings.library_dir / s["work"]["dir_path"]).exists()

    # Later files of the same audiobook now go straight to the merged work.
    make_mp3(settings.inbox_path / "magi2.mp3", 1.0, title="The Gift of the Magi",
             album="The Gift of the Magi", artist="Betsie Bush", track=2, freq=600)  # fmt: skip
    drain_inbox(settings)
    assert len(client.get("/api/library").json()["works"]) == 1
    assert client.get(f"/api/works/{target['id']}").json()["audio"]["tracks"] == 2

    # Dismissing keeps works apart.
    make_mp3(settings.inbox_path / "other.mp3", 1.0, album="The Gift of the Magus",
             artist="Someone Else", freq=700)  # fmt: skip
    drain_inbox(settings)
    (s2,) = client.get("/api/suggestions").json()["suggestions"]
    assert (
        api_post(client, f"/api/suggestions/{s2['id']}/dismiss", owner["csrf"]).status_code == 200
    )
    assert len(client.get("/api/library").json()["works"]) == 2
    with session_factory()() as db:
        assert db.query(MatchSuggestion).one().resolution == "dismissed"


@pytest.fixture
def magi_track(client, owner, settings):
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    shutil.copy(MAGI / "excerpt.mp3", settings.inbox_path / "excerpt.mp3")
    drain_inbox(settings)
    work = client.get("/api/library").json()["works"][0]
    track = client.get(f"/api/works/{work['id']}/tracks").json()["tracks"][0]
    return work, track


def test_range_streaming_semantics(client, owner, magi_track):
    work, track = magi_track
    url = track["stream_url"]
    size = track["size"]
    body = (MAGI / "excerpt.mp3").read_bytes()
    assert size == len(body)

    r = client.get(url)
    assert r.status_code == 200 and r.content == body
    assert r.headers["accept-ranges"] == "bytes" and r.headers["content-length"] == str(size)
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.headers["etag"] == f'"{track["sha256"]}"'
    assert r.headers["cache-control"] == "private, max-age=3600" and "last-modified" in r.headers

    r = client.get(url, headers={"range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == body[100:200]
    assert (
        r.headers["content-range"] == f"bytes 100-199/{size}"
        and r.headers["content-length"] == "100"
    )
    r = client.get(url, headers={"range": "bytes=1000-"})
    assert r.status_code == 206 and r.content == body[1000:]
    r = client.get(url, headers={"range": "bytes=-10"})
    assert r.status_code == 206 and r.content == body[-10:]
    r = client.get(url, headers={"range": f"bytes={size - 5}-{size + 500}"})  # end is clamped
    assert r.status_code == 206 and r.content == body[-5:]
    assert r.headers["content-range"] == f"bytes {size - 5}-{size - 1}/{size}"

    for unsatisfiable in (f"bytes={size}-", "bytes=-0"):
        r = client.get(url, headers={"range": unsatisfiable})
        assert r.status_code == 416 and r.headers["content-range"] == f"bytes */{size}"
    for ignored in ("bytes=0-1,5-9", "items=0-5", "bytes=9-2"):
        r = client.get(url, headers={"range": ignored})
        assert r.status_code == 200 and len(r.content) == size

    etag = r.headers["etag"]
    assert client.get(url, headers={"if-none-match": etag}).status_code == 304
    r = client.get(url, headers={"range": "bytes=0-9", "if-range": etag})
    assert r.status_code == 206
    r = client.get(url, headers={"range": "bytes=0-9", "if-range": '"stale"'})
    assert r.status_code == 200 and len(r.content) == size

    # Streaming is not a download: it is neither audited nor counted.
    actions = [e["action"] for e in client.get("/api/activity").json()["events"]]
    assert "download" not in actions
    assert client.get("/library/tracks/999999/stream").status_code == 404


def test_stream_accepts_app_password_and_hides_from_readers(client, owner, magi_track, settings):
    _work_, track = magi_track
    secret = make_app_password(client, owner["csrf"], "Phone app")
    basic = base64.b64encode(f"owner:{secret}".encode()).decode()
    client.cookies.clear()
    r = client.get(
        track["stream_url"], headers={"authorization": f"Basic {basic}", "range": "bytes=0-1"}
    )
    assert r.status_code == 206
    assert client.get(track["stream_url"]).status_code == 401
    bad = base64.b64encode(b"owner:wrong").decode()
    assert (
        client.get(track["stream_url"], headers={"authorization": f"Basic {bad}"}).status_code
        == 401
    )


def test_original_download_and_data_saver_rendition(client, owner, magi_track, settings):
    work, track = magi_track
    r = client.get(track["download_url"])
    assert r.status_code == 200 and r.content == (MAGI / "excerpt.mp3").read_bytes()
    assert "attachment" in r.headers["content-disposition"]
    assert "download" in [e["action"] for e in client.get("/api/activity").json()["events"]]

    r = client.get(track["stream_url"] + "?variant=low")
    assert r.status_code == 404 and r.json()["detail"] == "variant_unavailable"
    r = api_post(client, f"/api/works/{work['id']}/transcode", owner["csrf"])
    assert r.status_code == 200 and r.json()["status"] == "queued"
    again = api_post(client, f"/api/works/{work['id']}/transcode", owner["csrf"])
    assert again.json()["job_id"] == r.json()["job_id"]  # one live job per work
    assert client.get(f"/api/works/{work['id']}/tracks").json()["transcode"]["status"] == "queued"
    assert run_pending(session_factory(), settings) == 1

    tr = client.get(f"/api/works/{work['id']}/tracks").json()
    low = tr["tracks"][0]["variants"]["low"]
    assert low["codec"] == settings.audio_low_codec == "aac" and low["bitrate_kbps"] == 64
    assert tr["transcode"] is None
    v = client.get(low["stream_url"])
    assert v.status_code == 200 and v.headers["content-type"] == "audio/mp4"
    assert v.headers["etag"] != f'"{track["sha256"]}"' and len(v.content) == low["size"]
    import hashlib

    assert v.headers["etag"] == f'"{hashlib.sha256(v.content).hexdigest()}"'
    assert client.get(low["stream_url"], headers={"range": "bytes=0-9"}).status_code == 206
    derived = settings.library_dir / work["dir_path"] / "derived" / "audio"
    assert (derived / f"{track['sha256']}.m4a").is_file()
    # The original is untouched and still served by default.
    assert client.get(track["stream_url"]).content == (MAGI / "excerpt.mp3").read_bytes()
    # Duration is preserved (no trimming), so sentence timings stay valid.
    from lectern.ingest.audio import probe

    assert probe(derived / f"{track['sha256']}.m4a").duration_s == pytest.approx(
        track["duration_s"], abs=0.2
    )
    assert api_post(client, f"/api/works/{work['id']}/transcode", owner["csrf"]).json() == {
        "status": "ready"
    }


def test_listen_audit_once_per_session_and_listening_list(client, owner, magi_track):
    work, _ = magi_track
    assert api_post(client, f"/api/works/{work['id']}/listen", owner["csrf"]).json() == {
        "recorded": True
    }
    assert api_post(client, f"/api/works/{work['id']}/listen", owner["csrf"]).json() == {
        "recorded": False
    }
    items = client.get("/api/listening").json()["items"]
    assert [w["id"] for w in items] == [work["id"]] and items[0]["audio"]["tracks"] == 1


def test_sleep_timer_rewinds_to_the_sentence_start(client, owner, magi_track):
    work, _ = magi_track
    # The fixture's hand-checked sentences start at 35.89 s and 44.48 s (sentence_map.json).
    r = client.get(f"/api/works/{work['id']}/rewind-point", params={"t": 40.0}).json()
    assert r["method"] == "silence" and r["t"] == pytest.approx(35.89, abs=0.15)
    r = client.get(f"/api/works/{work['id']}/rewind-point", params={"t": 47.0}).json()
    assert r["t"] == pytest.approx(44.48, abs=0.15)


def test_track_rows_after_ingest_are_consistent(client, owner, settings, magi_track):
    with session_factory()() as db:
        (t,) = db.query(AudioTrack).all()
        assert t.rel_path.startswith("audio/D01-T000 ") and t.offset_s == 0.0
        assert (settings.library_dir / t.work.dir_path / t.rel_path).is_file()
