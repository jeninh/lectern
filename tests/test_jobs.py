"""Job queue and the external-worker protocol (docs/contracts.md, "Worker protocol").

The worker side runs over HTTP against the real app with a worker credential; alignment uses
the recorded faster-whisper transcript of the fixture, so no model is needed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from tests.conftest import (
    ORIGIN,
    api_post,
    login,
    make_app_password,
    make_sync_key,
    make_user,
)

from lectern.align import jobs
from lectern.align.pipeline import TrackInput, align_work
from lectern.align.transcribe import Word
from lectern.align.worker import LocalWorker, RemoteWorker
from lectern.cli import main as cli_main
from lectern.db import session_factory
from lectern.ingest.epub import read_epub_metadata
from lectern.ingest.hashing import partial_md5, sha256_file
from lectern.ingest.library import ingest_epub
from lectern.models import (
    Alignment,
    AlignmentStatus,
    Job,
    JobStatus,
    Sentence,
    Work,
    utcnow,
)
from lectern.sentence_map import load_sentence_map, map_path

FIXTURE = Path(__file__).parent / "fixtures" / "magi"
EPUB = FIXTURE / "the-gift-of-the-magi.epub"
EXCERPT = FIXTURE / "excerpt.mp3"
TRANSCRIPT = json.loads((FIXTURE / "transcript_small.json").read_text())


def recorded(_tracks, _language, progress):
    progress(0.5)
    return [Word(*w) for w in TRANSCRIPT["words"]], TRANSCRIPT["language"]


def ingest_fixture(settings, tmp_path: Path, *, audio: bool = True) -> int:
    """The fixture EPUB (and excerpt as the work's only track) in the library."""
    src = tmp_path / "magi.epub"
    shutil.copy(EPUB, src)
    with session_factory()() as db:
        meta = read_epub_metadata(src)
        work, _, _ = ingest_epub(
            db, settings.library_dir, src, meta, sha256_file(src), partial_md5(src)
        )
        if audio:
            shutil.copy(EXCERPT, settings.library_dir / work.dir_path / "01 excerpt.mp3")
        db.commit()
        return work.id


@pytest.fixture
def work_id(client, owner, settings, tmp_path) -> int:
    return ingest_fixture(settings, tmp_path)


@pytest.fixture
def worker_key(client, owner) -> str:
    r = api_post(client, "/api/worker-credentials", owner["csrf"], {"label": "gpu-box"})
    assert r.status_code == 201, r.text
    assert r.json()["secret"].startswith("lw_")
    return r.json()["secret"]


@pytest.fixture
def wc(app, worker_key) -> TestClient:
    """A cookie-less client that carries only the worker credential."""
    return TestClient(app, headers={"Authorization": f"Bearer {worker_key}"})


def request_align(client, owner, work_id, **body):
    return api_post(client, f"/api/works/{work_id}/align", owner["csrf"], body)


def claim(wc, **body):
    return wc.post("/api/worker/jobs/claim", json={"worker": "gpu-box", **body})


def lease(r) -> dict:
    assert r.status_code == 200, r.text
    return {"X-Lease-Token": r.json()["lease"]["token"]}


def sentence_map_body(work_id: int, **override) -> bytes:
    out = align_work(
        work_id=work_id,
        epub_path=EPUB,
        epub_sha256=sha256_file(EPUB),
        tracks=[TrackInput(0, EXCERPT, sha256_file(EXCERPT), _excerpt_duration(), 0.0)],
        model_label="recorded-small",
        transcribe=recorded,
    )
    data = json.loads(out.sentence_map.model_dump_json())
    data.update(override)
    return json.dumps(data).encode(), out.stats


def _excerpt_duration() -> float:
    from lectern.align.tracks import probe

    return probe(EXCERPT)[0]


def put_result(wc, job_id, headers, body):
    return wc.put(
        f"/api/worker/jobs/{job_id}/result",
        content=body,
        headers={**headers, "Content-Type": "application/json"},
    )


def db_job(job_id: int) -> Job:
    with session_factory()() as db:
        return db.get(Job, job_id)


def set_job(job_id: int, **values) -> None:
    with session_factory()() as db:
        job = db.get(Job, job_id)
        for k, v in values.items():
            setattr(job, k, v)
        db.commit()


# --- the whole protocol -------------------------------------------------------------------


def test_worker_protocol_end_to_end(client, owner, work_id, wc, settings):
    r = request_align(client, owner, work_id)
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "queued" and job["model"] == "small"
    assert job["eta"] is None  # nothing measured yet: no made-up number
    again = request_align(client, owner, work_id)
    assert again.status_code == 200 and again.json()["id"] == job["id"]

    c = claim(wc, lease_s=5)  # clamped to the 60 s minimum
    headers = lease(c)
    body = c.json()
    assert body["lease"]["lease_s"] == 60 and body["job"]["attempt"] == 1
    payload = body["job"]["payload"]
    assert payload["epub_sha256"] == sha256_file(EPUB)
    assert [t["sha256"] for t in payload["tracks"]] == [sha256_file(EXCERPT)]
    assert claim(wc).status_code == 204  # nothing else runnable

    # authenticated, hash-checked, range-capable inputs
    epub = wc.get(body["inputs"]["epub"], headers=headers)
    assert epub.status_code == 200
    assert hashlib.sha256(epub.content).hexdigest() == payload["epub_sha256"]
    assert epub.headers["x-content-sha256"] == payload["epub_sha256"]
    part = wc.get(body["inputs"]["tracks"][0], headers={**headers, "Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert part.content == EXCERPT.read_bytes()[:100]
    assert wc.get(body["inputs"]["epub"]).status_code == 409  # no lease token

    map_body, stats = sentence_map_body(work_id)
    stats = {**stats, "speed": 1.8}  # what a CPU worker measures (docs/alignment.md)
    hb = wc.post(
        f"/api/worker/jobs/{job['id']}/heartbeat",
        json={"progress": 0.4, "message": "transcribing", "stats": stats},
        headers=headers,
    )
    assert hb.status_code == 200
    listed = client.get(f"/api/jobs/{job['id']}").json()
    assert listed["status"] == "leased"
    assert listed["lease"]["worker"] == "gpu-box" and listed["lease"]["progress"] == 0.4

    res = put_result(wc, job["id"], headers, map_body)
    assert res.status_code == 200, res.text
    assert res.json() == {
        "job_id": job["id"],
        "status": "succeeded",
        "sentence_count": 293,
        "coverage": pytest.approx(15 / 293, abs=1e-4),
    }
    # idempotent with the same lease token; any other token is lease_lost
    assert put_result(wc, job["id"], headers, map_body).json() == res.json()
    assert put_result(wc, job["id"], {"X-Lease-Token": "x" * 43}, map_body).status_code == 409

    # applied: the file, the rows, the alignment and its diagnosis
    assert load_sentence_map(map_path(settings.data_dir, work_id)).work_id == work_id
    sentences = client.get(f"/api/works/{work_id}/sentences", params={"from": 14, "to": 16})
    data = sentences.json()
    assert data["count"] == 293 and [s["n"] for s in data["sentences"]] == [14, 15]
    assert data["sentences"][0]["matched"] and data["sentences"][0]["audio_start_s"] < 1.0
    tracks = client.get(f"/api/works/{work_id}/tracks").json()
    al = tracks["alignment"]
    assert al["status"] == "succeeded" and al["warnings"] == ["low_coverage"]
    # an excerpt: the narrated span matched fully, so a bigger model is not suggested
    assert al["diagnosis"] == "partial_audio" and al["span_coverage"] == 1.0
    assert al["suggested_model"] is None
    assert (
        tracks["tracks"][0]["stream_url"] == f"/library/tracks/{tracks['tracks'][0]['id']}/stream"
    )
    t = client.get(f"/api/works/{work_id}/sentences", params={"t": 3.5, "around": 0}).json()
    assert [s["n"] for s in t["sentences"]] == [15]  # "That was all."

    # with one measured run, a new job gets an honest ETA from that run's speed
    r = request_align(client, owner, work_id, force=True)
    assert r.status_code == 202
    eta = r.json()["eta"]
    assert eta == {
        "seconds": round(_excerpt_duration() / 1.8),
        "speed": 1.8,
        "basis": "median of 1 measured run",
    }


# --- credentials: scope, isolation, revocation --------------------------------------------


def test_worker_routes_accept_only_worker_credentials(client, owner, app, worker_key):
    anon = TestClient(app)
    for headers in (
        {},
        {"Authorization": "Bearer not-a-worker-key"},
        {"Authorization": "Bearer lw_" + "a" * 43},
        {"Authorization": "Basic " + base64.b64encode(b"owner:x").decode()},
    ):
        r = anon.post("/api/worker/jobs/claim", json={}, headers=headers)
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Bearer")
    # a browser session (the owner's cookie) is not a worker credential
    assert client.post("/api/worker/jobs/claim", json={}).status_code == 401
    # an app password or sync key sent as a bearer token is not one either
    app_pw = make_app_password(client, owner["csrf"], "kobo")
    sync = make_sync_key(client, owner["csrf"], "kobo")
    for secret in (app_pw, sync):
        r = anon.post("/api/worker/jobs/claim", headers={"Authorization": f"Bearer {secret}"})
        assert r.status_code == 401
    # and a worker key opens nothing outside /api/worker
    bearer = {"Authorization": f"Bearer {worker_key}"}
    for url in ("/api/library", "/api/jobs", "/api/me", "/opds", "/api/worker-credentials"):
        assert anon.get(url, headers=bearer).status_code == 401, url


def test_only_the_owner_manages_credentials_and_secrets_are_hashed(client, owner, settings):
    r = api_post(
        client,
        "/api/admin/users",
        owner["csrf"],
        {"username": "member", "role": "member", "password": "member password 123"},
    )
    assert r.status_code == 201
    created = api_post(client, "/api/worker-credentials", owner["csrf"], {"label": "box"}).json()
    from lectern.models import WorkerCredential

    with session_factory()() as db:
        row = db.get(WorkerCredential, created["id"])
        assert row.key_hash == hashlib.sha256(created["secret"].encode()).hexdigest()
    listed = client.get("/api/worker-credentials").json()["credentials"]
    assert "secret" not in listed[0] and listed[0]["label"] == "box"
    # without CSRF a write is refused
    r = client.post("/api/worker-credentials", json={"label": "x"}, headers={"origin": ORIGIN})
    assert r.status_code == 403
    csrf = login(client, "member", "member password 123")
    assert api_post(client, "/api/worker-credentials", csrf, {"label": "m"}).status_code == 403
    assert client.get("/api/worker-credentials").status_code == 403


def test_revoking_a_credential_reclaims_its_lease_at_once(client, owner, work_id, wc, worker_key):
    job_id = request_align(client, owner, work_id).json()["id"]
    headers = lease(claim(wc))
    cred_id = client.get("/api/worker-credentials").json()["credentials"][0]["id"]
    r = client.delete(
        f"/api/worker-credentials/{cred_id}",
        headers={"origin": ORIGIN, "x-csrf-token": owner["csrf"]},
    )
    assert r.status_code == 200 and r.json()["revoked_at"]
    assert db_job(job_id).status == JobStatus.QUEUED
    assert db_job(job_id).lease_token_hash is None
    hb = wc.post(f"/api/worker/jobs/{job_id}/heartbeat", json={}, headers=headers)
    assert hb.status_code == 401  # the key itself no longer authenticates


# --- leases, expiry, retries ------------------------------------------------------------------


def test_cancel_and_wrong_tokens_lose_the_lease(client, owner, work_id, wc):
    job_id = request_align(client, owner, work_id).json()["id"]
    headers = lease(claim(wc))
    wrong = {"X-Lease-Token": "y" * 43}
    for path in ("heartbeat", "fail"):
        r = wc.post(f"/api/worker/jobs/{job_id}/{path}", json={"error": "x"}, headers=wrong)
        assert r.status_code == 409 and r.json() == {"detail": "lease_lost"}
    r = api_post(client, f"/api/jobs/{job_id}/cancel", owner["csrf"])
    assert r.json()["status"] == "cancelled"
    assert (
        wc.post(f"/api/worker/jobs/{job_id}/heartbeat", json={}, headers=headers).status_code == 409
    )
    assert wc.get(f"/api/worker/jobs/{job_id}/inputs/epub", headers=headers).status_code == 409
    body, _ = sentence_map_body(work_id)
    assert put_result(wc, job_id, headers, body).status_code == 409
    with session_factory()() as db:
        assert db.scalar(select(Alignment).where(Alignment.work_id == work_id)).status == (
            AlignmentStatus.FAILED
        )


def test_expired_lease_is_reclaimed_with_backoff_then_fails_terminally(client, owner, work_id, wc):
    job_id = request_align(client, owner, work_id).json()["id"]
    for attempt in (1, 2, 3):
        set_job(job_id, run_after=utcnow() - timedelta(seconds=1))
        c = claim(wc)
        headers = lease(c)
        assert c.json()["job"]["attempt"] == attempt
        set_job(job_id, lease_expires_at=utcnow() - timedelta(seconds=1))
        # an expired lease is lost even before anyone reclaims it
        assert (
            wc.post(f"/api/worker/jobs/{job_id}/heartbeat", json={}, headers=headers).status_code
            == 409
        )
        before = utcnow()
        assert claim(wc).status_code == 204  # reclaims, but the job now waits out its backoff
        job = db_job(job_id)
        assert job.error == "lease expired"
        if attempt < 3:
            assert job.status == JobStatus.QUEUED and job.lease_token_hash is None
            wait = (job.run_after - before).total_seconds()
            base = 30 * 2 ** (attempt - 1)
            assert base * 0.8 - 1 <= wait <= base * 1.2 + 1
    assert job.status == JobStatus.FAILED and job.finished_at is not None
    with session_factory()() as db:
        al = db.scalar(select(Alignment).where(Alignment.work_id == work_id))
        assert al.status == AlignmentStatus.FAILED and al.error == "lease expired"
    # failed jobs are not retried on their own
    set_job(job_id, run_after=utcnow() - timedelta(seconds=1))
    assert claim(wc).status_code == 204


def test_worker_failures_retry_or_end(client, owner, work_id, wc):
    job_id = request_align(client, owner, work_id).json()["id"]
    headers = lease(claim(wc))
    r = wc.post(
        f"/api/worker/jobs/{job_id}/fail",
        json={"error": "CUDA out of memory", "retryable": True},
        headers=headers,
    )
    assert r.status_code == 200 and r.json()["status"] == "queued" and r.json()["run_after"]
    assert client.get(f"/api/jobs/{job_id}").json()["error"] == "CUDA out of memory"
    assert claim(wc).status_code == 204  # backoff
    set_job(job_id, run_after=utcnow())
    headers = lease(claim(wc))
    r = wc.post(
        f"/api/worker/jobs/{job_id}/fail",
        json={"error": "unsupported codec", "retryable": False},
        headers=headers,
    )
    assert r.json() == {"status": "failed"}
    assert db_job(job_id).status == JobStatus.FAILED


def test_heartbeat_extends_the_lease(client, owner, work_id, wc):
    job_id = request_align(client, owner, work_id).json()["id"]
    headers = lease(claim(wc, lease_s=120))
    set_job(job_id, lease_expires_at=utcnow() + timedelta(seconds=5))
    hb = wc.post(f"/api/worker/jobs/{job_id}/heartbeat", json={"progress": 2}, headers=headers)
    assert hb.status_code == 422  # progress is a fraction
    hb = wc.post(f"/api/worker/jobs/{job_id}/heartbeat", json={"progress": 0.5}, headers=headers)
    assert hb.status_code == 200
    left = (db_job(job_id).lease_expires_at - utcnow()).total_seconds()
    assert 110 <= left <= 120


# --- result validation and staleness -------------------------------------------------------


def test_result_validation(client, owner, work_id, wc):
    job_id = request_align(client, owner, work_id).json()["id"]
    headers = lease(claim(wc))
    r = put_result(wc, job_id, headers, b"{not json")
    assert r.status_code == 400 and r.json()["detail"] == "invalid_map"
    body, _ = sentence_map_body(work_id)
    data = json.loads(body)
    data["sentences"][3]["n"] = 7
    r = put_result(wc, job_id, headers, json.dumps(data).encode())
    assert r.status_code == 400 and r.json()["errors"]
    wrong_epub, _ = sentence_map_body(work_id, epub_sha256="0" * 64)
    assert put_result(wc, job_id, headers, wrong_epub).json() == {"detail": "input_mismatch"}
    wrong_work, _ = sentence_map_body(work_id + 1)
    assert put_result(wc, job_id, headers, wrong_work).status_code == 422
    # sentences past the end of the real spine item are rejected
    data = json.loads(body)
    last = data["sentences"][-1]
    last["char_end"] = last["char_start"] + 40_000
    last.pop("text", None)
    r = put_result(wc, job_id, headers, json.dumps(data).encode())
    assert r.status_code == 400 and "beyond spine item" in r.json()["errors"][0]
    big = b" " * (jobs.RESULT_MAX_BYTES + 1)
    assert put_result(wc, job_id, headers, big).status_code == 413
    # none of that cost the lease
    assert db_job(job_id).status == JobStatus.LEASED
    assert put_result(wc, job_id, headers, body).status_code == 200


def test_changed_audio_makes_results_stale_and_requeues(client, owner, work_id, wc, settings):
    job_id = request_align(client, owner, work_id).json()["id"]
    headers = lease(claim(wc))
    with session_factory()() as db:
        work = db.get(Work, work_id)
        track = settings.library_dir / work.dir_path / "01 excerpt.mp3"
    with track.open("ab") as fh:
        fh.write(b"\0" * 417)  # a re-encode or replacement
    body, _ = sentence_map_body(work_id)
    assert put_result(wc, job_id, headers, body).json() == {"detail": "lease_lost"}
    assert wc.get(f"/api/worker/jobs/{job_id}/inputs/tracks/0", headers=headers).status_code == 409
    # detected at once, not at the next rescan: cancelled and requeued with the new snapshot
    assert db_job(job_id).status == JobStatus.CANCELLED
    with session_factory()() as db:
        new_job = jobs.active_job(db, work_id)
        assert new_job is not None and new_job.id != job_id
        assert new_job.payload["audio_fingerprint"] != db_job(job_id).payload["audio_fingerprint"]


def test_success_then_changed_epub_marks_alignment_stale(client, owner, work_id, settings):
    worker = LocalWorker(
        session_factory(),
        data_dir=settings.data_dir,
        library=settings.library_dir,
        transcribe=recorded,
    )
    request_align(client, owner, work_id)
    assert worker.run_once()
    with session_factory()() as db:
        work = db.get(Work, work_id)
        book = settings.library_dir / work.dir_path / "book.epub"
        work.files[0].sha256 = "f" * 64  # what a re-ingest of a new edition records
        db.commit()
        queued = jobs.refresh_work(db, settings.library_dir, work)
        db.commit()
        assert queued is not None and queued.status == JobStatus.QUEUED
        al = db.scalar(select(Alignment).where(Alignment.work_id == work_id))
        assert al.status == AlignmentStatus.STALE
        # the old sentences stay readable until a new alignment replaces them
        assert (
            db.scalar(select(func.count()).select_from(Sentence).where(Sentence.work_id == work_id))
            == 293
        )
    assert book.is_file()
    assert "stale" in client.get(f"/api/works/{work_id}/tracks").json()["alignment"]["warnings"]


def test_align_needs_inputs_and_role(client, owner, settings, tmp_path):
    text_only = ingest_fixture(settings, tmp_path, audio=False)
    r = request_align(client, owner, text_only)
    assert r.status_code == 409 and r.json()["detail"] == "no_inputs"
    assert request_align(client, owner, text_only, model="huge").json()["detail"] == "unknown_model"
    assert request_align(client, owner, 9999).status_code == 404
    api_post(
        client,
        "/api/admin/users",
        owner["csrf"],
        {"username": "reader", "role": "reader", "password": "reader password 123"},
    )
    csrf = login(client, "reader", "reader password 123")
    r = api_post(client, f"/api/works/{text_only}/align", csrf, {})
    assert r.status_code == 403
    assert client.get("/api/jobs").status_code == 403


# --- claiming is atomic ----------------------------------------------------------------------


def test_concurrent_claims_lease_each_job_once(client):
    with session_factory()() as db:
        for i in range(6):
            w = Work(
                title=f"W{i}", sort_title=f"w{i}", author="A", sort_author="a", dir_path=f"A/W{i}"
            )
            db.add(w)
            db.flush()
            db.add(Job(kind="align", work_id=w.id, status=JobStatus.QUEUED, payload={"tracks": []}))
        db.commit()
    results: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        while True:
            with session_factory()() as db:
                got = jobs.claim(db, kinds=["align"], worker="t", lease_s=60, credential_id=None)
            if got is None:
                return
            with lock:
                results.append(got[0].id)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == sorted(set(results)) and len(results) == 6


# --- in-process and remote workers ------------------------------------------------------------


def test_local_worker_runs_a_job(client, owner, work_id, settings):
    worker = LocalWorker(
        session_factory(),
        data_dir=settings.data_dir,
        library=settings.library_dir,
        transcribe=recorded,
        model_label="recorded-small",
    )
    assert not worker.run_once()  # empty queue
    job_id = request_align(client, owner, work_id).json()["id"]
    assert worker.run_once()
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "succeeded" and job["result"]["sentence_count"] == 293
    assert job["result"]["stats"]["diagnosis"] == "partial_audio"
    assert job["result"]["stats"]["speed"] > 0
    assert (
        client.get(f"/api/works/{work_id}/tracks").json()["alignment"]["model"] == "recorded-small"
    )


def test_local_worker_hands_the_job_back_on_shutdown(client, owner, work_id, settings):
    worker = LocalWorker(
        session_factory(),
        data_dir=settings.data_dir,
        library=settings.library_dir,
        transcribe=None,
    )

    def stopping(_tracks, _language, progress):
        worker._stop.set()  # what the app lifespan does on shutdown
        progress(0.5)
        raise AssertionError("unreachable")

    worker._transcribe = stopping
    job_id = request_align(client, owner, work_id).json()["id"]
    assert worker.run_once()
    job = db_job(job_id)
    assert job.status == JobStatus.QUEUED and job.attempts == 0 and job.error is None


def test_local_worker_releases_a_busy_job_when_stopped(client, owner, work_id, settings):
    """Transcription can run for a long time between progress reports; a server restart must
    not leave the job leased until its lease expires."""
    started, unblock = threading.Event(), threading.Event()

    def busy(tracks, language, progress):
        started.set()
        unblock.wait(10)  # inside one long faster-whisper segment
        return recorded(tracks, language, progress)

    worker = LocalWorker(
        session_factory(), data_dir=settings.data_dir, library=settings.library_dir,
        transcribe=busy, poll_s=0.05,
    )  # fmt: skip
    job_id = request_align(client, owner, work_id).json()["id"]
    worker.start()
    assert started.wait(10)
    worker.stop()  # what the app lifespan does on shutdown
    job = db_job(job_id)
    assert job.status == JobStatus.QUEUED and job.attempts == 0 and job.lease_token_hash is None
    unblock.set()  # the old thread finishes late; its result must not land
    worker._thread.join(10)
    assert db_job(job_id).status == JobStatus.QUEUED
    with session_factory()() as db:
        assert db.scalar(select(func.count()).select_from(Sentence)) == 0


def test_local_worker_reports_failures(client, owner, work_id, settings):
    def broken(*_a):
        raise RuntimeError("decoder exploded")

    worker = LocalWorker(
        session_factory(),
        data_dir=settings.data_dir,
        library=settings.library_dir,
        transcribe=broken,
    )
    job_id = request_align(client, owner, work_id).json()["id"]
    assert worker.run_once()
    job = db_job(job_id)
    assert job.status == JobStatus.QUEUED and job.error == "RuntimeError: decoder exploded"


def test_remote_worker_over_http(client, owner, work_id, app, worker_key):
    http = TestClient(app, headers={"Authorization": f"Bearer {worker_key}"})
    worker = RemoteWorker(
        "http://testserver", worker_key, client=http, transcribe=recorded, name="laptop"
    )
    assert not worker.run_once()
    job_id = request_align(client, owner, work_id).json()["id"]
    assert worker.run_once()
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "succeeded", job
    assert job["result"]["coverage"] == pytest.approx(15 / 293, abs=1e-4)


def test_remote_worker_abandons_a_cancelled_job(client, owner, work_id, app, worker_key):
    http = TestClient(app, headers={"Authorization": f"Bearer {worker_key}"})

    def cancel_midway(tracks, language, progress):
        api_post(client, f"/api/jobs/{job_id}/cancel", owner["csrf"])
        return recorded(tracks, language, progress)

    worker = RemoteWorker("http://testserver", worker_key, client=http, transcribe=cancel_midway)
    job_id = request_align(client, owner, work_id).json()["id"]
    assert worker.run_once()
    assert db_job(job_id).status == JobStatus.CANCELLED
    with session_factory()() as db:
        assert db.scalar(select(func.count()).select_from(Sentence)) == 0


def test_remote_worker_rejects_tampered_inputs(client, owner, work_id, app, worker_key, settings):
    """The downloaded bytes are hashed against the job snapshot, not just the header."""
    http = TestClient(app, headers={"Authorization": f"Bearer {worker_key}"})
    worker = RemoteWorker("http://testserver", worker_key, client=http, transcribe=recorded)
    job_id = request_align(client, owner, work_id).json()["id"]
    original = http.stream

    def corrupting(method, url, **kw):
        ctx = original(method, url, **kw)

        class Wrap:
            def __enter__(self):
                r = ctx.__enter__()
                r.headers.pop("x-content-sha256", None)
                inner = r.iter_bytes
                r.iter_bytes = lambda n: (b[:-1] + b"!" for b in inner(n))  # type: ignore[method-assign]
                return r

            def __exit__(self, *exc):
                return ctx.__exit__(*exc)

        return Wrap()

    http.stream = corrupting  # type: ignore[method-assign]
    assert worker.run_once()
    job = db_job(job_id)
    assert job.status == JobStatus.QUEUED and "does not match sha256" in job.error


def test_worker_cli_refuses_plain_http_and_missing_keys(monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="plain http"):
        cli_main(["worker", "--server", "http://lectern.lan"])
    monkeypatch.delenv("LECTERN_WORKER_KEY", raising=False)
    with pytest.raises(SystemExit, match="worker credential is required"):
        cli_main(["worker", "--server", "https://lectern.example"])
    key = tmp_path / "key"
    key.write_text("sk_not_a_worker_key\n")
    with pytest.raises(SystemExit, match="worker credential is required"):
        cli_main(["worker", "--server", "https://lectern.example", "--key-file", str(key)])


def test_track_discovery_finds_subfolders_in_order_and_skips_derived(
    client, owner, settings, tmp_path
):
    work_id = ingest_fixture(settings, tmp_path, audio=False)
    with session_factory()() as db:
        work_dir = settings.library_dir / db.get(Work, work_id).dir_path
    audio = EXCERPT.read_bytes()
    files = {"CD2/01.mp3": 1, "CD1/10.mp3": 2, "CD1/2.mp3": 3, "CD2/copy.mp3": 1}
    files |= {"derived/audio/x.opus": 4, ".hidden/a.mp3": 5}
    for rel, tag in files.items():
        (work_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (work_dir / rel).write_bytes(audio + bytes([tag]) * 64)  # distinct content
    with session_factory()() as db:
        assert jobs.refresh_work(db, settings.library_dir, db.get(Work, work_id)) is not None
        db.commit()
    tracks = client.get(f"/api/works/{work_id}/tracks").json()["tracks"]
    assert [t["position"] for t in tracks] == [0, 1, 2]
    d = tracks[0]["duration_s"]
    assert [t["offset_s"] for t in tracks] == pytest.approx([0, d, 2 * d], abs=0.01)
    streamed = [client.get(t["stream_url"]).content[-1] for t in tracks]
    assert streamed == [3, 2, 1]  # CD2/copy.mp3 duplicates CD2/01.mp3 and is skipped
    with session_factory()() as db:
        from lectern.align.tracks import work_tracks

        assert [t.rel_path for t in work_tracks(db, work_id)] == [
            "CD1/2.mp3",
            "CD1/10.mp3",
            "CD2/01.mp3",
        ]


# --- review fixes --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stats",
    [
        {"speed": -1},
        {"speed": 0},
        {"speed": 1e12},
        {"diagnosis": "all good, trust me"},
        {"span_coverage": 1.5},
        {"likely_abridged": "maybe"},
        {"words": -3},
    ],
)
def test_heartbeat_rejects_invalid_stats(client, owner, work_id, wc, stats):
    job_id = request_align(client, owner, work_id).json()["id"]
    headers = lease(claim(wc))
    r = wc.post(f"/api/worker/jobs/{job_id}/heartbeat", json={"stats": stats}, headers=headers)
    assert r.status_code == 422
    assert "stats" not in (db_job(job_id).result or {})


def test_heartbeat_stores_only_known_finite_stats_and_caps_bodies(client, owner, work_id, wc):
    job_id = request_align(client, owner, work_id).json()["id"]
    headers = lease(claim(wc))
    url = f"/api/worker/jobs/{job_id}/heartbeat"
    raw = b'{"stats": {"speed": Infinity}}'
    r = wc.post(url, content=raw, headers={**headers, "Content-Type": "application/json"})
    assert r.status_code == 422
    r = wc.post(
        url, json={"stats": {"speed": 1.5, "diagnosis": "ok", "evil": "x" * 1000}}, headers=headers
    )
    assert r.status_code == 200
    assert db_job(job_id).result["stats"] == {"speed": 1.5, "diagnosis": "ok"}
    big = {"message": "m", "pad": "x" * 70_000}
    assert wc.post(url, json=big, headers=headers).status_code == 413
    fail = f"/api/worker/jobs/{job_id}/fail"
    assert (
        wc.post(fail, json={"error": "e", "pad": "x" * 70_000}, headers=headers).status_code == 413
    )
    assert db_job(job_id).status == JobStatus.LEASED


def test_measured_speed_ignores_bad_historical_values(client):
    rows = [
        {"stats": {"speed": "fast"}},
        {"stats": {"speed": float("nan")}},
        {"stats": {"speed": 0}},
        {"stats": {"speed": -4}},
        {"stats": {"speed": 1e12}},
        {"stats": {"speed": [1]}},
        {"stats": [1, 2]},
        {"audio_s": 10, "elapsed_s": 0},
        {"audio_s": "x", "elapsed_s": 2},
        {"stats": {"speed": 2.0}},
    ]
    with session_factory()() as db:
        for result in rows:
            db.add(Job(kind="align", status=JobStatus.SUCCEEDED, payload={"tracks": []},
                       result=result, finished_at=utcnow()))  # fmt: skip
        db.commit()
        assert jobs.measured_speed(db, None) == (2.0, 1)
        for job in db.scalars(select(Job)).all():
            out = jobs.job_json(db, job)  # never raises on old rows
            assert isinstance(out["result"]["stats"], dict)


def test_valid_workers_are_not_throttled_like_logins(client, owner, work_id, wc, app):
    from lectern.security.ratelimit import SlidingWindowLimiter

    job_id = request_align(client, owner, work_id).json()["id"]
    c = claim(wc)
    headers = lease(c)
    track = c.json()["inputs"]["tracks"][0]
    for i in range(150):  # many range requests: far beyond the 60/min login-style limit
        r = wc.get(track, headers={**headers, "Range": f"bytes={i}-{i + 99}"})
        assert r.status_code == 206, i
    # a valid key has its own bucket; when it is exhausted, /fail still gets through
    app.state.limiters["worker"] = SlidingWindowLimiter(limit=2, window_seconds=60)
    hb = f"/api/worker/jobs/{job_id}/heartbeat"
    assert [wc.post(hb, json={}, headers=headers).status_code for _ in range(3)] == [200, 200, 429]
    r = wc.post(f"/api/worker/jobs/{job_id}/fail", json={"error": "bye"}, headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "queued"
    # failed credential checks are limited per address, and then nothing is looked up
    bad = TestClient(app, headers={"Authorization": "Bearer lw_" + "z" * 43})
    codes = [bad.post("/api/worker/jobs/claim", json={}).status_code for _ in range(61)]
    assert codes[:60] == [401] * 60 and codes[60] == 429


def test_result_is_applied_off_the_event_loop(client, owner, work_id, wc, monkeypatch):
    import asyncio

    seen = {}
    original = jobs.apply_result

    def spy(*args, **kw):
        try:
            asyncio.get_running_loop()
            seen["loop"] = True
        except RuntimeError:
            seen["loop"] = False
        return original(*args, **kw)

    monkeypatch.setattr(jobs, "apply_result", spy)
    job_id = request_align(client, owner, work_id).json()["id"]
    headers = lease(claim(wc))
    body, _ = sentence_map_body(work_id)
    assert put_result(wc, job_id, headers, body).status_code == 200
    assert seen == {"loop": False}


def test_result_tracks_must_match_the_job_and_floats_must_be_finite(client, owner, work_id, wc):
    job_id = request_align(client, owner, work_id).json()["id"]
    headers = lease(claim(wc))
    body, _ = sentence_map_body(work_id)
    data = json.loads(body)
    data["tracks"][0]["duration_s"] += 1.0  # a stretched timeline
    r = put_result(wc, job_id, headers, json.dumps(data).encode())
    assert r.status_code == 422 and r.json()["detail"] == "input_mismatch"
    assert "duration" in r.json()["errors"][0]
    data = json.loads(body)
    data["sentences"][20]["audio_start_s"] = float("nan")
    r = put_result(wc, job_id, headers, json.dumps(data).encode())
    assert r.status_code == 400 and r.json()["detail"] == "invalid_map"
    assert put_result(wc, job_id, headers, body).status_code == 200


def test_force_is_refused_while_a_worker_holds_the_job(client, owner, work_id, wc):
    first = request_align(client, owner, work_id).json()["id"]
    queued = request_align(client, owner, work_id, force=True)
    assert queued.status_code == 202 and queued.json()["id"] != first  # queued: replaced
    assert db_job(first).status == JobStatus.CANCELLED
    lease(claim(wc))
    r = request_align(client, owner, work_id, force=True)
    assert r.status_code == 409 and r.json() == {"detail": "job_running"}
    assert db_job(queued.json()["id"]).status == JobStatus.LEASED


def test_force_replaces_only_a_job_the_caller_may_cancel(client, owner, work_id):
    """Issue #117 (1): force cancels the active job, so it follows the cancel rule (the owner,
    or whoever started it); a member cannot replace someone else's queued alignment."""
    mia = make_user(client, owner["csrf"], "mia", "member")
    m = TestClient(client.app)
    m_csrf = login(m, "mia", mia["password"])
    theirs = request_align(client, owner, work_id).json()["id"]
    r = request_align(m, {"csrf": m_csrf}, work_id, force=True)
    assert (r.status_code, r.json()["detail"]) == (403, "not_your_job")
    assert db_job(theirs).status == JobStatus.QUEUED
    r = request_align(m, {"csrf": m_csrf}, work_id)  # without force: the job, unchanged
    assert (r.status_code, r.json()["id"]) == (200, theirs)

    with session_factory()() as db:  # now hers: she may replace it
        db.get(Job, theirs).created_by = mia["id"]
        db.commit()
    r = request_align(m, {"csrf": m_csrf}, work_id, force=True)
    assert r.status_code == 202 and r.json()["id"] != theirs
    assert db_job(theirs).status == JobStatus.CANCELLED
    r = request_align(client, owner, work_id, force=True)  # and the owner may replace hers
    assert r.status_code == 202


def test_a_lease_is_bound_to_the_credential_that_claimed_it(client, owner, work_id, wc, app):
    other = api_post(client, "/api/worker-credentials", owner["csrf"], {"label": "other"}).json()
    wc2 = TestClient(app, headers={"Authorization": f"Bearer {other['secret']}"})
    job_id = request_align(client, owner, work_id).json()["id"]
    c = claim(wc)
    headers = lease(c)
    assert (
        wc2.post(f"/api/worker/jobs/{job_id}/heartbeat", json={}, headers=headers).status_code
        == 409
    )
    assert wc2.get(c.json()["inputs"]["epub"], headers=headers).status_code == 409
    body, _ = sentence_map_body(work_id)
    assert put_result(wc2, job_id, headers, body).status_code == 409
    assert (
        wc2.post(
            f"/api/worker/jobs/{job_id}/fail", json={"error": "x"}, headers=headers
        ).status_code
        == 409
    )
    assert put_result(wc, job_id, headers, body).status_code == 200


def test_jobs_go_only_to_workers_with_the_capability_and_the_allowlist(client, owner, work_id, wc):
    """Routing (docs/contracts.md, "Capabilities and routing"): a transcode job is not
    leased to a worker that advertises only align, nor to one whose credential the owner
    limited to align; a server-only capability never leaves the server at all."""
    with session_factory()() as db:
        db.add(Job(kind="transcode", work_id=work_id, status=JobStatus.QUEUED, payload={}))
        db.add(Job(kind="tts", work_id=work_id, status=JobStatus.QUEUED,
                   payload={"engine": "elevenlabs"}))  # fmt: skip
        db.commit()
    assert claim(wc, capabilities=["align"]).status_code == 204
    cred_id = client.get("/api/worker-credentials").json()["credentials"][0]["id"]
    r = client.patch(
        f"/api/worker-credentials/{cred_id}",
        json={"capabilities": ["align"]},
        headers={"x-csrf-token": owner["csrf"], "origin": ORIGIN},
    )
    assert r.status_code == 200 and r.json()["claimable"] == ["align"]
    assert claim(wc, capabilities=["transcode", "tts-elevenlabs"]).status_code == 204
    job_id = request_align(client, owner, work_id).json()["id"]
    got = claim(wc, capabilities=["transcode", "align"])
    assert got.status_code == 200 and got.json()["job"]["id"] == job_id
    # lifting the allowlist lets transcode through; ElevenLabs stays on the server
    client.patch(
        f"/api/worker-credentials/{cred_id}",
        json={"capabilities": None},
        headers={"x-csrf-token": owner["csrf"], "origin": ORIGIN},
    )
    got = claim(wc, capabilities=["transcode", "tts-elevenlabs"])
    assert got.status_code == 200 and got.json()["job"]["kind"] == "transcode"
    assert claim(wc, capabilities=["tts-elevenlabs"]).status_code == 204
    with session_factory()() as db:
        eleven = db.scalar(select(Job).where(Job.kind == "tts"))
        assert eleven.capability == "tts-elevenlabs" and eleven.status == JobStatus.QUEUED


def test_a_result_moves_stored_positions_onto_the_aligned_sentences(
    client, owner, work_id, wc, settings
):
    from tests.test_cross_sync import STORY, put_web

    from lectern.ingest.sentences import ensure_text_sentences

    with session_factory()() as db:
        assert ensure_text_sentences(db, settings.library_dir, work_id) > 0
        db.commit()
    char = STORY.index("Della finished her cry") + 5
    assert (
        put_web(client, owner, work_id, source="web", spine_index=2, char=char).status_code == 200
    )
    job_id = request_align(client, owner, work_id).json()["id"]
    body, _ = sentence_map_body(work_id)
    assert put_result(wc, job_id, lease(claim(wc)), body).status_code == 200
    got = client.get(f"/api/works/{work_id}/progress").json()
    assert got["text"] == {"spine_index": 2, "char": char}
    with session_factory()() as db:
        n = got["position"]["sentence_n"]
        s = db.scalar(select(Sentence).where(Sentence.work_id == work_id, Sentence.n == n))
        assert s.char_start <= char < s.char_end and s.spine_index == 2


# --- the isolated local worker (lectern.align.isolate) ------------------------------------------


def _transcript_file(tmp_path: Path) -> Path:
    path = tmp_path / "words.json"
    path.write_text(json.dumps(TRANSCRIPT))
    return path


def isolated_worker(settings, tmp_path, **kw) -> LocalWorker:
    kw.setdefault("transcript", _transcript_file(tmp_path))
    return LocalWorker(
        session_factory(),
        data_dir=settings.data_dir,
        library=settings.library_dir,
        model_label="recorded-small",
        isolated=True,
        poll_s=0.05,
        **kw,
    )


def child(code: str) -> list[str]:
    """A stand-in alignment process: reads the job spec like the real one, then runs ``code``."""
    return [
        sys.executable,
        "-c",
        "import json, os, signal, sys, time\nspec = json.load(sys.stdin)\n" + code,
    ]


def test_isolated_worker_aligns_in_a_child_process(client, owner, work_id, settings, tmp_path):
    worker = isolated_worker(settings, tmp_path, memory_limit_mb=0)
    job_id = request_align(client, owner, work_id).json()["id"]
    assert worker.run_once()
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "succeeded", job
    assert job["result"]["sentence_count"] == 293
    assert job["result"]["coverage"] == pytest.approx(15 / 293, abs=1e-4)
    # the child measured its own memory, and its log is kept
    assert job["result"]["stats"]["peak_rss_mb"] > 0
    assert (settings.data_dir / "logs" / "align" / f"job-{job_id}-attempt-1.log").is_file()
    assert not list((settings.data_dir / "tmp").glob("align-result-*"))


@pytest.mark.parametrize(
    ("code", "kind", "status", "error"),
    [
        # a native crash: retried with backoff like any failure
        ("os.kill(os.getpid(), signal.SIGSEGV)", "crash", JobStatus.QUEUED, "killed by SIGSEGV"),
        ("sys.exit(7)", "crash", JobStatus.QUEUED, "exited with status 7"),
        # what the kernel's out-of-memory killer does: fails for good, no retry loop
        ("os.kill(os.getpid(), signal.SIGKILL)", "oom", JobStatus.FAILED, "out-of-memory killer"),
        # the child's exit status for a Python MemoryError
        ("sys.exit(3)", "oom", JobStatus.FAILED, "MemoryError"),
    ],
)
def test_a_crashing_alignment_fails_the_job_not_the_server(
    client, owner, work_id, settings, tmp_path, code, kind, status, error
):
    code = "sys.stderr.write('decoding track 1\\n'); sys.stderr.flush()\n" + code
    worker = isolated_worker(settings, tmp_path, child_command=child(code))
    job_id = request_align(client, owner, work_id).json()["id"]
    assert worker.run_once()
    job = db_job(job_id)
    assert job.status == status and error in job.error, job.error
    assert "decoding track 1" in job.error  # the child's last log lines are kept
    assert job.lease_token_hash is None or status == JobStatus.FAILED
    if status == JobStatus.QUEUED:
        assert job.run_after > utcnow() and job.attempts == 1
    else:
        al = client.get(f"/api/works/{work_id}/tracks").json()["alignment"]
        assert al["status"] == "failed" and "out of memory" in al["error"]
    assert client.get("/healthz").status_code == 200


def test_memory_cap_kills_the_child(client, owner, work_id, settings, tmp_path):
    hog = "x = bytearray(400 * 2**20)\nfor i in range(0, len(x), 4096): x[i] = 1\ntime.sleep(30)"
    worker = isolated_worker(settings, tmp_path, child_command=child(hog), memory_limit_mb=150)
    job_id = request_align(client, owner, work_id).json()["id"]
    assert worker.run_once()
    job = db_job(job_id)
    assert job.status == JobStatus.FAILED
    assert "used more than 150 MiB" in job.error and "LECTERN_ALIGN_WINDOW_SECONDS" in job.error


def test_crashes_end_after_max_attempts(client, owner, work_id, settings, tmp_path):
    worker = isolated_worker(settings, tmp_path, child_command=child("os.abort()"))
    job_id = request_align(client, owner, work_id).json()["id"]
    for attempt in range(1, 4):
        set_job(job_id, run_after=utcnow())
        assert worker.run_once()
        assert db_job(job_id).attempts == attempt
    job = db_job(job_id)
    assert job.status == JobStatus.FAILED and "SIGABRT" in job.error
    set_job(job_id, run_after=utcnow())
    assert not worker.run_once()  # nothing left to claim


def _wait_for(predicate, timeout=10.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie is gone for our purposes
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


SLOW_CHILD = (
    "open(os.environ['PIDFILE'], 'w').write(str(os.getpid()))\n"
    "print(json.dumps({'type': 'progress', 'progress': 0.4, 'message': 'transcribing'}), flush=True)\n"
    "time.sleep(60)"
)


def test_shutdown_stops_the_child_and_hands_the_job_back(
    client, owner, work_id, settings, tmp_path, monkeypatch
):
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("PIDFILE", str(pidfile))
    worker = isolated_worker(settings, tmp_path, child_command=child(SLOW_CHILD))
    job_id = request_align(client, owner, work_id).json()["id"]
    worker.start()
    assert _wait_for(pidfile.exists)
    assert _wait_for(lambda: pidfile.read_text() != "")
    pid = int(pidfile.read_text())
    worker.stop()  # what the app lifespan does on shutdown
    job = db_job(job_id)
    assert job.status == JobStatus.QUEUED and job.attempts == 0 and job.lease_token_hash is None
    assert not _alive(pid)


def test_a_lost_lease_kills_the_child(client, owner, work_id, settings, tmp_path, monkeypatch):
    monkeypatch.setattr("lectern.align.worker.MAX_HEARTBEAT_S", 1.0)
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("PIDFILE", str(pidfile))
    worker = isolated_worker(settings, tmp_path, child_command=child(SLOW_CHILD), lease_s=60)
    job_id = request_align(client, owner, work_id).json()["id"]
    done = threading.Event()
    threading.Thread(target=lambda: (worker.run_once(), done.set())).start()
    assert _wait_for(lambda: pidfile.exists() and pidfile.read_text() != "")
    pid = int(pidfile.read_text())
    api_post(client, f"/api/jobs/{job_id}/cancel", owner["csrf"])
    assert done.wait(30)  # the next heartbeat (every second here) finds the lease gone
    assert db_job(job_id).status == JobStatus.CANCELLED
    assert not _alive(pid)


GRANDCHILD = (
    "import subprocess\n"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "open(os.environ['PIDFILE'], 'w').write(str(p.pid))\n"
)


@pytest.mark.parametrize("ending", ["sys.exit(7)", "time.sleep(60)"])
def test_nothing_the_child_started_outlives_it(
    client, owner, work_id, settings, tmp_path, monkeypatch, ending
):
    """A crash, and a lost lease, take the child's whole process group with it."""
    monkeypatch.setattr("lectern.align.worker.MAX_HEARTBEAT_S", 1.0)
    pidfile = tmp_path / "grandchild.pid"
    monkeypatch.setenv("PIDFILE", str(pidfile))
    worker = isolated_worker(settings, tmp_path, child_command=child(GRANDCHILD + ending))
    job_id = request_align(client, owner, work_id).json()["id"]
    done = threading.Event()
    threading.Thread(target=lambda: (worker.run_once(), done.set())).start()
    assert _wait_for(lambda: pidfile.exists() and pidfile.read_text() != "")
    if ending.startswith("time"):
        api_post(client, f"/api/jobs/{job_id}/cancel", owner["csrf"])
    assert done.wait(30)
    assert _wait_for(lambda: not _alive(int(pidfile.read_text())), timeout=5)


def test_native_output_on_stdout_does_not_break_the_protocol(
    client, owner, work_id, settings, tmp_path
):
    """Libraries in the child that print to fd 1 (no newline) end up in the log; before, the
    noise glued onto the "done" line and a successful alignment counted as a crash."""
    code = (
        "import os\nimport lectern.align.child as c\nemit = c._emit\n"
        "def noisy(msg):\n    os.write(1, b'native noise ')\n    emit(msg)\n"
        "c._emit = noisy\nc.main()\n"
    )
    worker = isolated_worker(settings, tmp_path, child_command=[sys.executable, "-c", code])
    job_id = request_align(client, owner, work_id).json()["id"]
    assert worker.run_once()
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "succeeded", job
    log = settings.data_dir / "logs" / "align" / f"job-{job_id}-attempt-1.log"
    assert "native noise" in log.read_text()


def test_the_supervisor_kills_the_child_if_it_fails_itself(tmp_path, monkeypatch):
    from lectern.align import isolate

    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("PIDFILE", str(pidfile))
    calls = []

    def interrupt():
        calls.append(1)
        if pidfile.exists() and pidfile.read_text():
            raise RuntimeError("supervisor bug")
        return None

    with pytest.raises(RuntimeError):
        isolate.run(
            {},
            workdir=tmp_path,
            log_path=None,
            on_progress=lambda *_: None,
            interrupt=interrupt,
            memory_limit_mb=None,
            command=child(SLOW_CHILD),
        )
    assert not _alive(int(pidfile.read_text()))


# --- stuck alignments: no engine, restarts, stalls, retry -------------------------------------


def read_aloud_alignment(client, work_id) -> dict:
    r = client.get(f"/api/works/{work_id}/read-aloud")
    assert r.status_code == 200, r.text
    return r.json()["alignment"]


@pytest.fixture
def no_local_engine(monkeypatch):
    """The default (slim) image: faster-whisper is not installed."""
    monkeypatch.setattr("lectern.align.transcribe.transcription_available", lambda: False)


def test_a_queued_alignment_without_any_engine_says_so_until_a_worker_connects(
    client, owner, work_id, wc, monkeypatch, no_local_engine
):
    job_id = request_align(client, owner, work_id).json()["id"]
    al = read_aloud_alignment(client, work_id)
    assert al["status"] == "pending" and al["job"]["status"] == "queued"
    assert al["unavailable"]["reason"] == "not_installed"
    assert "LECTERN_EXTRAS=align" in al["unavailable"]["message"]
    assert al["unavailable"]["docs_url"].endswith("docs/alignment.md#turning-it-on")
    # the job stays queued, so it runs as soon as an engine appears
    assert db_job(job_id).status == JobStatus.QUEUED

    # an external worker that polls (even with nothing runnable yet) is an engine
    set_job(job_id, run_after=utcnow() + timedelta(hours=1))
    assert claim(wc).status_code == 204
    assert read_aloud_alignment(client, work_id)["unavailable"] is None
    # ... until it has been gone for a while
    with session_factory()() as db:
        from lectern.models import WorkerCredential

        db.scalar(select(WorkerCredential)).last_used_at = utcnow() - timedelta(minutes=10)
        db.commit()
    assert read_aloud_alignment(client, work_id)["unavailable"]["reason"] == "not_installed"
    # a server that aligns itself is never "unavailable"
    monkeypatch.setattr("lectern.align.transcribe.transcription_available", lambda: True)
    assert read_aloud_alignment(client, work_id)["unavailable"] is None


def _restart(settings) -> None:
    """What the app lifespan does at startup (and at shutdown)."""
    from lectern.services import start_services

    settings.track_rescan_seconds = 3600
    for service in start_services(session_factory(), settings):
        service.stop()


def _claim_locally(settings) -> None:
    with session_factory()() as db:
        assert jobs.claim(db, kinds=["align"], worker="local", lease_s=600, credential_id=None)


def test_a_restart_requeues_the_dead_local_workers_job_then_fails_it_visibly(
    client, owner, work_id, wc, settings, no_local_engine
):
    job_id = request_align(client, owner, work_id).json()["id"]
    # an external worker's lease survives a server restart: that worker is still running
    lease(claim(wc))
    _restart(settings)
    assert db_job(job_id).status == JobStatus.LEASED
    # the in-process worker's lease does not (the server crashed or was OOM-killed): it is
    # reclaimed at once, not when its 10-minute lease runs out
    set_job(job_id, leased_by_id=None)
    for attempt in (1, 2, 3):
        if attempt > 1:
            set_job(job_id, run_after=utcnow())
            _claim_locally(settings)
        assert db_job(job_id).status == JobStatus.LEASED
        _restart(settings)
        job = db_job(job_id)
        assert job.attempts == attempt and "server restarted" in job.error
        if attempt < 3:
            assert job.status == JobStatus.QUEUED and job.run_after > utcnow()
            al = read_aloud_alignment(client, work_id)
            assert al["job"]["status"] == "queued" and al["job"]["error"] == job.error
    assert job.status == JobStatus.FAILED
    al = read_aloud_alignment(client, work_id)
    assert al["status"] == "failed" and "server restarted" in al["error"]


@pytest.mark.parametrize("how", ["lease_expired", "no_progress"])
def test_the_sweep_reclaims_dead_and_stalled_runs_without_another_claim(
    client, owner, work_id, wc, settings, how
):
    from lectern.services import Housekeeping

    job_id = request_align(client, owner, work_id).json()["id"]
    headers = lease(claim(wc))

    def heartbeat(progress):
        return wc.post(
            f"/api/worker/jobs/{job_id}/heartbeat",
            json={"progress": progress, "message": "transcribing"},
            headers=headers,
        )

    def sweep_once():
        settings.job_sweep_seconds, settings.track_rescan_seconds = 0.05, 3600
        hk = Housekeeping(session_factory(), settings)
        hk.start()
        _wait_for(lambda: db_job(job_id).status != JobStatus.LEASED, timeout=1.0)
        hk.stop()

    assert heartbeat(0.3).status_code == 200
    long_ago = (utcnow() - timedelta(hours=2)).isoformat()
    # a long run that keeps making progress is left alone, however long ago it was claimed
    set_job(job_id, result={**db_job(job_id).result, "claimed_at": long_ago})
    sweep_once()
    assert db_job(job_id).status == JobStatus.LEASED
    if how == "lease_expired":  # the worker was killed: no heartbeats
        set_job(job_id, lease_expires_at=utcnow() - timedelta(seconds=1))
        expected = "lease expired"
    else:  # the worker still heartbeats, but its progress has not moved in two hours
        set_job(job_id, result={**db_job(job_id).result, "progress_at": long_ago})
        assert heartbeat(0.3).status_code == 200  # the same progress is not progress
        expected = "no progress for 120 min"
    sweep_once()
    job = db_job(job_id)
    assert job.status == JobStatus.QUEUED and job.error == expected and job.attempts == 1
    assert heartbeat(0.4).status_code == 409  # the worker's lease is gone


def test_running_alignment_reports_heartbeat_progress_and_an_eta(client, owner, work_id, wc):
    job_id = request_align(client, owner, work_id).json()["id"]
    headers = lease(claim(wc))
    al = read_aloud_alignment(client, work_id)
    assert al["status"] == "running" and al["job"]["status"] == "leased"
    assert al["job"]["progress"] == 0.0 and al["job"]["eta_s"] is None  # nothing to go on
    # 100 s into the run, a quarter done: about 300 s left (no finished run to measure yet)
    claimed = (utcnow() - timedelta(seconds=100)).isoformat()
    set_job(job_id, result={**db_job(job_id).result, "claimed_at": claimed})
    r = wc.post(
        f"/api/worker/jobs/{job_id}/heartbeat",
        json={"progress": 0.25, "message": "transcribing"},
        headers=headers,
    )
    assert r.status_code == 200
    job = read_aloud_alignment(client, work_id)["job"]
    assert job["progress"] == 0.25 and job["message"] == "transcribing"
    assert 295 <= job["eta_s"] <= 305
    assert job["heartbeat_at"] and job["attempts"] == 1 and job["max_attempts"] == 3
    assert client.get(f"/api/jobs/{job_id}").json()["eta"]["basis"] == "this run's progress so far"


def _fail_alignment(client, owner, work_id, wc, model, error):
    job_id = request_align(client, owner, work_id, model=model).json()["id"]
    r = wc.post(
        f"/api/worker/jobs/{job_id}/fail",
        json={"error": error, "retryable": False},
        headers=lease(claim(wc)),
    )
    assert r.status_code == 200, r.text
    return job_id


@pytest.mark.parametrize(
    ("model", "error", "retry"),
    [
        # an ordinary failure: Retry reruns the model that failed
        ("medium", "lease expired", "medium"),
        # out of memory: never the same model again, one size smaller
        ("medium", "out of memory: the alignment used more than 900 MiB.", "small"),
        ("small", "alignment process killed by the out-of-memory killer", "base"),
        ("large-v3-turbo", "CUDA out of memory", "medium"),
    ],
)
def test_retry_of_a_failed_alignment_avoids_the_model_that_ran_out_of_memory(
    client, owner, work_id, wc, model, error, retry
):
    job_id = _fail_alignment(client, owner, work_id, wc, model, error)
    al = read_aloud_alignment(client, work_id)
    assert al["status"] == "failed" and al["job"]["error"] == error
    assert al["retry_model"] == retry  # what the Formats card's Retry button runs

    r = request_align(client, owner, work_id)  # Retry without naming a model
    assert r.status_code == 202, r.text
    assert r.json()["id"] != job_id and r.json()["model"] == retry
    al = read_aloud_alignment(client, work_id)
    assert al["status"] == "pending" and al["job"]["status"] == "queued"
    assert al["job"]["error"] is None and al["retry_model"] is None


def test_out_of_memory_on_the_smallest_model_needs_a_deliberate_rerun(client, owner, work_id, wc):
    _fail_alignment(client, owner, work_id, wc, "tiny", "out of memory: MemoryError")
    assert read_aloud_alignment(client, work_id)["retry_model"] is None  # no Retry button
    # not the server's default (larger) model either
    r = request_align(client, owner, work_id)
    assert r.status_code == 409 and r.json()["detail"] == "out_of_memory_choose_model"
    r = request_align(client, owner, work_id, model="tiny")  # the editor names one
    assert r.status_code == 202 and r.json()["model"] == "tiny"


def test_readers_see_that_alignment_failed_but_not_its_error_text(
    client, owner, work_id, wc, settings
):
    error = "out of memory: see /data/logs/align/job-1-attempt-1.log"
    _fail_alignment(client, owner, work_id, wc, "small", error)
    assert read_aloud_alignment(client, work_id)["error"] == error  # the owner does
    rex = make_user(client, owner["csrf"], "rex", "reader")
    with session_factory()() as db:  # the book is on a shelf shared with rex
        from lectern.models import Shelf, ShelfGrant, ShelfItem, ShelfPermission, User

        owner_id = db.scalar(select(User.id).where(User.username == "owner"))
        shelf = Shelf(owner_id=owner_id, name="Shared")
        db.add(shelf)
        db.flush()
        db.add(ShelfItem(shelf_id=shelf.id, work_id=work_id))
        db.add(ShelfGrant(shelf_id=shelf.id, user_id=rex["id"], permission=ShelfPermission.VIEW))
        db.commit()
    login(client, "rex", rex["password"])
    st = client.get(f"/api/works/{work_id}/read-aloud")
    assert st.status_code == 200, st.text
    al = st.json()["alignment"]
    assert al["status"] == "failed" and al["error"] is None
    assert al["job"]["status"] == "failed" and al["job"]["error"] is None
    assert al["retry_model"] is None
    assert client.get(f"/api/works/{work_id}/tracks").json()["alignment"]["error"] is None
    assert "/data/logs" not in st.text


def test_remote_worker_polls_inside_the_servers_worker_seen_window(worker_key, caplog):
    from lectern.align.worker import MAX_POLL_S

    assert MAX_POLL_S < jobs.WORKER_SEEN_S
    slow = RemoteWorker("https://lectern.example", worker_key, poll_s=900, client=object())
    assert slow.poll_s == MAX_POLL_S
    assert "polling every" in caplog.text
    assert RemoteWorker("https://x", worker_key, poll_s=30, client=object()).poll_s == 30


def test_claims_reclaim_stalled_runs_after_the_configured_stall_time(
    client, owner, work_id, wc, settings, tmp_path
):
    """LECTERN_ALIGN_STALL_SECONDS applies to the reclaim before every claim, not only to
    the sweep: here 10 min, so a run stuck for 20 min (under the 1 h default) is reclaimed."""
    settings.align_stall_seconds = 600
    stuck = (utcnow() - timedelta(minutes=20)).isoformat()
    job_id = request_align(client, owner, work_id).json()["id"]
    lease(claim(wc))
    set_job(job_id, result={**db_job(job_id).result, "progress_at": stuck})
    assert claim(wc).status_code == 204  # the external worker's claim reclaims it
    job = db_job(job_id)
    assert job.status == JobStatus.QUEUED and job.error == "no progress for 20 min"

    # the same through the in-process worker's claim
    set_job(job_id, run_after=utcnow())
    lease(claim(wc))
    set_job(job_id, result={**db_job(job_id).result, "progress_at": stuck})
    worker = isolated_worker(settings, tmp_path, stall_s=settings.align_stall_seconds)
    assert not worker.run_once()  # reclaimed, then waiting out its backoff
    job = db_job(job_id)
    assert job.status == JobStatus.QUEUED and job.attempts == 2
    assert job.error == "no progress for 20 min"


@pytest.mark.parametrize(
    "race", ["succeeded", "cancelled_and_requeued", "claimed_again", "heartbeat"]
)
def test_reclaim_leaves_a_lease_that_changed_while_it_looked_alone(
    client, owner, work_id, wc, settings, monkeypatch, race
):
    """Between reading the expired lease and requeueing it, the job finishes, is cancelled
    and replaced, or is requeued and claimed again with a new token. The reclaim must not
    overwrite any of that (nor, for the replacement, trip the one-active-job index)."""
    job_id = request_align(client, owner, work_id).json()["id"]
    lease(claim(wc))
    if race == "heartbeat":  # stalled for two hours, its lease still current
        stuck = (utcnow() - timedelta(hours=2)).isoformat()
        set_job(job_id, result={**db_job(job_id).result, "progress_at": stuck})
    else:
        set_job(job_id, lease_expires_at=utcnow() - timedelta(seconds=1))
    new_hash = "f" * 64
    replacement: list[int] = []
    real_backoff = jobs.backoff_s

    def concurrently(attempts, rng=None):
        monkeypatch.setattr(jobs, "backoff_s", real_backoff)  # once
        with session_factory()() as other:
            job = other.get(Job, job_id)
            if race == "succeeded":
                job.status, job.finished_at, job.lease_expires_at = (
                    JobStatus.SUCCEEDED,
                    utcnow(),
                    None,
                )
                other.scalar(select(Alignment)).status = AlignmentStatus.SUCCEEDED
            elif race == "cancelled_and_requeued":
                jobs.cancel_job(other, job)
                new, _ = jobs.enqueue_align(other, settings.library_dir, other.get(Work, work_id))
                other.flush()
                replacement.append(new.id)
            elif race == "heartbeat":  # the stalled run reports progress after all
                jobs.heartbeat(other, job, progress=0.6, message="transcribing")
            else:  # requeued by its worker and leased again by another
                job.lease_token_hash = new_hash
                job.lease_expires_at = utcnow() + timedelta(minutes=10)
                job.attempts = 2
            other.commit()
        return real_backoff(attempts, rng)

    monkeypatch.setattr(jobs, "backoff_s", concurrently)
    with session_factory()() as db:
        assert jobs.reclaim_expired(db) == 0
    job = db_job(job_id)
    assert job.error is None
    with session_factory()() as db:
        al = db.scalar(select(Alignment))
    if race == "succeeded":
        assert job.status == JobStatus.SUCCEEDED and al.status == AlignmentStatus.SUCCEEDED
    elif race == "cancelled_and_requeued":
        assert job.status == JobStatus.CANCELLED
        assert db_job(replacement[0]).status == JobStatus.QUEUED
        assert al.status == AlignmentStatus.PENDING
    elif race == "heartbeat":
        assert job.status == JobStatus.LEASED and job.attempts == 1
        assert job.result["progress"] == 0.6 and al.status == AlignmentStatus.RUNNING
    else:
        assert job.status == JobStatus.LEASED and job.lease_token_hash == new_hash
        assert al.status == AlignmentStatus.RUNNING
