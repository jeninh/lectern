"""Optional backups (docs/backups.md): off by default, restic-only, consistent while running,
restorable with ``lectern restore``. CI sets RESTIC_REQUIRED so nothing here silently skips.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from tests.conftest import (
    OWNER_PASSWORD,
    api_post,
    drain_inbox,
    login,
    make_epub,
    make_user,
    post_form,
    put_json,
)

from lectern import backups, cli
from lectern.backups import BackupConfig, due
from lectern.db import get_engine, session_factory
from lectern.fsops import LIBRARY_LOCK
from lectern.main import create_app


@pytest.fixture(autouse=True)
def _restic_required():
    if shutil.which("restic") is None:
        if os.environ.get("RESTIC_REQUIRED"):
            pytest.fail("RESTIC_REQUIRED is set but restic is not installed")
        pytest.skip("restic not installed")


@pytest.fixture
def bsettings(settings, tmp_path):
    target = tmp_path / "usb" / "lectern-backups"
    target.mkdir(parents=True)
    return settings.model_copy(update={"backup_dir": target})


@pytest.fixture
def bclient(bsettings, setup_token):
    app = create_app(bsettings, start_watcher=False, start_background=False)
    with TestClient(app) as c:
        c.setup_token = setup_token.token  # type: ignore[attr-defined]
        yield c


@pytest.fixture
def bowner(bclient):
    r = post_form(
        bclient,
        "/setup",
        {
            "token": bclient.setup_token,
            "username": "owner",
            "password": OWNER_PASSWORD,
            "password_confirm": OWNER_PASSWORD,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return {"csrf": bclient.get("/api/me").json()["csrf_token"]}


def _add_book(settings, title: str) -> Path:
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    make_epub(settings.inbox_path / "a.epub", title=title, cover=True)
    drain_inbox(settings)
    return next(settings.library_dir.rglob("book.json")).parent


def _run(settings, **cfg) -> dict:
    with session_factory()() as db:
        backups.save_config(db, BackupConfig(**{"enabled": True, **cfg}), 1)
        db.commit()
    return backups.run_and_record(session_factory(), settings, reason="test")


def _restore(settings, monkeypatch, *argv: str) -> None:
    monkeypatch.setattr("lectern.config.get_settings", lambda: settings)
    cli.main(["restore", *argv])


def _usernames(settings) -> set[str]:
    con = sqlite3.connect(settings.db_path)
    try:
        return {r[0] for r in con.execute("SELECT username FROM users")}
    finally:
        con.close()


def _stop(client) -> None:
    client.__exit__(None, None, None)


def test_off_by_default_and_owner_only(bclient, bowner, bsettings):
    view = bclient.get("/api/admin/backups").json()
    assert view["config"] == {
        "enabled": False,
        "include_library": False,
        "retention": 7,
        "hour_utc": 8,
    }
    assert view["status"] == {}
    assert view["target"]["path"] == str(bsettings.backup_dir) and view["target"]["writable"]
    with session_factory()() as db:
        assert not due(backups.load_config(db), {}, datetime(2030, 1, 1, 23, tzinfo=UTC))

    mia = make_user(bclient, bowner["csrf"], "mia", "member")
    bclient.cookies.clear()
    csrf = login(bclient, "mia", mia["password"])
    assert bclient.get("/api/admin/backups").status_code == 403
    assert put_json(bclient, "/api/admin/backups", csrf, {"enabled": True}).status_code == 403
    assert api_post(bclient, "/api/admin/backups/run", csrf).status_code == 403


def test_settings_are_restic_only(bclient, bowner, bsettings, monkeypatch):
    csrf = bowner["csrf"]
    # The tar method is gone: the API rejects it, and a stored pre-release value is ignored.
    r = put_json(bclient, "/api/admin/backups", csrf, {"enabled": True, "method": "tar"})
    assert r.status_code == 422
    with session_factory()() as db:
        backups._put(db, backups.CONFIG_KEY, {"enabled": True, "method": "tar", "retention": 3})
        db.commit()
        assert backups.load_config(db) == BackupConfig(enabled=True, retention=3)

    monkeypatch.setattr(bsettings, "restic_path", "/nonexistent/restic")
    r = put_json(bclient, "/api/admin/backups", csrf, {"enabled": True, "hour_utc": 2})
    assert r.status_code == 400 and r.json()["detail"] == "restic_unavailable"
    monkeypatch.undo()

    put_json(bclient, "/api/admin/backups", csrf, {"enabled": False})
    r = put_json(bclient, "/api/admin/backups", csrf, {"enabled": True, "hour_utc": 2})
    assert r.status_code == 200
    assert r.json()["config"]["hour_utc"] == 2
    assert r.json()["status"]["enabled_at"]  # the schedule starts now, nothing ran yet


def test_snapshot_includes_committed_rows_still_in_the_wal(bclient, bowner, bsettings, tmp_path):
    """A file copy of lectern.db would miss these rows: they are only in lectern.db-wal."""
    with get_engine().begin() as con:
        con.execute(text("PRAGMA wal_autocheckpoint=0"))
        con.execute(text("CREATE TABLE IF NOT EXISTS probe (v TEXT)"))
        con.execute(text("INSERT INTO probe VALUES ('only-in-wal')"))
    assert (bsettings.data_dir / "lectern.db-wal").stat().st_size > 0
    shutil.copy2(bsettings.db_path, tmp_path / "plain-copy.db")
    raw = sqlite3.connect(tmp_path / "plain-copy.db")
    assert "probe" not in {r[0] for r in raw.execute("SELECT name FROM sqlite_master")}
    raw.close()

    manifest = backups.build_snapshot(bsettings, tmp_path / "s" / "stage", include_library=False)
    copy = sqlite3.connect(tmp_path / "s" / "stage" / "db" / "lectern.db")
    assert copy.execute("SELECT v FROM probe").fetchall() == [("only-in-wal",)]
    copy.close()
    from alembic.script import ScriptDirectory

    from lectern.migrate import alembic_config

    head = ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()
    assert manifest["schema_revision"] == head


def test_database_is_copied_under_the_library_lock(bclient, bowner, bsettings, tmp_path):
    """Review: the DB copy must not race an edit that moves covers or folders."""
    stage = tmp_path / "s" / "stage"
    held, release = threading.Event(), threading.Event()

    def editor():
        with LIBRARY_LOCK:
            held.set()
            release.wait(5)

    t = threading.Thread(target=editor)
    t.start()
    held.wait(5)
    b = threading.Thread(target=backups.build_snapshot, args=(bsettings, stage, False))
    b.start()
    time.sleep(0.3)
    assert not (stage / "db" / "lectern.db").exists()  # waiting for the lock
    release.set()
    b.join(10)
    t.join(5)
    assert (stage / "db" / "lectern.db").is_file()


def test_backup_while_running_then_restore_with_the_cli(bclient, bowner, bsettings, monkeypatch):
    work = _add_book(bsettings, "Kept Book")
    (work / "derived").mkdir(exist_ok=True)
    (work / "derived" / "cache.bin").write_bytes(b"regenerable")
    (bsettings.data_dir / "align").mkdir(exist_ok=True)
    (bsettings.data_dir / "align" / "1.json").write_text('{"map": 1}')
    secret_before = (bsettings.data_dir / "secret_key").read_text()
    book_json_before = (work / "book.json").read_text()

    status = _run(bsettings)  # the server is up and holds the database open
    assert status["last_result"] == "ok", status
    assert status["last_location"].startswith("restic:")
    assert status["last_skipped_files"] == 0
    pw = backups.restic_password_file(bsettings)
    assert oct(pw.stat().st_mode & 0o777) == "0o600"
    assert not (bsettings.backup_dir / backups.STAGING_DIR).exists()  # no plaintext left

    # After the backup: a new user, a lost cover, an edited sidecar, a broken sentence map.
    make_user(bclient, bowner["csrf"], "later", "member")
    (work / "cover.jpg").unlink()
    (work / "book.json").write_text('{"edited": "after the backup"}')
    (bsettings.data_dir / "align" / "1.json").write_text("garbage")

    with pytest.raises(SystemExit) as refused:
        _restore(bsettings, monkeypatch)
    assert "Lectern is running" in str(refused.value)
    assert "later" in _usernames(bsettings)

    _stop(bclient)
    _restore(bsettings, monkeypatch)  # latest

    assert _usernames(bsettings) == {"owner"}
    assert (work / "cover.jpg").is_file()
    assert (work / "book.json").read_text() == book_json_before
    assert (work / "book.epub").is_file()  # untouched; restores never delete library files
    assert (bsettings.data_dir / "align" / "1.json").read_text() == '{"map": 1}'
    assert (bsettings.data_dir / "secret_key").read_text() == secret_before
    assert not (bsettings.data_dir / "lectern.db-wal").exists()
    # Review: every replaced file is kept, library files included.
    aside = next(bsettings.data_dir.glob("pre-restore-*"))
    assert {"lectern.db", "secret_key", "align", "library"} <= {p.name for p in aside.iterdir()}
    rel = work.relative_to(bsettings.library_dir)
    assert (aside / "library" / rel / "book.json").read_text() == '{"edited": "after the backup"}'
    assert not list(bsettings.library_dir.rglob(".*.restore-*"))  # no staged leftovers

    # The restored server starts and the owner signs in with the old password.
    app = create_app(bsettings, start_watcher=False, start_background=False)
    with TestClient(app) as c:
        login(c, "owner", OWNER_PASSWORD)
        assert c.get("/api/me").json()["counts"]["works"] == 1


def test_library_files_round_trip_without_derived_inbox_or_symlinks(
    bclient, bowner, bsettings, monkeypatch, tmp_path, caplog
):
    work = _add_book(bsettings, "Whole Library")
    (work / "derived").mkdir(exist_ok=True)
    (work / "derived" / "cache.bin").write_bytes(b"regenerable")
    (bsettings.inbox_path / "waiting.epub").write_bytes(b"not filed yet")
    # Review: an absolute symlink in the library must neither break the backup nor make a
    # restore copy whatever it points at.
    host_secret = tmp_path / "host" / "id_rsa"
    host_secret.parent.mkdir()
    host_secret.write_text("PRIVATE KEY")
    (work / "extra.txt").symlink_to(host_secret)
    (work / "host-dir").symlink_to(host_secret.parent, target_is_directory=True)
    (work / "same-book.epub").symlink_to(work / "book.epub")  # inside the library
    (bsettings.data_dir / "align").mkdir(exist_ok=True)
    (bsettings.data_dir / "align" / "9.json").symlink_to(host_secret)
    epub_bytes = (work / "book.epub").read_bytes()

    with caplog.at_level("INFO", logger="lectern.backups"):
        status = _run(bsettings, include_library=True)
    assert status["last_result"] == "ok", status
    # Never followed; the ones resolving outside the backed-up folders are named.
    outside = {
        str(work / "extra.txt"),
        str(work / "host-dir"),
        str(bsettings.data_dir / "align" / "9.json"),
    }
    assert set(status["last_skipped_links_outside"]) == outside
    warned = {r.getMessage() for r in caplog.records if r.levelname == "WARNING"}
    assert all(any(link in m and "outside" in m for m in warned) for link in outside)
    assert any("same-book.epub" in r.getMessage() for r in caplog.records)
    assert not any("same-book.epub" in m for m in warned)

    _stop(bclient)
    shutil.rmtree(work)
    (bsettings.inbox_path / "waiting.epub").unlink()
    _restore(bsettings, monkeypatch, status["last_location"])
    assert (work / "book.epub").read_bytes() == epub_bytes
    assert (work / "cover.jpg").is_file()
    assert not (work / "derived").exists()
    for name in ("extra.txt", "host-dir", "same-book.epub"):
        assert not (work / name).exists() and not (work / name).is_symlink()
    # Nothing was backed up under align/, so the live link is left as it was.
    assert (bsettings.data_dir / "align" / "9.json").is_symlink()
    assert not list(bsettings.library_dir.rglob("id_rsa"))
    assert not list(bsettings.library_dir.rglob("waiting.epub"))

    shutil.rmtree(work)
    _restore(bsettings, monkeypatch, "--no-library")
    assert not work.exists()


def test_unreadable_file_still_counts_and_retention_still_prunes(bclient, bowner, bsettings):
    """Review: restic exit 3 (snapshot made, a file unreadable) is success with a warning,
    and forget --prune still runs."""
    work = _add_book(bsettings, "Partly Readable")
    locked = work / "locked.bin"
    locked.write_bytes(b"x")
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running as root: permissions are not enforced")
        for _ in range(3):
            status = _run(bsettings, include_library=True, retention=1)
            assert status["last_result"] == "ok", status
            assert status["last_skipped_files"] == 1
            assert status["last_skipped_examples"] == [str(locked)]
    finally:
        locked.chmod(0o600)
    assert len(backups.restic_snapshots(bsettings)) == 1
    notes = bclient.get("/api/notifications").json()["items"]
    assert not [n for n in notes if n["kind"] == "backup.failed"]


def test_retention_keeps_the_newest_snapshots(bclient, bowner, bsettings):
    made = [_run(bsettings, retention=2)["last_location"] for _ in range(3)]
    kept = {f"restic:{s['short_id']}" for s in backups.restic_snapshots(bsettings)}
    assert kept == set(made[1:])


def test_a_busy_target_is_a_skip_not_a_failure(bclient, bowner, bsettings):
    """Review: the CLI and the nightly run overlapping must not alert anyone."""
    ok = _run(bsettings)
    fd = os.open(bsettings.backup_dir / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # another process is backing up
        status = backups.run_and_record(session_factory(), bsettings, reason="nightly")
    finally:
        os.close(fd)
    assert status["skipped"] is True
    assert status["last_result"] == "ok" and status["last_success_at"] == ok["last_success_at"]
    with session_factory()() as db:
        assert "skipped" not in backups.load_status(db)
    notes = bclient.get("/api/notifications").json()["items"]
    assert not [n for n in notes if n["kind"] == "backup.failed"]


def test_stale_plaintext_staging_from_a_killed_run_is_removed(bclient, bowner, bsettings):
    stale = bsettings.backup_dir / backups.STAGING_DIR / backups.STAGE_NAME / "secrets"
    stale.mkdir(parents=True)
    (stale / "secret_key").write_text("left by a killed run")
    assert _run(bsettings)["last_result"] == "ok"
    assert not (bsettings.backup_dir / backups.STAGING_DIR).exists()


def test_failure_alerts_owners_once_a_day(bclient, bowner, bsettings):
    ok = _run(bsettings)
    bsettings.backup_dir.chmod(0o500)  # e.g. the USB drive remounted read-only
    try:
        if os.access(bsettings.backup_dir, os.W_OK):
            pytest.skip("running as root: permissions are not enforced")
        for _ in range(2):
            failed = backups.run_and_record(session_factory(), bsettings, reason="nightly")
    finally:
        bsettings.backup_dir.chmod(0o700)
    assert failed["last_result"] == "failed" and "not writable" in failed["last_error"]
    assert failed["last_success_at"] == ok["last_success_at"]
    notes = [
        n for n in bclient.get("/api/notifications").json()["items"] if n["kind"] == "backup.failed"
    ]
    assert len(notes) == 1 and notes[0]["severity"] == "warning"


def test_run_now_from_the_api(bclient, bowner, bsettings):
    assert api_post(bclient, "/api/admin/backups/run", bowner["csrf"]).status_code == 202
    for _ in range(300):
        view = bclient.get("/api/admin/backups").json()
        if not view["running"] and view["status"].get("last_result"):
            break
        time.sleep(0.05)
    assert view["status"]["last_result"] == "ok", view
    assert view["status"]["last_reason"] == "manual"


def test_nightly_schedule():
    cfg = BackupConfig(enabled=True, hour_utc=3)

    def at(s: str) -> datetime:
        return datetime.fromisoformat(s).replace(tzinfo=UTC)

    enabled = {"enabled_at": "2026-10-21T10:00:00Z"}
    assert not due(cfg, enabled, at("2026-10-21T23:59"))  # enabling does not back up on the spot
    assert not due(cfg, enabled, at("2026-10-22T02:59"))
    assert due(cfg, enabled, at("2026-10-22T03:00"))
    ran = {**enabled, "last_attempt_at": "2026-10-22T03:00:30Z"}
    assert not due(cfg, ran, at("2026-10-22T04:00"))
    assert not due(cfg, ran, at("2026-10-23T02:59"))
    assert due(cfg, ran, at("2026-10-23T12:00"))  # off at 03:00: catches up once on start
    assert not due(cfg.model_copy(update={"enabled": False}), ran, at("2026-10-24T12:00"))


def _crafted_snapshot(bsettings, tmp_path, edit) -> None:
    """A snapshot whose staged files were tampered with by ``edit(stage)``, as an attacker
    with write access to the repository password could make one."""
    stage = tmp_path / "craft" / backups.STAGING_DIR / backups.STAGE_NAME
    backups.build_snapshot(bsettings, stage, include_library=False)
    edit(stage)
    if not (bsettings.backup_dir / "restic" / "config").is_file():
        backups._restic(bsettings, "init", create_password=True)
    backups._restic(
        bsettings,
        "backup",
        "--host",
        "lectern",
        "--tag",
        "lectern",
        str(stage),
        create_password=True,
    )


def test_restore_refuses_a_manifest_that_points_outside_the_backup(
    bclient, bowner, bsettings, monkeypatch, tmp_path
):
    """Review: manifest.library_root is untrusted; it must not read host directories."""
    host = tmp_path / "host-home" / ".ssh"
    host.mkdir(parents=True)
    (host / "id_rsa").write_text("PRIVATE KEY")

    def traverse(stage):
        m = json.loads((stage / "manifest.json").read_text())
        m.update(include_library=True, library_root="/" + "../" * 30 + str(host).lstrip("/"))
        (stage / "manifest.json").write_text(json.dumps(m))

    _crafted_snapshot(bsettings, tmp_path, traverse)
    make_user(bclient, bowner["csrf"], "later", "member")
    _stop(bclient)
    with pytest.raises(SystemExit) as exc:
        _restore(bsettings, monkeypatch)
    assert "invalid library path" in str(exc.value)
    assert not list(bsettings.library_dir.rglob("id_rsa"))
    assert "later" in _usernames(bsettings)
    assert not list(bsettings.data_dir.glob("pre-restore-*"))


def test_restore_never_follows_links_inside_the_snapshot(
    bclient, bowner, bsettings, monkeypatch, tmp_path
):
    """Symlinks planted in the snapshot: the restore refuses it and changes nothing."""
    host = tmp_path / "host-secret"
    host.write_text("HOST FILE")

    def plant(stage):
        for rel in ("secrets/secret_key", "config/lectern.env", "data/align/7.json"):
            (stage / rel).parent.mkdir(parents=True, exist_ok=True)
            (stage / rel).unlink(missing_ok=True)
            (stage / rel).symlink_to(host)
        (stage / "data" / "align" / "8.json").write_text('{"map": 8}')

    _crafted_snapshot(bsettings, tmp_path, plant)
    make_user(bclient, bowner["csrf"], "later", "member")
    _stop(bclient)
    secret_before = (bsettings.data_dir / "secret_key").read_text()
    with pytest.raises(SystemExit) as exc:
        _restore(bsettings, monkeypatch)
    assert "symlink" in str(exc.value)
    assert (bsettings.data_dir / "secret_key").read_text() == secret_before
    assert "later" in _usernames(bsettings)
    assert not (bsettings.data_dir / "restored-lectern.env").exists()
    assert not (bsettings.data_dir / "align").exists()
    assert not list(bsettings.data_dir.glob("pre-restore-*"))
    assert "HOST FILE" not in "".join(
        p.read_text(errors="ignore") for p in bsettings.data_dir.rglob("*") if p.is_file()
    )


def test_restore_refuses_a_backup_from_a_newer_schema(
    bclient, bowner, bsettings, monkeypatch, tmp_path
):
    def newer(stage):
        con = sqlite3.connect(stage / "db" / "lectern.db")
        con.execute("UPDATE alembic_version SET version_num = '9999'")
        con.commit()
        con.close()
        m = json.loads((stage / "manifest.json").read_text())
        m["schema_revision"] = "9999"
        (stage / "manifest.json").write_text(json.dumps(m))

    _crafted_snapshot(bsettings, tmp_path, newer)
    _stop(bclient)
    before = _usernames(bsettings)
    with pytest.raises(SystemExit) as exc:
        _restore(bsettings, monkeypatch)
    assert "newer version" in str(exc.value)
    assert _usernames(bsettings) == before
    assert not list(bsettings.data_dir.glob("pre-restore-*"))


@pytest.mark.parametrize("lost_folder", [False, True], ids=["edited-book", "lost-book-folder"])
def test_a_restore_that_fails_part_way_is_rolled_back(
    bclient, bowner, bsettings, monkeypatch, lost_folder
):
    """Review: a failure after files were moved aside must not leave a half-restored server,
    nor empty folders in the library or in pre-restore-*."""
    work = _add_book(bsettings, "Rollback")
    assert _run(bsettings)["last_result"] == "ok"
    make_user(bclient, bowner["csrf"], "later", "member")
    if lost_folder:
        shutil.rmtree(work)
    else:
        (work / "book.json").write_text('{"edited": true}')
        (work / "cover.jpg").unlink()
    _stop(bclient)
    db_before = bsettings.db_path.read_bytes()
    secret_before = (bsettings.data_dir / "secret_key").read_bytes()

    real_put, calls = backups._Swap.put, []

    def failing_put(self, staged, dest, aside_rel):
        calls.append(dest)
        if dest.name == "cover.jpg":  # the last file, after the DB, key, and book.json
            raise OSError(28, "No space left on device")
        return real_put(self, staged, dest, aside_rel)

    monkeypatch.setattr(backups._Swap, "put", failing_put)
    with pytest.raises(SystemExit) as exc:
        _restore(bsettings, monkeypatch)
    assert "undone" in str(exc.value) and "No space left" in str(exc.value)
    assert bsettings.db_path.read_bytes() == db_before
    assert (bsettings.data_dir / "secret_key").read_bytes() == secret_before
    assert "later" in _usernames(bsettings)
    assert len(calls) >= 4
    if lost_folder:
        assert not work.exists()  # the folders the restore created are gone again
    else:
        assert (work / "book.json").read_text() == '{"edited": true}'
        assert not (work / "cover.jpg").exists()
    assert not list(bsettings.data_dir.glob("pre-restore-*"))
    assert not list(bsettings.data_dir.glob(".*.restore-*"))
    assert not list(bsettings.library_dir.rglob(".*.restore-*"))


def test_restore_on_a_fresh_machine_needs_the_saved_password(
    bclient, bowner, bsettings, monkeypatch
):
    assert _run(bsettings)["last_result"] == "ok"
    _stop(bclient)
    pw = backups.restic_password_file(bsettings)
    saved = bsettings.backup_dir.parent / "saved-password"
    shutil.copy2(pw, saved)
    pw.unlink()
    with pytest.raises(SystemExit) as exc:
        _restore(bsettings, monkeypatch)
    assert "password" in str(exc.value)
    _restore(bsettings, monkeypatch, "--restic-password-file", str(saved))
    assert _usernames(bsettings) == {"owner"}


def test_restore_refuses_paths_that_escape_the_library(
    bclient, bowner, bsettings, monkeypatch, tmp_path
):
    """A book folder that is now a link to elsewhere would make the restore write there."""
    work = _add_book(bsettings, "Escape")
    assert _run(bsettings)["last_result"] == "ok"
    make_user(bclient, bowner["csrf"], "later", "member")
    _stop(bclient)
    elsewhere = tmp_path / "elsewhere"
    shutil.move(work, elsewhere)
    (elsewhere / "cover.jpg").unlink()
    work.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(SystemExit) as exc:
        _restore(bsettings, monkeypatch)
    assert "outside the library" in str(exc.value)
    assert not (elsewhere / "cover.jpg").exists()
    assert "later" in _usernames(bsettings)
    assert not list(bsettings.data_dir.glob("pre-restore-*"))
    assert not list(bsettings.data_dir.glob(".*.restore-*"))


def test_free_space_preflight_and_skipping_the_pre_restore_copy(
    bclient, bowner, bsettings, monkeypatch
):
    """Library on another volume than data (a Pi: USB library, SD-card data): replaced
    library files kept in pre-restore-*/ are copies on the data volume, so they must fit."""
    work = _add_book(bsettings, "Big Sidecar")
    assert _run(bsettings)["last_result"] == "ok"
    make_user(bclient, bowner["csrf"], "later", "member")
    _stop(bclient)
    big = b"x" * (8 * 2**20)
    (work / "book.json").write_bytes(big)  # replaced by the restore
    data_free = bsettings.db_path.stat().st_size + 2 * 2**20
    library = bsettings.library_dir.resolve()
    real_volume = backups._volume

    def volume(p):
        _dev, at = real_volume(p)
        return (1 if at.resolve().is_relative_to(library) else 2), at

    monkeypatch.setattr(backups, "_volume", volume)
    monkeypatch.setattr(
        backups, "_free", lambda at: 10**12 if at.resolve().is_relative_to(library) else data_free
    )
    monkeypatch.setattr(backups, "RESTORE_RESERVE_BYTES", 0)

    with pytest.raises(SystemExit) as exc:
        _restore(bsettings, monkeypatch)
    msg = str(exc.value)
    assert "not enough free space" in msg and "nothing was changed" in msg
    assert str(bsettings.data_dir) in msg and "pre-restore copies" in msg
    assert "--no-pre-restore-copy" in msg
    assert (work / "book.json").read_bytes() == big
    assert "later" in _usernames(bsettings)
    assert not list(bsettings.data_dir.glob("pre-restore-*"))

    _restore(bsettings, monkeypatch, "--no-pre-restore-copy")
    assert _usernames(bsettings) == {"owner"}
    assert (work / "book.json").read_bytes() != big
    aside = next(bsettings.data_dir.glob("pre-restore-*"))
    assert (aside / "lectern.db").is_file()  # data files are always kept
    assert not (aside / "library").exists()
    assert not list(bsettings.library_dir.rglob(".*.replaced-*"))


def test_keep_replaced_setting_turns_the_copy_off(bclient, bowner, bsettings, monkeypatch):
    work = _add_book(bsettings, "Setting")
    assert _run(bsettings)["last_result"] == "ok"
    _stop(bclient)
    (work / "book.json").write_text('{"edited": true}')
    _restore(bsettings.model_copy(update={"restore_keep_replaced": False}), monkeypatch)
    assert (work / "book.json").read_text() != '{"edited": true}'
    assert not (next(bsettings.data_dir.glob("pre-restore-*")) / "library").exists()
    assert not list(bsettings.library_dir.rglob(".*.replaced-*"))


def test_a_symlink_loop_in_the_library_does_not_fail_the_backup(bclient, bowner, bsettings):
    """Re-review: resolve() raises on a loop (RuntimeError on 3.12) after the snapshot exists."""
    work = _add_book(bsettings, "Loop")
    (work / "a").symlink_to(work / "b")
    (work / "b").symlink_to(work / "a")
    status = _run(bsettings, include_library=True)
    assert status["last_result"] == "ok", status
    assert {str(work / "a"), str(work / "b")} <= set(status["last_skipped_links_outside"])
    notes = bclient.get("/api/notifications").json()["items"]
    assert not [n for n in notes if n["kind"] == "backup.failed"]


def test_no_pre_restore_copy_replaces_a_folder_where_a_file_belongs(
    bclient, bowner, bsettings, monkeypatch
):
    """Re-review: the replaced item may be a folder; it must be removed, not crash the restore."""
    work = _add_book(bsettings, "Folder In The Way")
    assert _run(bsettings)["last_result"] == "ok"
    _stop(bclient)
    (work / "cover.jpg").unlink()
    (work / "cover.jpg").mkdir()
    (work / "cover.jpg" / "junk").write_text("x")
    _restore(bsettings, monkeypatch, "--no-pre-restore-copy")
    assert (work / "cover.jpg").is_file()
    assert (bsettings.data_dir / "restored-lectern.env").is_file()  # the steps after the swap ran
    assert not list(bsettings.library_dir.rglob(".*.replaced-*"))
