"""Resumable uploads: chunks, resume, checksums, ownership, limits, cancel, expiry, hand-off."""

from __future__ import annotations

import hashlib
import os
import time
from datetime import timedelta

import pytest
from tests.conftest import (
    ORIGIN,
    api_delete,
    api_post,
    drain_inbox,
    login,
    make_epub,
    make_user,
)

from lectern.db import session_factory
from lectern.models import UploadSession, UploadStatus, utcnow
from lectern.uploads import part_path, sweep

CHUNK = 1024


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch):
    monkeypatch.setattr("lectern.routes.api.uploads.UPLOAD_CHUNK_MAX_BYTES", CHUNK)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def create(client, csrf, data: bytes, name="book.epub", media="application/epub+zip", **extra):
    body = {"filename": name, "size": len(data), "media_type": media, **extra}
    return api_post(client, "/api/uploads", csrf, body)


def chunk(client, csrf, uid: str, offset: int, data: bytes, digest: str | None = None):
    return client.put(
        f"/api/uploads/{uid}/chunk",
        content=data,
        headers={
            "origin": ORIGIN,
            "x-csrf-token": csrf,
            "upload-offset": str(offset),
            "x-chunk-sha256": digest or sha(data),
            "content-type": "application/octet-stream",
        },
    )


def test_resumable_upload_happy_path_resume_and_replay(client, owner, settings, tmp_path):
    epub = make_epub(tmp_path / "big.epub", title="Resumed Book", padding=3000).read_bytes()
    csrf = owner["csrf"]
    r = create(client, csrf, epub, sha256=sha(epub))
    assert r.status_code == 201, r.text
    s = r.json()
    uid = s["id"]
    assert len(uid) == 22 and s["chunk_size"] == CHUNK and s["received_bytes"] == 0
    assert part_path(settings, uid).exists()
    assert not any(settings.inbox_path.glob("*.epub")) if settings.inbox_path.exists() else True

    parts = [epub[i : i + CHUNK] for i in range(0, len(epub), CHUNK)]
    assert len(parts) >= 3
    r = chunk(client, csrf, uid, 0, parts[0])
    assert r.status_code == 204 and r.headers["upload-offset"] == str(CHUNK)

    # The response was "lost": the client retries the same chunk. Same bytes -> 204, no write.
    r = chunk(client, csrf, uid, 0, parts[0])
    assert r.status_code == 204 and r.headers["upload-offset"] == str(CHUNK)
    # A different chunk claiming that offset is a conflict, and says where to resume.
    r = chunk(client, csrf, uid, 0, b"x" * CHUNK)
    assert r.status_code == 409 and r.json()["detail"] == "offset_mismatch"
    assert r.headers["upload-offset"] == str(CHUNK)
    # Skipping ahead is also a conflict.
    r = chunk(client, csrf, uid, 2 * CHUNK, parts[2])
    assert r.status_code == 409 and r.headers["upload-offset"] == str(CHUNK)

    # "Network drop": the client asks where to resume.
    assert client.get(f"/api/uploads/{uid}").json()["received_bytes"] == CHUNK
    r = api_post(client, f"/api/uploads/{uid}/finalize", csrf)
    assert r.status_code == 409 and r.json() == {"detail": "incomplete", "received_bytes": CHUNK}

    offset = CHUNK
    for p in parts[1:]:
        r = chunk(client, csrf, uid, offset, p)
        assert r.status_code == 204, r.text
        offset += len(p)
    assert offset == len(epub)

    r = api_post(client, f"/api/uploads/{uid}/finalize", csrf)
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "completed" and done["title"] == "Resumed Book"
    assert done["queued"].startswith("book-") and done["queued"].endswith(".epub")
    assert not part_path(settings, uid).exists()
    assert (settings.inbox_path / done["queued"]).read_bytes() == epub
    # Finalize is idempotent; chunks after completion are gone.
    again = api_post(client, f"/api/uploads/{uid}/finalize", csrf)
    assert again.status_code == 200 and again.json()["queued"] == done["queued"]
    assert chunk(client, csrf, uid, len(epub), b"x").status_code == 410

    drain_inbox(settings)
    titles = [w["title"] for w in client.get("/api/library").json()["works"]]
    assert titles == ["Resumed Book"]
    actions = [e["action"] for e in client.get("/api/activity").json()["events"]]
    assert "upload.complete" in actions and "upload.start" not in actions  # start is audit-only


def test_chunk_validation_rolls_back_bad_bytes(client, owner, settings):
    data = bytes(range(256)) * 10  # 2560 bytes: chunks 1024, 1024, 512
    csrf = owner["csrf"]
    uid = create(client, csrf, data, name="t.mp3", media="audio/mpeg").json()["id"]
    # Wrong digest: 422 and the partial file is truncated back.
    r = chunk(client, csrf, uid, 0, data[:CHUNK], digest=sha(b"other"))
    assert r.status_code == 422 and r.json()["detail"] == "chunk_checksum"
    assert part_path(settings, uid).stat().st_size == 0
    # Not the last chunk, but short.
    r = chunk(client, csrf, uid, 0, data[:100])
    assert r.status_code == 400 and r.json()["detail"] == "short_chunk"
    assert part_path(settings, uid).stat().st_size == 0
    # Larger than chunk_size: refused before anything is written.
    r = chunk(client, csrf, uid, 0, data[: CHUNK + 1])
    assert r.status_code == 413
    assert part_path(settings, uid).stat().st_size == 0
    # Missing headers.
    r = client.put(
        f"/api/uploads/{uid}/chunk",
        content=data[:CHUNK],
        headers={"origin": ORIGIN, "x-csrf-token": csrf, "x-chunk-sha256": sha(data[:CHUNK])},
    )
    assert r.status_code == 400 and r.json()["detail"] == "upload_offset_required"
    # CSRF is required like every other write.
    r = client.put(
        f"/api/uploads/{uid}/chunk",
        content=data[:CHUNK],
        headers={"origin": ORIGIN, "upload-offset": "0", "x-chunk-sha256": sha(data[:CHUNK])},
    )
    assert r.status_code == 403

    for off in (0, CHUNK):
        assert chunk(client, csrf, uid, off, data[off : off + CHUNK]).status_code == 204
    # The final chunk may be short; it may not run past the declared size.
    r = chunk(client, csrf, uid, 2 * CHUNK, data[2 * CHUNK :] + b"!")
    assert r.status_code == 413
    assert part_path(settings, uid).stat().st_size == 2 * CHUNK
    assert chunk(client, csrf, uid, 2 * CHUNK, data[2 * CHUNK :]).status_code == 204

    # No sha256 declared at create: finalize needs one, and checks it.
    r = api_post(client, f"/api/uploads/{uid}/finalize", csrf)
    assert r.status_code == 400 and r.json()["detail"] == "sha256_required"
    r = api_post(client, f"/api/uploads/{uid}/finalize", csrf, {"sha256": sha(b"nope")})
    assert r.status_code == 422 and r.json()["detail"] == "checksum_mismatch"
    s = client.get(f"/api/uploads/{uid}").json()
    assert s["status"] == "failed" and s["error"] == "checksum_mismatch"
    assert not part_path(settings, uid).exists()
    assert chunk(client, csrf, uid, 0, data[:CHUNK]).status_code == 410


def test_not_an_epub_or_audio_fails_finalize(client, owner, settings):
    csrf = owner["csrf"]
    junk = b"this is not a zip file at all"
    uid = create(client, csrf, junk, sha256=sha(junk)).json()["id"]
    assert chunk(client, csrf, uid, 0, junk).status_code == 204
    r = api_post(client, f"/api/uploads/{uid}/finalize", csrf)
    assert r.status_code == 422 and r.json()["detail"] == "not_epub"
    uid = create(client, csrf, junk, name="x.mp3", media="audio/mpeg", sha256=sha(junk)).json()[
        "id"
    ]
    assert chunk(client, csrf, uid, 0, junk).status_code == 204
    r = api_post(client, f"/api/uploads/{uid}/finalize", csrf)
    assert r.status_code == 422 and r.json()["detail"] == "not_audio"
    assert not list(settings.inbox_path.glob("x-*")) if settings.inbox_path.exists() else True


def test_limits_types_quota_and_idempotency(client, owner, settings):
    csrf = owner["csrf"]
    settings.upload_max_bytes = 10_000
    settings.upload_quota_bytes = 25_000
    settings.upload_max_active = 3
    r = create(client, csrf, b"x" * 10_001)
    assert r.status_code == 413 and r.json()["detail"] == "too_large"
    r = create(client, csrf, b"x" * 10, media="application/x-rar")
    assert r.status_code == 415
    r = api_post(
        client, "/api/uploads", csrf, {"filename": "a", "size": 0, "media_type": "audio/mpeg"}
    )
    assert r.status_code == 422

    ids = [create(client, csrf, b"x" * 10_000).json()["id"] for _ in range(2)]
    r = create(client, csrf, b"x" * 6_000)
    assert r.status_code == 413 and r.json()["detail"] == "upload_quota"
    ids.append(create(client, csrf, b"x" * 10).json()["id"])
    r = create(client, csrf, b"x" * 10)
    assert r.status_code == 429 and r.json()["detail"] == "too_many_uploads"
    # Cancelling frees a slot.
    assert api_delete(client, f"/api/uploads/{ids[2]}", csrf).status_code == 204
    assert api_delete(client, f"/api/uploads/{ids[2]}", csrf).status_code == 204  # idempotent
    assert not part_path(settings, ids[2]).exists()
    assert client.get(f"/api/uploads/{ids[2]}").json()["status"] == "cancelled"

    headers = {"origin": ORIGIN, "x-csrf-token": csrf, "idempotency-key": "file-123"}
    body = {"filename": "k.epub", "size": 5, "media_type": "application/epub+zip"}
    first = client.post("/api/uploads", json=body, headers=headers)
    second = client.post("/api/uploads", json=body, headers=headers)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    listed = client.get("/api/uploads").json()["uploads"]
    assert listed[0]["id"] == first.json()["id"] and listed[0]["user"] == "owner"


def test_upload_sessions_are_private_and_readers_cannot_upload(client, owner, settings):
    csrf = owner["csrf"]
    data = b"y" * 50
    owner_uid = create(client, csrf, data).json()["id"]
    make_user(client, csrf, "mia", "member")
    make_user(client, csrf, "rex", "reader")

    mia = login(client, "mia", "mia-correct horse")
    assert client.get(f"/api/uploads/{owner_uid}").status_code == 404
    assert chunk(client, mia, owner_uid, 0, data).status_code == 404
    assert api_post(client, f"/api/uploads/{owner_uid}/finalize", mia).status_code == 404
    assert api_delete(client, f"/api/uploads/{owner_uid}", mia).status_code == 404
    mia_uid = create(client, mia, data).json()["id"]
    assert [u["id"] for u in client.get("/api/uploads").json()["uploads"]] == [mia_uid]

    rex = login(client, "rex", "rex-correct horse")
    assert create(client, rex, data).status_code == 403
    assert client.get("/api/uploads").status_code == 403

    # The owner role may list and cancel anyone's session.
    from tests.conftest import OWNER_PASSWORD

    csrf = login(client, "owner", OWNER_PASSWORD)
    users = {u["id"]: u["user"] for u in client.get("/api/uploads").json()["uploads"]}
    assert users == {owner_uid: "owner", mia_uid: "mia"}
    assert api_delete(client, f"/api/uploads/{mia_uid}", csrf).status_code == 204
    assert not part_path(settings, mia_uid).exists()


def test_sweeper_expires_cleans_orphans_and_forgets_old_rows(client, owner, settings):
    csrf = owner["csrf"]
    live = create(client, csrf, b"a" * 10).json()["id"]
    stale = create(client, csrf, b"b" * 10).json()["id"]
    old_done = create(client, csrf, b"c" * 10).json()["id"]
    orphan = part_path(settings, "no-such-session-xxxxxx")
    orphan.write_bytes(b"left behind")
    young = part_path(settings, "being-created-right-now")  # row not visible yet: keep it
    young.write_bytes(b"")
    hour_ago = time.time() - 3600
    for p in (orphan, part_path(settings, old_done)):
        os.utime(p, (hour_ago, hour_ago))
    with session_factory()() as db:
        rows = {r.public_id: r for r in db.query(UploadSession)}
        rows[stale].expires_at = utcnow() - timedelta(minutes=1)
        rows[old_done].status = UploadStatus.CANCELLED
        rows[old_done].updated_at = utcnow() - timedelta(days=8)
        db.commit()
        counts = sweep(db, settings)
    assert counts == {"expired": 1, "orphans": 2, "forgotten": 1}  # old_done's part + orphan
    assert part_path(settings, live).exists()
    assert not part_path(settings, stale).exists() and not orphan.exists()
    assert young.exists()
    assert client.get(f"/api/uploads/{stale}").json()["status"] == "expired"
    assert client.get(f"/api/uploads/{old_done}").status_code == 404
    r = chunk(client, csrf, stale, 0, b"b" * 10)
    assert r.status_code == 410 and r.json()["status"] == "expired"


def test_direct_epub_upload_keeps_its_own_cap(client, owner, settings, tmp_path):
    settings.upload_direct_max_bytes = 1000
    epub = make_epub(tmp_path / "x.epub", padding=4000).read_bytes()
    r = client.post(
        "/api/books/upload",
        content=epub,
        headers={
            "origin": ORIGIN,
            "x-csrf-token": owner["csrf"],
            "content-type": "application/epub+zip",
        },
    )
    assert r.status_code == 413


def test_sweeper_runs_with_ingest_disabled(settings):
    """The hourly upload sweep lives in the job runner, which must start even without ingest."""
    from fastapi.testclient import TestClient

    from lectern.main import create_app

    settings.ingest_enabled = False
    app = create_app(settings)
    with TestClient(app):
        assert app.state.watcher is None
        assert app.state.jobs is not None and app.state.jobs._thread.is_alive()


def test_session_row_is_committed_before_its_part_file(client, owner, settings, monkeypatch):
    """A sweep in another thread never sees a fresh .part file without its session row."""
    from pathlib import Path

    seen = []
    real_touch = Path.touch

    def touch(self, *a, **kw):
        if self.suffix == ".part":
            with session_factory()() as db:
                seen.append(db.query(UploadSession).filter_by(public_id=self.stem).count())
        return real_touch(self, *a, **kw)

    monkeypatch.setattr(Path, "touch", touch)
    assert create(client, owner["csrf"], b"x" * 10).status_code == 201
    assert seen == [1]


def test_chunk_storage_runs_off_the_event_loop(client, owner, monkeypatch):
    """Database, hashing, and file work for a chunk happen in the threadpool."""
    import asyncio

    from lectern.routes.api import uploads as route

    loops = []
    real_open, real_store = route._open_session, route._store_chunk

    def probe():
        try:
            asyncio.get_running_loop()
            loops.append("event loop")
        except RuntimeError:
            loops.append("worker")

    def open_session(*a):
        probe()
        return real_open(*a)

    def store(*a):
        probe()
        return real_store(*a)

    monkeypatch.setattr(route, "_open_session", open_session)
    monkeypatch.setattr(route, "_store_chunk", store)
    data = b"y" * 10
    uid = create(client, owner["csrf"], data).json()["id"]
    r = chunk(client, owner["csrf"], uid, 0, data)
    assert r.status_code == 204 and r.headers["upload-offset"] == "10"
    assert loops == ["worker", "worker"]
