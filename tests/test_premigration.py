"""The database snapshot before a startup schema upgrade, and the restore when it fails
(lectern.premigration, docs/backups.md "Schema upgrades at startup").

Never hard-codes a revision: the database is built at the revision before the current head."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import stat
import threading
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from pydantic import ValidationError

from lectern import premigration
from lectern.backups import BackupError
from lectern.config import Settings
from lectern.db import make_engine
from lectern.main import create_app
from lectern.migrate import alembic_config, upgrade_to_head

SECRET = "TOPSECRET-do-not-log-4f1c"
real_upgrade = upgrade_to_head


def _revs(url: str) -> tuple[str, str]:
    script = ScriptDirectory.from_config(alembic_config(url))
    (head,) = script.get_heads()
    prev = script.get_revision(head).down_revision
    assert isinstance(prev, str)
    return prev, head


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    """A database at the revision before head, with rows of our own."""
    path = tmp_path_factory.mktemp("tpl") / "lectern.db"
    engine = make_engine(f"sqlite+pysqlite:///{path}")
    prev, _ = _revs(str(engine.url))
    cfg = alembic_config(str(engine.url))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, prev)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE canary (id INTEGER PRIMARY KEY, v TEXT)")
        conn.exec_driver_sql(
            "INSERT INTO canary (v) VALUES " + ",".join(f"('row {i}')" for i in range(200))
        )
    engine.dispose()
    return path


def _settings(tmp_path: Path, **kw) -> Settings:
    return Settings(
        data_dir=tmp_path / "data", library_dir=tmp_path / "lib", secret_key=SECRET, **kw
    )


@pytest.fixture
def pending(tmp_path, template):
    """Settings and an engine for a data directory whose database awaits one upgrade."""
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    shutil.copyfile(template, settings.db_path)
    engine = make_engine(settings.db_url)
    yield settings, engine
    engine.dispose()


def _revision(db: Path) -> str:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        con.close()


def _dump(db: Path) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return list(con.iterdump())
    finally:
        con.close()


def _snapshots(settings: Settings) -> list[Path]:
    d = premigration.snapshot_dir(settings)
    return sorted(p for p in d.iterdir() if p.suffix == ".db") if d.exists() else []


def test_settings_defaults_and_bounds(monkeypatch):
    s = Settings()
    assert (s.premigration_snapshot, s.premigration_snapshot_keep) == (True, 5)
    assert s.premigration_free_margin_mb == 64
    monkeypatch.setenv("LECTERN_PREMIGRATION_SNAPSHOT", "")  # empty = default, not off
    assert Settings().premigration_snapshot is True
    monkeypatch.setenv("LECTERN_PREMIGRATION_SNAPSHOT_KEEP", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_up_to_date_and_new_installs_take_no_snapshot(tmp_path):
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    engine = make_engine(settings.db_url)
    assert premigration.upgrade(engine, settings) is None  # new install: migrated, no copy
    assert premigration.upgrade(engine, settings) is None  # normal restart
    assert not premigration.snapshot_dir(settings).exists()
    assert _revision(settings.db_path) == _revs(settings.db_url)[1]
    engine.dispose()


def test_pending_upgrade_snapshots_first(pending):
    settings, engine = pending
    prev, head = _revs(settings.db_url)
    before = _dump(settings.db_path)

    snap = premigration.upgrade(engine, settings)

    assert snap is not None and snap.parent == premigration.snapshot_dir(settings)
    assert f"-{prev}-to-{head}-" in snap.name
    assert stat.S_IMODE(snap.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(snap.stat().st_mode) == 0o600
    assert _revision(snap) == prev and _dump(snap) == before
    assert _revision(settings.db_path) == head
    # The next start has nothing to do and copies nothing.
    assert premigration.upgrade(engine, settings) is None
    assert _snapshots(settings) == [snap]


def test_snapshot_is_consistent_while_a_writer_is_active(pending):
    settings, _ = pending
    prev, head = _revs(settings.db_url)
    writer = sqlite3.connect(settings.db_path, isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")  # committed rows stay in the WAL only
    writer.execute("INSERT INTO canary (v) VALUES ('committed in wal')")
    assert Path(f"{settings.db_path}-wal").stat().st_size > 0
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO canary (v) VALUES ('uncommitted')")
    try:
        directory = premigration._prepare_dir(settings)
        snap = premigration._take_snapshot(settings, directory, prev, head)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    con = sqlite3.connect(snap)
    values = {v for (v,) in con.execute("SELECT v FROM canary")}
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()
    assert "committed in wal" in values and "uncommitted" not in values
    assert len(values) == 201


LEAKED: list[sqlite3.Connection] = []


def _fail_after_real_upgrade(engine):
    """A migration that commits half its work, then breaks (with a secret in its message),
    leaving a connection open with committed frames still in the WAL."""
    real_upgrade(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE canary SET v = 'clobbered'")
        conn.exec_driver_sql("CREATE TABLE junk (x)")
    leak = sqlite3.connect(engine.url.database, isolation_level=None, check_same_thread=False)
    leak.execute("PRAGMA wal_autocheckpoint=0")
    leak.execute("UPDATE canary SET v = 'in the stale wal'")
    LEAKED.append(leak)
    raise RuntimeError(f"migration blew up near {SECRET}")


@pytest.fixture(autouse=True)
def _close_leaked():
    yield
    while LEAKED:
        LEAKED.pop().close()


def test_failed_upgrade_restores_byte_for_byte_and_refuses(pending, monkeypatch, caplog):
    settings, engine = pending
    settings = settings.model_copy(update={"premigration_snapshot_keep": 1})
    prev, head = _revs(settings.db_url)
    before = _dump(settings.db_path)
    d = premigration.snapshot_dir(settings)
    d.mkdir(mode=0o700)
    old = [d / f"2020010{i}T000000000000Z-{prev}-to-{head}-0000000{i}.db" for i in range(1, 4)]
    for p in old:
        p.write_bytes(b"older snapshot")
    monkeypatch.setattr(premigration, "upgrade_to_head", _fail_after_real_upgrade)
    caplog.set_level(logging.INFO)

    with pytest.raises(premigration.MigrationRefused) as err:
        premigration.upgrade(engine, settings)

    message = str(err.value)
    (snap,) = [p for p in _snapshots(settings) if p not in old]
    assert prev in message and head in message and str(snap) in message
    assert "RuntimeError" in message
    assert SECRET not in message and SECRET not in caplog.text
    assert _revision(settings.db_path) == prev
    assert _dump(settings.db_path) == before
    assert settings.db_path.read_bytes() == snap.read_bytes()
    assert not Path(f"{settings.db_path}-wal").exists()
    assert not Path(f"{settings.db_path}-shm").exists()
    assert all(p.exists() for p in old)  # nothing is pruned after a failure
    # The server's own engine (WAL mode) sees the restored rows, not the failed file's WAL.
    reopened = make_engine(settings.db_url)
    with reopened.connect() as conn:
        assert {v for (v,) in conn.exec_driver_sql("SELECT DISTINCT v FROM canary")} == {
            f"row {i}" for i in range(200)
        }
    reopened.dispose()
    assert not list(d.glob("*.partial")) and not list(settings.data_dir.glob(".lectern.db.*"))

    # A restart (the compose restart policy) refuses at once: no retry, no second copy.
    monkeypatch.setattr(premigration, "upgrade_to_head", real_upgrade)
    with pytest.raises(premigration.MigrationRefused, match="earlier start"):
        premigration.upgrade(engine, settings)
    assert len(_snapshots(settings)) == 4
    assert _revision(settings.db_path) == prev

    # Different migration code (a fixed release) tries again; success prunes to the newest.
    monkeypatch.setattr(premigration, "_scripts_fingerprint", lambda: "fixed")
    newest = premigration.upgrade(engine, settings)
    assert _revision(settings.db_path) == head
    assert _snapshots(settings) == [newest]
    assert not (d / premigration.FAILURE_MARKER).exists()


def test_retention_keeps_the_newest_and_leaves_other_files(pending):
    settings, engine = pending
    settings = settings.model_copy(update={"premigration_snapshot_keep": 2})
    d = premigration.snapshot_dir(settings)
    d.mkdir(mode=0o700)
    old = [d / f"2021-0{i}-old.db" for i in range(3)]  # not ours: never touched
    ours = [d / f"2020010{i}T000000000000Z-0001-to-0002-0000000{i}.db" for i in range(1, 6)]
    # Written while the clock was ahead (a Pi has no clock battery): newest by name.
    future = [d / f"209{i}0101T000000000000Z-0001-to-0002-0000000f.db" for i in (8, 9)]
    for p in [*old, *ours, *future]:
        p.write_bytes(b"x")

    snap = premigration.upgrade(engine, settings)

    # The one just taken always stays, with the newest keep - 1 others.
    assert sorted(_snapshots(settings)) == sorted([*old, future[1], snap])


def test_insufficient_free_space_refuses_without_changes(pending, monkeypatch):
    settings, engine = pending
    settings = settings.model_copy(update={"premigration_free_margin_mb": 0})
    prev, head = _revs(settings.db_url)
    size = settings.db_path.stat().st_size  # no WAL: the template was closed cleanly
    called = []
    monkeypatch.setattr(premigration, "upgrade_to_head", called.append)

    # One copy fits, the second (the restore's staging copy) does not.
    monkeypatch.setattr(premigration, "_free_bytes", lambda _p: 2 * size - 1)
    with pytest.raises(premigration.MigrationRefused, match="free") as err:
        premigration.upgrade(engine, settings)
    assert "LECTERN_PREMIGRATION_FREE_MARGIN_MB" in str(err.value)
    assert not called and not _snapshots(settings)
    assert _revision(settings.db_path) == prev

    monkeypatch.setattr(premigration, "_free_bytes", lambda _p: 2 * size)
    premigration.upgrade(engine, settings)
    assert called == [engine] and len(_snapshots(settings)) == 1


def test_free_margin_is_required(pending, monkeypatch):
    settings, engine = pending
    size = settings.db_path.stat().st_size
    monkeypatch.setattr(premigration, "_free_bytes", lambda _p: 2 * size + 10 * 2**20)
    with pytest.raises(premigration.MigrationRefused, match="free"):
        premigration.upgrade(engine, settings)  # default margin: 64 MiB
    assert _revision(settings.db_path) == _revs(settings.db_url)[0]


def test_snapshot_failure_prevents_the_upgrade(pending, monkeypatch):
    settings, engine = pending
    called = []
    monkeypatch.setattr(premigration, "upgrade_to_head", called.append)

    def broken_copy(src, dest):
        dest.write_bytes(b"half a copy")
        raise BackupError("the database copy failed its integrity check")

    monkeypatch.setattr(premigration, "_sqlite_copy", broken_copy)
    with pytest.raises(premigration.MigrationRefused, match="snapshot .* failed"):
        premigration.upgrade(engine, settings)
    assert not called
    assert not list(premigration.snapshot_dir(settings).iterdir())  # partial copy removed
    assert _revision(settings.db_path) == _revs(settings.db_url)[0]


def test_corrupt_snapshot_fails_loudly_and_keeps_the_database(pending, monkeypatch):
    settings, engine = pending
    prev, _ = _revs(settings.db_url)
    before = _dump(settings.db_path)

    def corrupt_then_fail(engine):
        (snap,) = _snapshots(settings)
        snap.write_bytes(b"\0" * 4096)
        raise RuntimeError(SECRET)

    monkeypatch.setattr(premigration, "upgrade_to_head", corrupt_then_fail)
    with pytest.raises(premigration.MigrationRefused, match="restoring the snapshot") as err:
        premigration.upgrade(engine, settings)
    assert "failed too" in str(err.value) and SECRET not in str(err.value)
    assert len(_snapshots(settings)) == 1  # kept, for whoever repairs it by hand
    assert _revision(settings.db_path) == prev and _dump(settings.db_path) == before
    assert not (premigration.snapshot_dir(settings) / premigration.FAILURE_MARKER).exists()


def test_opt_out_upgrades_without_a_snapshot(pending, caplog):
    settings, engine = pending
    settings = settings.model_copy(update={"premigration_snapshot": False})
    assert premigration.upgrade(engine, settings) is None
    assert _revision(settings.db_path) == _revs(settings.db_url)[1]
    assert not premigration.snapshot_dir(settings).exists()
    assert "WITHOUT a snapshot" in caplog.text


def test_concurrent_startups_snapshot_once(pending):
    settings, engine = pending
    other = make_engine(settings.db_url)
    barrier = threading.Barrier(2)
    results, errors = [], []

    def start(e):
        barrier.wait()
        try:
            results.append(premigration.upgrade(e, settings))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=start, args=(e,)) for e in (engine, other)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    other.dispose()
    assert not errors
    assert sorted(results, key=bool) == [None, *_snapshots(settings)]
    assert _revision(settings.db_path) == _revs(settings.db_url)[1]


def test_server_refuses_to_start_after_a_failed_upgrade(pending, monkeypatch, caplog):
    settings, engine = pending
    engine.dispose()
    before = _dump(settings.db_path)
    monkeypatch.setattr(premigration, "upgrade_to_head", _fail_after_real_upgrade)
    app = create_app(settings, start_watcher=False, start_background=False)
    with pytest.raises(premigration.MigrationRefused), TestClient(app):
        pass
    assert _dump(settings.db_path) == before
    assert "Lectern will not start" in caplog.text and SECRET not in caplog.text
