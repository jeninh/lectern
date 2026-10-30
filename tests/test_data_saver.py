"""Data-saver renditions (issue #18; docs/contracts.md, "Optional low-bitrate variant"):
scheduling between a transcoding worker and the server's own throttled ffmpeg, the message
when neither exists, timestamps that survive the re-encode, and the owner's switches.

Real ffmpeg, a real app over HTTP, and the real ``lectern worker`` transcode runner.
"""

from __future__ import annotations

import subprocess
from datetime import timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.conftest import ORIGIN, api_delete, api_post, drain_inbox, needs_ffmpeg

from lectern import jobs as transcode
from lectern.align.worker import RemoteWorker
from lectern.db import session_factory
from lectern.models import Job, JobStatus, Work, WorkerCredential, utcnow
from lectern.worker.runners import TranscodeRunner

pytestmark = needs_ffmpeg

# Where each track's click is, in seconds into the file (original timeline).
CLICKS = (1.30, 0.70)
# Documented in docs/contracts.md: a rendition puts every sample within this of the original.
SEEK_TOLERANCE_S = 0.05


def _click_track(path, seconds: float, click_at: float, track: int) -> None:
    """Silence with a 50 ms 1 kHz burst at ``click_at``: an onset any decoder places exactly."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={click_at}",
         "-f", "lavfi", "-i", "sine=f=1000:r=44100:d=0.05",
         "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={seconds - click_at - 0.05}",
         "-filter_complex", "[1]aformat=channel_layouts=stereo[s];[0][s][2]concat=n=3:v=0:a=1",
         "-c:a", "libmp3lame", "-b:a", "128k",
         "-metadata", "album=Clicks", "-metadata", "artist=Ann Writer",
         "-metadata", f"title=Part {track}", "-metadata", f"track={track}", str(path)],
        check=True,
    )  # fmt: skip


def onset(path) -> float:
    """Seconds to the first loud sample, as a player decodes it (edit lists and Ogg pre-skip
    applied by ffmpeg, as browsers do)."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "44100", "-f", "s16le", "-"],
        capture_output=True,
        check=True,
    ).stdout
    samples = np.abs(np.frombuffer(raw, dtype=np.int16).astype(np.int32))
    return int(np.argmax(samples > 1000)) / 44100


@pytest.fixture
def book(client, owner, settings) -> dict:
    """A two-track audiobook, each track with a click at a known place."""
    folder = settings.inbox_path / "Clicks"
    folder.mkdir(parents=True)
    for i, at in enumerate(CLICKS, start=1):
        _click_track(folder / f"{i}.mp3", 3.0, at, i)
    drain_inbox(settings)
    work = next(w for w in client.get("/api/library").json()["works"] if w["title"] == "Clicks")
    tracks = client.get(f"/api/works/{work['id']}/tracks").json()["tracks"]
    assert len(tracks) == 2
    return {"id": work["id"], "dir": settings.library_dir / work["dir_path"], "tracks": tracks}


@pytest.fixture
def worker(app, client, owner):
    key = api_post(client, "/api/worker-credentials", owner["csrf"], {"label": "mac"}).json()
    return TestClient(app, headers={"Authorization": f"Bearer {key['secret']}"})


def make(client, owner, work_id):
    return api_post(client, f"/api/works/{work_id}/transcode", owner["csrf"])


def the_job() -> Job:
    with session_factory()() as db:
        return db.scalar(select(Job).where(Job.kind == "transcode").order_by(Job.id.desc()))


def lows(client, work_id) -> list:
    return [
        t["variants"]["low"] for t in client.get(f"/api/works/{work_id}/tracks").json()["tracks"]
    ]


# --- scheduling -------------------------------------------------------------------------------


def test_a_connected_worker_makes_the_copy_and_the_pi_leaves_it_alone(
    client, owner, settings, book, worker
):
    assert worker.post("/api/worker/status", json={"state": "ready", "capabilities": ["transcode"]}
                       ).status_code == 200  # fmt: skip
    assert make(client, owner, book["id"]).json()["status"] == "queued"
    # The server could transcode (ffmpeg is here) but a worker that can is online: it waits.
    assert transcode.run_pending(session_factory(), settings) == 0
    assert the_job().status == JobStatus.QUEUED

    agent = RemoteWorker("http://testserver", "", client=worker, runners=[TranscodeRunner()])
    assert agent.run_once()
    job = the_job()
    assert job.status == JobStatus.SUCCEEDED and job.result == {"done": 2, "total": 2}
    with session_factory()() as db:
        assert db.scalar(select(WorkerCredential.id)) == job.leased_by_id  # the worker made it
    assert all(lows(client, book["id"]))


def test_without_a_worker_the_pi_makes_it_one_throttled_track_at_a_time(
    client, owner, settings, book, monkeypatch
):
    calls = []
    real = subprocess.run

    def spy(args, **kw):
        calls.append((args, kw))
        return real(args, **kw)

    monkeypatch.setattr(transcode.subprocess, "run", spy)
    make(client, owner, book["id"])
    assert transcode.run_pending(session_factory(), settings) == 1
    job = the_job()
    assert job.status == JobStatus.SUCCEEDED and job.leased_by_id is None
    assert all(lows(client, book["id"]))
    assert len(calls) == 2  # one ffmpeg per track, in turn
    for args, kw in calls:
        assert args[args.index("-threads") + 1] == "1"
        assert kw["preexec_fn"] is not None


def test_the_throttle_lowers_priority_and_caps_memory():
    out = subprocess.run(
        ["sh", "-c", "nice; ulimit -v"],
        preexec_fn=transcode._throttle(512),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert out == ["19", str(512 * 1024)]


def test_a_paused_worker_or_a_long_wait_hands_the_job_to_the_pi(
    client, owner, settings, book, worker
):
    worker.post("/api/worker/status", json={"state": "ready", "capabilities": ["transcode"]})
    make(client, owner, book["id"])
    worker.post("/api/worker/status", json={"state": "paused", "info": {"reason": "on battery"}})
    assert transcode.run_pending(session_factory(), settings) == 1
    assert all(lows(client, book["id"]))

    # back, but a new job has waited past LECTERN_LOCAL_DEFER_SECONDS: the Pi runs it anyway
    api_delete(client, f"/api/works/{book['id']}/transcode", owner["csrf"])
    worker.post("/api/worker/status", json={"state": "ready", "capabilities": ["transcode"]})
    make(client, owner, book["id"])
    assert transcode.run_pending(session_factory(), settings) == 0
    with session_factory()() as db:
        db.get(Job, the_job().id).created_at = utcnow() - timedelta(
            seconds=settings.local_defer_seconds + 1
        )
        db.commit()
    assert transcode.run_pending(session_factory(), settings) == 1


def test_with_no_transcoder_anywhere_it_says_so_instead_of_queuing(
    client, owner, settings, book, worker, monkeypatch
):
    monkeypatch.setattr(settings, "ffmpeg_path", "/nonexistent/ffmpeg")
    r = make(client, owner, book["id"])
    assert r.status_code == 503 and r.json()["detail"] == "no_transcoder"
    assert the_job() is None
    ds = client.get(f"/api/works/{book['id']}/tracks").json()["data_saver"]
    assert "cannot be made" in ds["unavailable"]["message"]

    # A worker that can transcode connects: the job is queued (and the Pi, without ffmpeg,
    # never takes it).
    worker.post("/api/worker/status", json={"state": "ready", "capabilities": ["transcode"]})
    assert make(client, owner, book["id"]).json()["status"] == "queued"
    assert client.get(f"/api/works/{book['id']}/tracks").json()["transcode"]["message"] is None
    assert transcode.run_pending(session_factory(), settings) == 0
    # It goes away before claiming: the waiting job says why it is not moving.
    with session_factory()() as db:
        for cred in db.scalars(select(WorkerCredential)):
            cred.last_used_at = utcnow() - timedelta(hours=1)
        db.commit()
    tr = client.get(f"/api/works/{book['id']}/tracks").json()
    assert tr["transcode"]["status"] == "queued"
    assert "no worker that can transcode" in tr["transcode"]["message"]


# --- the same timeline ------------------------------------------------------------------------


@pytest.mark.parametrize("codec", ["aac", "opus"])
def test_a_rendition_keeps_every_track_boundary_and_timestamp(
    client, owner, settings, book, codec, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "audio_low_codec", codec)
    make(client, owner, book["id"])
    assert transcode.run_pending(session_factory(), settings) == 1
    tracks = client.get(f"/api/works/{book['id']}/tracks").json()["tracks"]
    from lectern.ingest.audio import probe

    for t, at in zip(tracks, CLICKS, strict=True):
        original = tmp_path / f"{t['id']}.mp3"
        original.write_bytes(client.get(t["stream_url"]).content)
        low = book["dir"] / "derived" / "audio" / f"{t['sha256']}.{transcode.EXT[codec]}"
        assert t["variants"]["low"]["codec"] == codec
        assert onset(original) == pytest.approx(at, abs=0.01)
        assert abs(onset(low) - onset(original)) <= SEEK_TOLERANCE_S
        assert probe(low).duration_s == pytest.approx(t["duration_s"], abs=SEEK_TOLERANCE_S)
    # Track offsets on the work timeline (progress, sentences, read-along) do not depend on
    # which rendition plays: they are the originals' and unchanged.
    assert [t["offset_s"] for t in tracks] == [t["offset_s"] for t in book["tracks"]]


# --- serving, switches, deletion --------------------------------------------------------------


def test_data_saver_streams_with_ranges_and_deleting_falls_back_to_the_original(
    client, owner, settings, book
):
    make(client, owner, book["id"])
    transcode.run_pending(session_factory(), settings)
    t = client.get(f"/api/works/{book['id']}/tracks").json()
    low = t["tracks"][0]["variants"]["low"]
    assert t["data_saver"]["tracks_ready"] == 2
    assert 0 < t["data_saver"]["low_bytes"] < t["data_saver"]["original_bytes"]
    r = client.get(low["stream_url"], headers={"range": "bytes=10-19"})
    assert r.status_code == 206 and len(r.content) == 10
    assert r.headers["content-range"] == f"bytes 10-19/{low['size']}"
    assert r.headers["content-type"] == "audio/mp4"
    original = client.get(t["tracks"][0]["stream_url"], headers={"range": "bytes=0-3"})
    assert original.headers["content-type"] == "audio/mpeg"

    freed = api_delete(client, f"/api/works/{book['id']}/transcode", owner["csrf"]).json()
    assert freed["deleted_bytes"] == t["data_saver"]["low_bytes"]
    assert lows(client, book["id"]) == [None, None]
    assert client.get(low["stream_url"]).status_code == 404
    # the originals are exactly what was ingested
    from lectern.ingest.hashing import sha256_file

    assert sorted(sha256_file(p) for p in book["dir"].rglob("*.mp3")) == sorted(
        x["sha256"] for x in book["tracks"]
    )
    # regenerable
    make(client, owner, book["id"])
    transcode.run_pending(session_factory(), settings)
    assert all(lows(client, book["id"]))


def test_the_owner_turns_copies_off_for_the_library_or_one_book(client, owner, settings, book):
    csrf = owner["csrf"]
    make(client, owner, book["id"])  # queued, then the library switch goes off
    r = client.put("/api/library-settings", json={"data_saver_enabled": False},
                   headers={"origin": ORIGIN, "x-csrf-token": csrf})  # fmt: skip
    assert r.status_code == 200 and r.json()["settings"]["data_saver_enabled"] is False
    assert the_job().status == JobStatus.CANCELLED
    assert transcode.run_pending(session_factory(), settings) == 0
    r = make(client, owner, book["id"])
    assert r.status_code == 409 and r.json()["detail"] == "data_saver_off"
    admin = client.get("/api/admin/data-saver").json()
    assert admin["enabled"] is False and admin["audiobooks"] == 1 and admin["with_copies"] == 0

    client.put("/api/library-settings", json={"data_saver_enabled": True},
               headers={"origin": ORIGIN, "x-csrf-token": csrf})  # fmt: skip
    make(client, owner, book["id"])
    version = client.get(f"/api/works/{book['id']}/edit").json()["version"]
    r = client.patch(f"/api/works/{book['id']}", json={"version": version, "data_saver_enabled": False},
                     headers={"origin": ORIGIN, "x-csrf-token": csrf})  # fmt: skip
    assert r.status_code == 200, r.text
    assert the_job().status == JobStatus.CANCELLED
    assert client.get(f"/api/works/{book['id']}/edit").json()["data_saver_enabled"] is False
    r = make(client, owner, book["id"])
    assert r.status_code == 409 and r.json()["detail"] == "data_saver_off_for_work"
    # automatic copies after import respect it too
    with session_factory()() as db:
        from lectern.models import Work

        assert transcode.auto_enqueue(db, settings, db.get(Work, book["id"])) is None


def test_a_job_cancelled_mid_run_publishes_no_more_tracks(
    client, owner, settings, book, monkeypatch
):
    make(client, owner, book["id"])
    real = transcode.transcode_track

    def then_cancel(*a, **kw):
        real(*a, **kw)
        with session_factory()() as db:
            transcode.cancel_transcode(db, db.get(Job, the_job().id))
            db.commit()

    monkeypatch.setattr(transcode, "transcode_track", then_cancel)
    transcode.run_pending(session_factory(), settings)
    assert the_job().status == JobStatus.CANCELLED
    assert lows(client, book["id"]) == [None, None]  # the first finished after the cancel


def test_only_the_owner_or_who_started_it_may_stop_a_copy_being_made(client, owner, settings, book):
    """Issue #117 review: deleting the copies cancels the transcode in progress, so it follows
    the cancel rule (POST /api/jobs/{id}/cancel): the owner or whoever started it."""
    from tests.conftest import login, make_user

    mia = make_user(client, owner["csrf"], "mia", "member")
    m = TestClient(client.app)
    m_csrf = login(m, "mia", mia["password"])
    url = f"/api/works/{book['id']}/transcode"
    with session_factory()() as db:  # queued automatically after the import: no starter
        auto = transcode.enqueue_transcode(db, settings, db.get(Work, book["id"]))
        db.commit()
        auto_id = auto.id
    r = api_delete(m, url, m_csrf)
    assert (r.status_code, r.json()["detail"]) == (403, "not_your_job")
    assert the_job().status == JobStatus.QUEUED

    assert api_delete(client, url, owner["csrf"]).status_code == 200  # the owner may
    with session_factory()() as db:
        assert db.get(Job, auto_id).status == JobStatus.CANCELLED
    assert api_post(m, url, m_csrf).json()["status"] == "queued"  # hers: she may stop it
    assert api_delete(m, url, m_csrf).status_code == 200
    assert the_job().status == JobStatus.CANCELLED


def test_cancelling_a_stale_copy_never_undoes_a_finished_job(client, owner, settings, book):
    make(client, owner, book["id"])
    with session_factory()() as stale:
        job = stale.get(Job, the_job().id)  # loaded while queued
        assert job.status == JobStatus.QUEUED
        assert transcode.run_pending(session_factory(), settings) == 1
        transcode.cancel_transcode(stale, job)
        stale.commit()
    assert the_job().status == JobStatus.SUCCEEDED


def test_the_account_default_round_trips(client, owner):
    assert client.get("/api/me").json()["preferences"]["data_saver"] is False
    r = api_post(client, "/api/me/preferences", owner["csrf"], {"data_saver": True})
    assert r.json()["preferences"]["data_saver"] is True
    assert client.get("/api/me").json()["preferences"]["data_saver"] is True


def test_a_transcode_waits_for_a_free_heavy_job_slot(client, owner, settings, book, monkeypatch):
    from lectern import limits

    base = limits.Limits.from_settings(settings)
    gov = limits.Governor(limits.Limits(1, base.features))
    monkeypatch.setattr(limits, "GOVERNOR", gov)
    make(client, owner, book["id"])
    with gov.gate(limits.KOKORO) as gate:
        narration = gate.admit(limits.KOKORO, 1)  # one heavy job (the default cap) is running
    assert transcode.run_pending(session_factory(), settings) == 0
    assert the_job().status == JobStatus.QUEUED
    narration.release()
    assert transcode.run_pending(session_factory(), settings) == 1
    assert the_job().status == JobStatus.SUCCEEDED
    assert gov.snapshot()["heavy_jobs"]["active"] == 0  # the slot was given back
