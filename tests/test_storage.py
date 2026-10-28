"""The storage dashboard (issue #74, docs/storage.md): the scan's categories match the bytes on
disk, cleanup previews exactly what it removes, needs confirmation, removes only regenerable
files, never follows a symlink, waits for a backup, and is owner-only."""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.conftest import api_post, drain_inbox, login, make_epub, make_user

from lectern import backups, jobs, storage
from lectern.db import session_factory
from lectern.main import create_app
from lectern.models import Job, JobStatus


@pytest.fixture
def ssettings(settings, tmp_path):
    target = tmp_path / "usb" / "lectern-backups"
    target.mkdir(parents=True)
    return settings.model_copy(update={"backup_dir": target})


@pytest.fixture
def sclient(ssettings, setup_token):
    app = create_app(ssettings, start_watcher=False, start_background=False)
    with TestClient(app) as c:
        c.setup_token = setup_token.token  # type: ignore[attr-defined]
        yield c


@pytest.fixture
def sowner(sclient):
    from tests.conftest import OWNER_PASSWORD, post_form

    r = post_form(
        sclient,
        "/setup",
        {
            "token": sclient.setup_token,
            "username": "owner",
            "password": OWNER_PASSWORD,
            "password_confirm": OWNER_PASSWORD,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return {"csrf": sclient.get("/api/me").json()["csrf_token"]}


def _write(path: Path, n: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(n))
    return n


def _book(client, settings, title: str) -> dict:
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    make_epub(settings.inbox_path / f"{title}.epub", title=title)
    drain_inbox(settings)
    w = next(w for w in client.get("/api/library").json()["works"] if w["title"] == title)
    return {**w, "dir": settings.library_dir / w["dir_path"]}


def _scan(settings) -> dict:
    result = storage.run_scan(session_factory(), settings)
    assert result is not None
    return result


def _cleanup(client, csrf, action, **params) -> tuple[dict, dict]:
    """Preview, then confirm every previewed item; returns (preview, response)."""
    preview = api_post(client, "/api/admin/storage/preview", csrf, {"action": action, **params})
    assert preview.status_code == 200, preview.text
    keys = [i["key"] for i in preview.json()["items"]]
    r = api_post(
        client,
        "/api/admin/storage/cleanup",
        csrf,
        {"action": action, **params, "keys": keys, "confirm": True},
    )
    assert r.status_code == 202, r.text
    return preview.json(), r.json()


def _run_jobs(settings) -> None:
    jobs.run_pending(session_factory(), settings)


def _last_job() -> Job:
    with session_factory()() as db:
        return db.scalar(select(Job).where(Job.kind == storage.CLEANUP).order_by(Job.id.desc()))


# --- the scan ---------------------------------------------------------------------------------


def test_category_totals_match_the_bytes_on_disk(sclient, sowner, ssettings, tmp_path):
    book = _book(sclient, ssettings, "Sized")
    d, data, target = book["dir"], ssettings.data_dir, ssettings.backup_dir
    expect = {
        "generated_audio": _write(d / "derived" / "audio" / ("a" * 64 + ".m4a"), 3000),
        "ocr": _write(d / "derived" / "ocr-0123456789abcdef.pdf", 5000)
        + _write(d / "derived" / "ocr-0123456789abcdef.json", 700),
        "converted": _write(d / "book-converted-from-pdf.epub", 4000)
        + _write(d / "derived" / "pdf2epub-report.json", 100)
        + _write(d / "derived" / "read-aloud.epub", 900),
        "covers": _write(d / "derived" / "cover-abc.jpg", 1200)
        + _write(data / "revisions" / "1" / "cover-before.jpg", 300),
        "trash": _write(ssettings.library_dir / "_trash" / "files" / "ab12" / "old.epub", 6000),
        "backups": _write(target / "restic" / "data" / "00" / "pack", 9000)
        + _write(target / ".staging" / "lectern-backup" / "manifest.json", 50)
        + _write(data / "pre-restore-20260101T020304Z" / "lectern.db", 2500),
    }
    _write(data / "uploads" / "abc.part", 1500)
    _write(d / "derived" / "pdf-text-0123456789abcdef.json", 800)
    _write(data / "align" / "1.json", 400)
    # A link to a big file outside: never followed, never sized.
    outside = _write(tmp_path / "elsewhere" / "huge.bin", 50_000)
    (d / "derived" / "linked.bin").symlink_to(tmp_path / "elsewhere" / "huge.bin")

    result = _scan(ssettings)
    cats = result["categories"]
    for cat, n in expect.items():
        assert cats[cat]["bytes"] == n, cat
    originals = sum(p.stat().st_size for p in d.iterdir() if p.is_file() and p.suffix == ".epub")
    originals -= 4000  # the converted edition
    originals += (d / "book.json").stat().st_size if (d / "book.json").is_file() else 0
    assert cats["originals"]["bytes"] == originals
    assert cats["other"]["parts"]["upload_parts"] == 1500
    assert cats["derived_text"]["parts"]["pdf_text"] == 800
    assert cats["derived_text"]["parts"]["sentence_maps"] == 400
    assert cats["other"]["parts"]["database"] > 0
    assert result["skipped_links"] == 1
    # Every regular file under the three roots is counted exactly once, nothing more (the
    # database is left out of the comparison: storing the result wrote to it since).
    on_disk = sum(
        p.lstat().st_size
        for root in (ssettings.data_dir, ssettings.library_dir, ssettings.backup_dir)
        for p in root.rglob("*")
        if p.is_file()
        and not p.is_symlink()
        and not (p.parent == data and p.name.startswith("lectern.db"))
    )
    assert result["total_bytes"] - cats["other"]["parts"]["database"] == on_disk
    assert outside not in (c["bytes"] for c in cats.values())
    assert result["largest_works"][0]["id"] == book["id"]
    assert result["pre_restore"] == [{"name": "pre-restore-20260101T020304Z", "bytes": 2500}]


def test_the_page_reads_the_cache_and_is_owner_only(sclient, sowner, ssettings):
    _scan(ssettings)
    view = sclient.get("/api/admin/storage").json()
    assert view["scan"]["scanned_at"] and view["volumes"]
    assert view["volumes"][0]["free"] > 0 and "low" in view["volumes"][0]
    assert len(view["history"]) == 1
    make_user(sclient, sowner["csrf"], "mia", "member")
    make_user(sclient, sowner["csrf"], "rae", "reader")
    for name in ("mia", "rae"):
        csrf = login(sclient, name, f"{name}-correct horse")
        assert sclient.get("/api/admin/storage").status_code == 403
        r = api_post(sclient, "/api/admin/storage/preview", csrf, {"action": "trash"})
        assert r.status_code == 403
        r = api_post(sclient, "/api/admin/storage/scan", csrf)
        assert r.status_code == 403


def test_the_maintenance_tick_rescans_only_when_stale(sclient, sowner, ssettings):
    from datetime import timedelta

    from lectern.models import utcnow

    assert storage.tick(session_factory(), ssettings) is True
    assert storage.tick(session_factory(), ssettings) is False
    later = utcnow() + storage.SCAN_INTERVAL + timedelta(minutes=1)
    assert storage.tick(session_factory(), ssettings, now=later) is True


# --- cleanup ----------------------------------------------------------------------------------


def test_generated_covers_preview_confirm_and_regenerate(sclient, sowner, ssettings):
    book = _book(sclient, ssettings, "Coverless")
    assert sclient.get(f"/library/works/{book['id']}/cover.jpg").status_code == 200
    cached = list((book["dir"] / "derived").glob("cover-*.jpg"))
    assert len(cached) == 1
    epub = next(book["dir"].glob("*.epub"))
    original = epub.read_bytes()

    preview = api_post(sclient, "/api/admin/storage/preview", sowner["csrf"],
                       {"action": "covers"}).json()  # fmt: skip
    assert [i["key"] for i in preview["items"]] == [f"work:{book['id']}"]
    assert preview["total_bytes"] == cached[0].stat().st_size
    # Nothing happens without the confirmation.
    body = {"action": "covers", "keys": [f"work:{book['id']}"], "confirm": False}
    r = api_post(sclient, "/api/admin/storage/cleanup", sowner["csrf"], body)
    assert r.status_code == 400 and cached[0].is_file()

    _cleanup(sclient, sowner["csrf"], "covers")
    _run_jobs(ssettings)
    job = _last_job()
    assert job.status == JobStatus.SUCCEEDED
    assert job.result["freed_bytes"] == preview["total_bytes"]
    assert not cached[0].exists()
    assert epub.read_bytes() == original  # the original is untouched
    # The cover is drawn again on demand.
    assert sclient.get(f"/library/works/{book['id']}/cover.jpg").status_code == 200
    assert list((book["dir"] / "derived").glob("cover-*.jpg"))
    rows = sclient.get("/api/admin/audit").json()["rows"]
    actions = [r["action"] for r in rows]
    assert "storage.cleanup" in actions and "storage.cleanup.done" in actions


def test_originals_are_never_offered_or_accepted(sclient, sowner, ssettings):
    book = _book(sclient, ssettings, "Keep Me")
    epub = next(book["dir"].glob("*.epub"))
    for action in storage.ACTIONS:
        params = {"name": "pre-restore-20260101T000000Z"} if action == "pre_restore" else {}
        r = api_post(sclient, "/api/admin/storage/preview", sowner["csrf"],
                     {"action": action, **params})  # fmt: skip
        assert r.status_code == 200, (action, r.text)
        assert r.json()["items"] == [], action
    # A key that was never offered (the work itself, not in the Trash) is refused.
    body = {"action": "trash", "keys": [f"work:{book['id']}"], "confirm": True}
    r = api_post(sclient, "/api/admin/storage/cleanup", sowner["csrf"], body)
    assert r.status_code == 409
    r = api_post(sclient, "/api/admin/storage/preview", sowner["csrf"], {"action": "originals"})
    assert r.status_code == 422
    assert epub.is_file()


def test_renditions_go_and_symlinks_are_never_followed(sclient, sowner, ssettings, tmp_path):
    book = _book(sclient, ssettings, "Audio")
    audio = book["dir"] / "derived" / "audio"
    _write(audio / ("b" * 64 + ".m4a"), 2000)
    _write(audio / ("b" * 64 + ".m4a.sha256"), 64)
    precious = tmp_path / "precious.m4a"
    _write(precious, 7000)
    (audio / ("c" * 64 + ".m4a")).symlink_to(precious)
    # A work without audio tracks is not offered data-saver cleanup: fake one.
    from lectern.models import AudioTrack

    with session_factory()() as db:
        db.add(AudioTrack(work_id=book["id"], position=0, rel_path="audio/01.mp3",
                          media_type="audio/mpeg", codec="mp3", offset_s=0.0, size=1, sha256="b" * 64,
                          duration_s=1.0))  # fmt: skip
        db.commit()
    preview, _ = _cleanup(sclient, sowner["csrf"], "renditions", work_id=book["id"])
    assert preview["total_bytes"] == 2064
    _run_jobs(ssettings)
    assert _last_job().status == JobStatus.SUCCEEDED
    assert not (audio / ("b" * 64 + ".m4a")).exists()
    assert precious.read_bytes() and precious.stat().st_size == 7000


def test_emptying_the_trash_older_than_n_days(sclient, sowner, ssettings):
    from datetime import timedelta

    from lectern.models import Work, utcnow

    old = _book(sclient, ssettings, "Old")
    new = _book(sclient, ssettings, "New")
    for b in (old, new):
        assert api_post(sclient, f"/api/works/{b['id']}/trash", sowner["csrf"]).status_code == 200
    with session_factory()() as db:
        db.get(Work, old["id"]).trashed_at = utcnow() - timedelta(days=20)
        db.commit()
    preview, _ = _cleanup(sclient, sowner["csrf"], "trash", older_than_days=7)
    assert [i["key"] for i in preview["items"]] == [f"work:{old['id']}"]
    assert preview["total_bytes"] > 0
    _run_jobs(ssettings)
    with session_factory()() as db:
        assert db.get(Work, old["id"]) is None
        assert db.get(Work, new["id"]) is not None  # 2 days old: kept
    assert _last_job().result["freed_bytes"] == preview["total_bytes"]


def test_a_restored_book_is_skipped_not_purged(sclient, sowner, ssettings):
    from lectern.models import Work

    b = _book(sclient, ssettings, "Second Thoughts")
    api_post(sclient, f"/api/works/{b['id']}/trash", sowner["csrf"])
    _cleanup(sclient, sowner["csrf"], "trash")
    r = api_post(sclient, f"/api/trash/works/{b['id']}/restore", sowner["csrf"])
    assert r.status_code == 200, r.text
    _run_jobs(ssettings)
    job = _last_job()
    assert job.status == JobStatus.SUCCEEDED and job.result["skipped"] == 1
    with session_factory()() as db:
        assert db.get(Work, b["id"]).trashed_at is None


def test_a_running_backup_blocks_cleanup_until_it_ends(sclient, sowner, ssettings):
    book = _book(sclient, ssettings, "Blocked")
    sclient.get(f"/library/works/{book['id']}/cover.jpg")
    cover = next((book["dir"] / "derived").glob("cover-*.jpg"))
    _cleanup(sclient, sowner["csrf"], "covers")
    # Another process's backup holds the target's lock.
    fd = os.open(ssettings.backup_dir / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert storage.blocked(ssettings) == "backup_running"
        assert sclient.get("/api/admin/storage").json()["blocked"] == "backup_running"
        _run_jobs(ssettings)
        job = _last_job()
        assert job.status == JobStatus.QUEUED and "backup" in job.error
        assert cover.is_file()
    finally:
        os.close(fd)
    # This process's own backup blocks it too.
    with backups._RUN_LOCK:
        assert storage.blocked(ssettings) == "backup_running"
    with session_factory()() as db:
        db.get(Job, job.id).run_after = job.created_at
        db.commit()
    _run_jobs(ssettings)
    assert _last_job().status == JobStatus.SUCCEEDED
    assert not cover.exists()


def test_one_cleanup_at_a_time(sclient, sowner, ssettings):
    book = _book(sclient, ssettings, "Twice")
    sclient.get(f"/library/works/{book['id']}/cover.jpg")
    _cleanup(sclient, sowner["csrf"], "covers")
    body = {"action": "covers", "keys": [f"work:{book['id']}"], "confirm": True}
    r = api_post(sclient, "/api/admin/storage/cleanup", sowner["csrf"], body)
    assert r.status_code == 409


def test_leftovers_take_only_stale_parts_and_staging(sclient, sowner, ssettings):
    data = ssettings.data_dir
    stale = data / "uploads" / "orphan.part"
    young = data / "uploads" / "fresh.part"
    _write(stale, 1000)
    _write(young, 1000)
    past = time.time() - 2 * 3600
    os.utime(stale, (past, past))
    _write(ssettings.backup_dir / ".staging" / "lectern-backup" / "db" / "lectern.db", 4000)
    scratch = data / "tmp" / "123-old.m4a"
    _write(scratch, 500)
    os.utime(scratch, (time.time() - 3 * 86400,) * 2)
    _write(data / "tmp" / "456-running.m4a", 500)
    preview, _ = _cleanup(sclient, sowner["csrf"], "leftovers")
    assert sorted(i["key"] for i in preview["items"]) == [
        "backup-staging",
        "tmp:123-old.m4a",
        "upload:orphan.part",
    ]
    assert preview["total_bytes"] == 5500
    _run_jobs(ssettings)
    assert not stale.exists() and young.is_file()
    assert not (ssettings.backup_dir / ".staging").exists()
    assert not scratch.exists() and (data / "tmp" / "456-running.m4a").is_file()


def test_pre_restore_folders_one_at_a_time_without_following_links(
    sclient, sowner, ssettings, tmp_path
):
    data = ssettings.data_dir
    a = data / "pre-restore-20260101T020304Z"
    b = data / "pre-restore-20260202T020304Z"
    _write(a / "lectern.db", 3000)
    _write(b / "lectern.db", 4000)
    keep = tmp_path / "outside"
    _write(keep / "important.txt", 10)
    (a / "library").symlink_to(keep, target_is_directory=True)
    r = api_post(sclient, "/api/admin/storage/preview", sowner["csrf"], {"action": "pre_restore"})
    assert r.status_code == 400  # one named folder at a time
    preview, _ = _cleanup(sclient, sowner["csrf"], "pre_restore", name=a.name)
    assert [i["key"] for i in preview["items"]] == [f"pre-restore:{a.name}"]
    assert preview["items"][0]["created_at"] == "2026-01-01T02:03:04Z"
    assert preview["total_bytes"] == 3000
    _run_jobs(ssettings)
    assert not a.exists() and b.is_dir()
    assert (keep / "important.txt").is_file()


def test_cleanup_jobs_are_hidden_from_members(sclient, sowner, ssettings):
    book = _book(sclient, ssettings, "Hidden")
    sclient.get(f"/library/works/{book['id']}/cover.jpg")
    _, r = _cleanup(sclient, sowner["csrf"], "covers")
    job_id = r["job"]["id"]
    make_user(sclient, sowner["csrf"], "mia", "member")
    login(sclient, "mia", "mia-correct horse")
    assert all(j["kind"] != storage.CLEANUP for j in sclient.get("/api/jobs").json()["jobs"])
    assert sclient.get(f"/api/jobs/{job_id}").status_code == 404
