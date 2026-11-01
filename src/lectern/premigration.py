"""A snapshot of the database before every startup schema upgrade, restored if it fails.

When the database's Alembic revision differs from the code's head, startup:

1. takes ``<data>/premigration.lock`` exclusively (a second server starting on the same data
   directory waits, then finds nothing left to do);
2. checks free space: the snapshot and, on the same volume, the copy a restore stages next to
   the database (the database and its WAL, twice, plus ``LECTERN_PREMIGRATION_FREE_MARGIN_MB``);
3. copies the database with SQLite's online backup API (consistent, WAL included) into
   ``<data>/premigration-snapshots/`` (directory 0700, file 0600), flushed to disk, named
   ``<UTC time>-<from>-to-<to>-<random>.db``;
4. upgrades. On success it keeps the newest ``LECTERN_PREMIGRATION_SNAPSHOT_KEEP`` snapshots.

If the upgrade fails, the engine is disposed, the snapshot is checked, copied next to the
database, flushed, the stale ``-wal``/``-shm`` files are removed, and the copy is renamed over
the database; the result is checked again (``PRAGMA integrity_check`` and the revision). The
server then refuses to start and leaves ``failed-upgrade.json`` in the snapshot directory, so
a restart loop neither retries the same upgrade nor takes a snapshot every few seconds; it is
ignored once the migration scripts change. The snapshot is never deleted on a failure. Every
message names revisions and paths only: never a setting, a key, or the database's contents.

Opting out (``LECTERN_PREMIGRATION_SNAPSHOT=0``) upgrades without a snapshot. See
docs/backups.md, "Schema upgrades at startup".
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import exc as sa_exc
from sqlalchemy.engine import Engine

from lectern.backups import BackupError, _sqlite_copy
from lectern.config import Settings
from lectern.migrate import MIGRATIONS_DIR, alembic_config, upgrade_to_head

log = logging.getLogger("lectern.premigration")

SNAPSHOT_DIR = "premigration-snapshots"
LOCK_NAME = "premigration.lock"
FAILURE_MARKER = "failed-upgrade.json"
SNAPSHOT_RE = re.compile(r"^\d{8}T\d{12}Z-[A-Za-z0-9_.]+-to-[A-Za-z0-9_.]+-[0-9a-f]{8}\.db$")
PARTIAL_SUFFIX = ".partial"


class MigrationRefused(RuntimeError):
    """The server must not start; the message is safe to log and show."""


def snapshot_dir(settings: Settings) -> Path:
    return settings.data_dir / SNAPSHOT_DIR


def upgrade(engine: Engine, settings: Settings) -> Path | None:
    """Upgrade to head with a snapshot first; returns the snapshot taken (None when nothing was
    pending or snapshots are off). Raises MigrationRefused when the server must not start."""
    current, head = _revisions(engine)
    if current == head:
        return None
    with _lock(settings):
        current, head = _revisions(engine)  # another server may have upgraded meanwhile
        if current == head:
            return None
        from_rev, to_rev = _label(current), _label(head)
        if not settings.premigration_snapshot:
            log.warning(
                "upgrading the database from %s to %s WITHOUT a snapshot "
                "(LECTERN_PREMIGRATION_SNAPSHOT=0)",
                from_rev,
                to_rev,
            )
            upgrade_to_head(engine)
            return None
        if not current and not _has_tables(settings.db_path):
            upgrade_to_head(engine)  # a new install: nothing to keep
            return None
        directory = _prepare_dir(settings)
        _check_marker(directory, from_rev, to_rev)
        _check_space(settings, directory)
        snapshot = _take_snapshot(settings, directory, from_rev, to_rev)
        log.info("database snapshot before upgrading %s to %s: %s", from_rev, to_rev, snapshot)
        try:
            upgrade_to_head(engine)
        except Exception as exc:  # noqa: BLE001 - every failure restores
            _restore_and_refuse(engine, settings, snapshot, from_rev, to_rev, exc)
        _forget_marker(directory)
        _prune(directory, snapshot, settings.premigration_snapshot_keep)
        return snapshot


# --- revisions -------------------------------------------------------------------------------


def _revisions(engine: Engine) -> tuple[tuple[str, ...], tuple[str, ...]]:
    script = ScriptDirectory.from_config(alembic_config(str(engine.url)))
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_heads()
    return tuple(sorted(current)), tuple(sorted(script.get_heads()))


def _label(revs: tuple[str, ...]) -> str:
    # Revision ids are ours ("0044"); anything else is made file-name safe.
    return re.sub(r"[^A-Za-z0-9_.]", "_", "_".join(revs) or "none")


def _file_revision(db_file: Path) -> str:
    con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT version_num FROM alembic_version").fetchall()
    finally:
        con.close()
    return _label(tuple(sorted(r[0] for r in rows)))


def _has_tables(db_file: Path) -> bool:
    if not db_file.is_file():
        return False
    con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
        return row is not None
    finally:
        con.close()


def _scripts_fingerprint() -> str:
    h = hashlib.sha256()
    for p in sorted((MIGRATIONS_DIR / "versions").glob("*.py")):
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


# --- directory, lock, marker, space --------------------------------------------------------


@contextlib.contextmanager
def _lock(settings: Settings) -> Iterator[None]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(settings.data_dir / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _prepare_dir(settings: Settings) -> Path:
    d = snapshot_dir(settings)
    if d.is_symlink() or (d.exists() and not d.is_dir()):
        raise MigrationRefused(
            f"Lectern will not start: {d} must be a directory, not a link or a file; "
            "the database was not upgraded"
        )
    d.mkdir(mode=0o700, exist_ok=True)
    d.chmod(0o700)
    # Copies a crashed start left half-written. Only this process writes here (the lock).
    for p in d.iterdir():
        if p.name.endswith(PARTIAL_SUFFIX) and p.is_file() and not p.is_symlink():
            p.unlink()
    return d


def _check_marker(directory: Path, from_rev: str, to_rev: str) -> None:
    marker = directory / FAILURE_MARKER
    if not marker.exists() and not marker.is_symlink():
        return
    try:
        failed = json.loads(marker.read_text())
        same = (failed["from"], failed["to"], failed["scripts"]) == (
            from_rev,
            to_rev,
            _scripts_fingerprint(),
        )
    except (OSError, ValueError, KeyError, TypeError):
        same = True  # unreadable: assume the worst
    if same:
        raise MigrationRefused(
            f"Lectern will not start: upgrading the database from {from_rev} to {to_rev} failed "
            "on an earlier start and was rolled back from its snapshot; Lectern does not retry "
            f"the same upgrade on its own. Read that start's log, fix the cause, then delete "
            f"{marker} to try again (or run the previous Lectern version). See docs/backups.md, "
            "'Schema upgrades at startup'"
        )
    marker.unlink()  # a different upgrade (new code): that failure no longer applies


def _forget_marker(directory: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        (directory / FAILURE_MARKER).unlink()


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _db_bytes(settings: Settings) -> int:
    total = 0
    for p in (settings.db_path, settings.db_path.with_name(settings.db_path.name + "-wal")):
        with contextlib.suppress(FileNotFoundError):
            total += p.stat().st_size
    return total


def _check_space(settings: Settings, directory: Path) -> None:
    size = _db_bytes(settings)
    margin = settings.premigration_free_margin_mb * 2**20
    same_volume = directory.stat().st_dev == settings.db_path.parent.stat().st_dev
    # The snapshot, and the copy a restore stages next to the database.
    need = size * (2 if same_volume else 1) + margin
    free = _free_bytes(directory)
    if not same_volume:
        db_need = size + margin
        db_free = _free_bytes(settings.db_path.parent)
        if db_free < db_need:
            free, need, directory = db_free, db_need, settings.db_path.parent
    if free < need:
        raise MigrationRefused(
            f"Lectern will not start: a schema upgrade is pending and its database snapshot "
            f"needs {_mib(need)} free on the volume of {directory}, but only {_mib(free)} is "
            "free. Free up space (old snapshots in "
            f"{snapshot_dir(settings)} can go), or lower LECTERN_PREMIGRATION_FREE_MARGIN_MB. "
            "The database was not changed"
        )


def _mib(n: int) -> str:
    return f"{n / 2**20:.0f} MiB"


# --- snapshot, restore, retention ----------------------------------------------------------


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _take_snapshot(settings: Settings, directory: Path, from_rev: str, to_rev: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    final = directory / f"{stamp}-{from_rev}-to-{to_rev}-{secrets.token_hex(4)}.db"
    partial = final.with_name(final.name + PARTIAL_SUFFIX)
    try:
        # Created 0600 before SQLite writes into it (an empty file is a valid database).
        os.close(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        _sqlite_copy(settings.db_path, partial)
        if _file_revision(partial) != from_rev:
            raise BackupError("the snapshot does not have the database's revision")
        _fsync(partial)
        os.replace(partial, final)
        _fsync(directory)
    except (OSError, sqlite3.Error, BackupError) as exc:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise MigrationRefused(
            f"Lectern will not start: the database snapshot before upgrading {from_rev} to "
            f"{to_rev} failed ({_reason(exc)}), so the database was not upgraded. Snapshot "
            f"directory: {directory}"
        ) from None
    return final


def _check_copy(path: Path, revision: str) -> None:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()
    if ok != "ok":
        raise BackupError("integrity check failed")
    if _file_revision(path) != revision:
        raise BackupError(f"revision is not {revision}")


def _restore(engine: Engine, db: Path, snapshot: Path, revision: str) -> None:
    engine.dispose()  # no pooled connection may keep the old file or its WAL open
    _check_copy(snapshot, revision)  # a bad snapshot leaves the database untouched
    staged = db.with_name(f".{db.name}.restore-{secrets.token_hex(4)}")
    try:
        with snapshot.open("rb") as src:
            fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as dst:
                shutil.copyfileobj(src, dst, 2**20)
                dst.flush()
                os.fsync(dst.fileno())
        _check_copy(staged, revision)
        # The WAL and index belong to the failed file; left beside the restored one, SQLite would
        # replay them into it. Removed first: a crash in between leaves the failed file without
        # its WAL, still a consistent database, and the snapshot.
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                db.with_name(db.name + suffix).unlink()
        os.replace(staged, db)
        _fsync(db.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            staged.unlink()
    _check_copy(db, revision)


def _restore_and_refuse(
    engine: Engine, settings: Settings, snapshot: Path, from_rev: str, to_rev: str, exc: Exception
) -> None:
    failure = f"upgrading the database from {from_rev} to {to_rev} failed ({_reason(exc)})"
    log.error("%s; restoring the snapshot %s", failure, snapshot)
    try:
        _restore(engine, settings.db_path, snapshot, from_rev)
    except (OSError, sqlite3.Error, BackupError) as rexc:
        raise MigrationRefused(
            f"Lectern will not start: {failure}, and restoring the snapshot {snapshot} failed "
            f"too ({_reason(rexc)}). The database may be half-upgraded; the snapshot is kept. "
            "See docs/backups.md, 'Schema upgrades at startup'"
        ) from None
    marker = snapshot.parent / FAILURE_MARKER
    record = {
        "from": from_rev,
        "to": to_rev,
        "scripts": _scripts_fingerprint(),
        "snapshot": snapshot.name,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    tmp = marker.with_name(marker.name + PARTIAL_SUFFIX)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(record))
        os.replace(tmp, marker)
    except OSError:
        log.warning("could not record the failed upgrade in %s", marker)
    raise MigrationRefused(
        f"Lectern will not start: {failure}. The database was restored to {from_rev} from "
        f"{snapshot}, which is kept. See docs/backups.md, 'Schema upgrades at startup'"
    ) from None


def _reason(exc: BaseException) -> str:
    """The kind of failure, never its full text: a statement's parameters can hold data. A
    DBAPI error's own message (\"no such table: x\") names schema objects only."""
    if isinstance(exc, sa_exc.DBAPIError) and exc.orig is not None:
        return f"{type(exc.orig).__name__}: {exc.orig}"
    if isinstance(exc, sqlite3.Error | BackupError):
        return f"{type(exc).__name__}: {exc}"
    if isinstance(exc, OSError):
        return f"{type(exc).__name__}: {os.strerror(exc.errno) if exc.errno else 'I/O error'}"
    return type(exc).__name__


def _prune(directory: Path, current: Path, keep: int) -> None:
    """Keep ``current`` and the newest ``keep - 1`` others (by the time in their names; a Pi
    without a clock battery may have stamped ``current`` before them)."""
    others = sorted(
        p
        for p in directory.iterdir()
        if SNAPSHOT_RE.match(p.name) and p != current and p.is_file() and not p.is_symlink()
    )
    for old in others[: max(len(others) - (keep - 1), 0)]:
        old.unlink()
        log.info("removed old database snapshot %s", old.name)
