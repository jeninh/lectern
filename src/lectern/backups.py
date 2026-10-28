"""Optional nightly backups and the restore that reads them back (docs/backups.md).

Off by default. The owner turns them on in Admin → Backups; the target directory is
deployment configuration (``LECTERN_BACKUP_DIR``, mounted from ``LECTERN_BACKUP_PATH`` on the
host, e.g. a USB drive on a Raspberry Pi) and cannot be changed from the web.

Backups go into an encrypted, deduplicated restic repository at ``<target>/restic``
(restic ships in the image). One backup is a *snapshot* staged under
``<target>/.staging/lectern-backup/``:

- ``db/lectern.db``: the database through SQLite's online backup API, so it is consistent
  while the server keeps running (no stop, no torn WAL);
- ``secrets/``: the signing key and the restic password (when those are files);
- ``config/``: the ``LECTERN_*`` settings the server runs with (``lectern.env``) and a
  JSON summary without the signing key;
- ``data/align`` and ``data/revisions``: sentence maps (hours of transcription to redo) and
  the cover backups behind Undo;
- ``library-meta/``: every work's ``book.json`` and cover art;
- ``manifest.json``: what is inside, the schema revision, and the Lectern version.

With ``include_library`` the library files themselves (minus ``_inbox``, the regenerable
``derived/`` folders, and symlinks) go into the same snapshot. ``restic forget --keep-last
<retention> --prune`` keeps the newest snapshots. Staging happens on the target, not in the
data directory, so the SD card a Pi boots from only sees reads; it is removed after each run
and before the next one.

``restore`` (``lectern restore``) refuses while a server holds ``<data>/lectern.lock``, checks
the database copy, stages everything next to its destination, then swaps it in. Every file it
replaces, library files included, is moved into ``<data>/pre-restore-<time>/``; if any step
fails the swap is rolled back. Its plan is journalled first (``<data>/restore-journal.json``),
so a crash or power loss part-way through can be finished (``lectern restore --resume``) or
undone (``--undo``); the server refuses to start while the journal exists. The server
migrates the schema on its next start.
"""

from __future__ import annotations

import errno
import fcntl
import filecmp
import json
import logging
import os
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from lectern import __version__, notify
from lectern.config import Settings
from lectern.fsops import LIBRARY_LOCK
from lectern.models import InstanceSetting, utcnow

log = logging.getLogger("lectern.backups")

CONFIG_KEY = "backup_config"
STATUS_KEY = "backup_status"
STAGE_NAME = "lectern-backup"
STAGING_DIR = ".staging"
MANIFEST_FORMAT = 2
# Under <data>: copied as-is. Uploads, scratch, logs, and models are not worth keeping.
DATA_SUBDIRS = ("align", "revisions")
DATA_FILES = ("lectern.db", "lectern.db-wal", "lectern.db-shm", "secret_key")
# config/lectern.env keeps these (restic encrypts the repository); the JSON summary does not.
SECRET_SETTINGS = {"LECTERN_SECRET_KEY"}
# Head room left on a volume after a restore stages its files there.
RESTORE_RESERVE_BYTES = 32 * 2**20
RESTIC_PARTIAL = 3  # "snapshot created, but some source files could not be read"


class BackupError(Exception):
    """A backup or restore that cannot go on; the message is shown to the owner."""


class BackupBusy(BackupError):
    """Another backup holds the target. Not a failure: that run is doing the work."""


class BackupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    include_library: bool = False
    retention: int = Field(default=7, ge=1, le=365)
    # Hour of the day (UTC) the nightly backup starts. The web app shows it in local time.
    hour_utc: int = Field(default=8, ge=0, le=23)


# --- persisted config and status -----------------------------------------------------------


def _get(db: Session, key: str) -> dict:
    row = db.get(InstanceSetting, key)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def _put(db: Session, key: str, value: dict, user_id: int | None = None) -> None:
    row = db.get(InstanceSetting, key)
    if row is None:
        db.add(InstanceSetting(key=key, value=value, updated_by=user_id))
    else:
        row.value = value
        if user_id is not None:
            row.updated_by = user_id
    db.flush()


def load_config(db: Session) -> BackupConfig:
    stored = _get(db, CONFIG_KEY)
    stored.pop("method", None)  # pre-release builds also offered tar; restic is the only one
    try:
        return BackupConfig.model_validate(stored)
    except ValueError:
        log.warning("stored backup settings are invalid; using defaults (off)")
        return BackupConfig()


def save_config(db: Session, cfg: BackupConfig, user_id: int) -> None:
    _put(db, CONFIG_KEY, cfg.model_dump(), user_id)


def load_status(db: Session) -> dict:
    return _get(db, STATUS_KEY)


def record_status(db: Session, **fields) -> dict:
    status = {**load_status(db), **fields}
    _put(db, STATUS_KEY, status)
    return status


# --- target -----------------------------------------------------------------------------------


def restic_password_file(settings: Settings) -> Path:
    return settings.backup_restic_password_file or settings.data_dir / "backup-restic-password"


def target_info(settings: Settings) -> dict:
    """What the Admin page shows about the target before anything runs."""
    d = settings.backup_dir
    info: dict = {
        "path": str(d),
        "exists": d.is_dir(),
        "writable": d.is_dir() and os.access(d, os.W_OK | os.X_OK),
        "free_bytes": None,
        "same_disk_as_data": None,
        "restic_available": shutil.which(settings.restic_path) is not None,
        "restic_password_file": str(restic_password_file(settings)),
    }
    if info["exists"]:
        try:
            info["free_bytes"] = shutil.disk_usage(d).free
            info["same_disk_as_data"] = d.stat().st_dev == settings.data_dir.stat().st_dev
        except OSError:
            pass
    return info


# --- locks ------------------------------------------------------------------------------------


@contextmanager
def server_lock(settings: Settings) -> Iterator[None]:
    """Held (shared) for as long as a server runs on this data directory; ``restore`` needs it
    exclusively. ``flock`` works across containers that bind-mount the same directory."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(settings.data_dir / "lectern.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)


@contextmanager
def _exclusive(path: Path, busy: BackupError) -> Iterator[None]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise busy from None
        yield
    finally:
        os.close(fd)


# --- snapshot ---------------------------------------------------------------------------------


def _walk(
    root: Path, *, skip_inbox: bool = False, links: list[Path] | None = None
) -> Iterator[Path]:
    """Regular files under ``root``, never following or yielding symlinks (each one met,
    file or folder, is appended to ``links``). Skips the inbox (optionally) and every work's
    regenerable ``derived/`` folder."""
    if root.is_symlink():
        if links is not None:
            links.append(root)
        return
    if not root.is_dir():
        return
    for here_s, dirs, files in os.walk(root, followlinks=False):
        here = Path(here_s)
        if skip_inbox and here == root and "_inbox" in dirs:
            dirs.remove("_inbox")
        if here == root and ".lectern-staging" in dirs:  # remote results being published
            dirs.remove(".lectern-staging")
        if "derived" in dirs and (here / "book.json").is_file():
            dirs.remove("derived")
        for name in [d for d in dirs if (here / d).is_symlink()]:
            dirs.remove(name)
            if links is not None:
                links.append(here / name)
        dirs.sort()
        for name in sorted(files):
            p = here / name
            if p.is_symlink():
                if links is not None:
                    links.append(p)
            elif p.is_file():
                yield p


def _resolved(path: Path) -> Path | None:
    """``path.resolve()``, or None when it runs into a symlink loop. A non-strict resolve()
    raises on a loop only before Python 3.13 (later it returns a path, as if there were none);
    a strict one raises on every version, and on a missing target too, which is no loop."""
    try:
        return path.resolve(strict=True)
    except RuntimeError:  # a loop, on Python 3.12
        return None
    except OSError as e:
        if e.errno == errno.ELOOP:
            return None
    return path.resolve()


def _report_links(links: list[Path], roots: tuple[Path, ...]) -> list[str]:
    """Log skipped symlinks; return those that resolve outside every backed-up root."""
    outside = []
    for link in dict.fromkeys(links):
        target = _resolved(link)
        if target is None:
            outside.append(str(link))
            log.warning("backup skipped %s: a symlink that cannot be resolved (a loop?)", link)
            continue
        if not any(target.is_relative_to(r.resolve()) for r in roots):
            outside.append(str(link))
            log.warning(
                "backup skipped %s: a symlink to %s, outside the backed-up folders", link, target
            )
        else:
            log.info("backup skipped %s: symlinks are never backed up", link)
    return outside


def library_files(library: Path, links: list[Path] | None = None) -> list[Path]:
    """Every library file worth keeping: not the inbox, not ``<work>/derived/``, and not
    symlinks (never followed, never backed up; see ``links``)."""
    return list(_walk(library, skip_inbox=True, links=links))


def metadata_files(library: Path, links: list[Path] | None = None) -> list[Path]:
    """``book.json`` and cover art of every work: small, and painful to rebuild by hand."""
    return [
        p
        for p in library_files(library, links)
        if p.name == "book.json"
        or (p.name.startswith("cover.") and (p.parent / "book.json").is_file())
    ]


def _sqlite_copy(src: Path, dest: Path) -> None:
    """Online backup: a consistent copy even while the server writes (WAL included)."""
    if not src.is_file():
        raise BackupError(f"no database at {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target, pages=1024, sleep=0.05)
            target.execute("PRAGMA journal_mode=DELETE")  # one self-contained file
            ok = target.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            target.close()
    finally:
        source.close()
    if ok != "ok":
        raise BackupError(f"the database copy failed its integrity check: {ok}")


def _schema_revision(db_file: Path) -> str | None:
    con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _env_lines() -> str:
    lines = [f"{k}={v}" for k, v in sorted(os.environ.items()) if k.startswith("LECTERN_")]
    return (
        "# LECTERN_* settings the server ran with (from the backup). Merge into your .env;\n"
        "# host-only values (LECTERN_UID, *_PATH, TS_*) are not visible to the container.\n"
        + "\n".join(lines)
        + "\n"
    )


def _private(path: Path) -> None:
    try:
        path.chmod(0o600 if path.is_file() else 0o700)
    except OSError:
        pass


def build_snapshot(
    settings: Settings, stage: Path, include_library: bool, links: list[Path] | None = None
) -> dict:
    """Write the snapshot into ``stage`` (emptied first). Returns the manifest. Symlinks met
    on the way are skipped, not followed, and appended to ``links``."""
    links = [] if links is None else links
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    _private(stage.parent)
    _private(stage)
    db_copy = stage / "db" / "lectern.db"

    # The database, the data side files, and the library metadata are copied together under
    # the library lock, so no edit (a cover replaced, a folder renamed) lands between them.
    # Library *files* (include_library) are read afterwards by restic without the lock: a long
    # read must not block ingest. A book filed meanwhile is simply in the next backup.
    with LIBRARY_LOCK:
        _sqlite_copy(settings.db_path, db_copy)
        for sub in DATA_SUBDIRS:
            for f in _walk(settings.data_dir / sub, links=links):
                dest = stage / "data" / sub / f.relative_to(settings.data_dir / sub)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
        meta = metadata_files(settings.library_dir, links)
        for f in meta:
            dest = stage / "library-meta" / f.relative_to(settings.library_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)

    secrets_dir = stage / "secrets"
    secrets_dir.mkdir()
    kept_secrets = []
    for name, path in (
        ("secret_key", settings.data_dir / "secret_key"),
        ("restic-password", restic_password_file(settings)),
    ):
        if path.is_symlink():
            links.append(path)
        elif path.is_file():
            shutil.copy2(path, secrets_dir / name)
            _private(secrets_dir / name)
            kept_secrets.append(name)

    config_dir = stage / "config"
    config_dir.mkdir()
    (config_dir / "lectern.env").write_text(_env_lines())
    _private(config_dir / "lectern.env")
    summary = {
        k: str(v)
        for k, v in settings.model_dump().items()
        if f"LECTERN_{k.upper()}" not in SECRET_SETTINGS
    }
    (config_dir / "settings.json").write_text(json.dumps(summary, indent=1, sort_keys=True))

    manifest = {
        "format": MANIFEST_FORMAT,
        "lectern_version": __version__,
        "created_at": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_revision": _schema_revision(db_copy),
        "secrets": kept_secrets,
        "include_library": include_library,
        "library_root": str(settings.library_dir.absolute()),
        "library_meta_files": len(meta),
    }
    (stage / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


# --- restic -----------------------------------------------------------------------------------


def _restic_env(settings: Settings, create: bool) -> dict:
    pw = restic_password_file(settings)
    if not pw.is_file() and not create:
        raise BackupError(
            f"the restic password file {pw} is missing; put the password you saved there "
            "(or pass --restic-password-file)"
        )
    if not pw.is_file():
        pw.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(pw, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
        log.warning("created the restic repository password at %s; keep a copy elsewhere", pw)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108 - restic wants a HOME; nothing is written there
        "RESTIC_REPOSITORY": str(settings.backup_dir / "restic"),
        "RESTIC_PASSWORD_FILE": str(pw),
    }


def _restic(
    settings: Settings,
    *args: str,
    stdin: str | None = None,
    timeout: float = 6 * 3600,
    create_password: bool = False,
    ok_codes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    exe = shutil.which(settings.restic_path)
    if exe is None:
        raise BackupError("restic is not installed on the server (it ships in the image)")
    try:
        proc = subprocess.run(  # noqa: S603 - fixed executable, arguments built here
            [exe, "--no-cache", *args],
            input=stdin,
            env=_restic_env(settings, create_password),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise BackupError(f"restic {args[0]} timed out") from None
    if proc.returncode not in ok_codes:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise BackupError(f"restic {args[0]} failed: {' '.join(tail)[:400]}")
    return proc


def _json_lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _restic_backup(settings: Settings, cfg: BackupConfig, stage: Path, links: list[Path]) -> dict:
    repo = settings.backup_dir / "restic"
    if not (repo / "config").is_file():
        _restic(settings, "init", create_password=True)
    files = [stage]
    if cfg.include_library:
        files += library_files(settings.library_dir, links)
    proc = _restic(
        settings,
        "backup",
        "--json",
        "--host",
        "lectern",
        "--tag",
        "lectern",
        "--files-from-verbatim",
        "/dev/stdin",
        stdin="".join(f"{p}\n" for p in files),
        ok_codes=(0, RESTIC_PARTIAL),
    )
    summary = next((m for m in _json_lines(proc.stdout) if m.get("message_type") == "summary"), {})
    if not summary.get("snapshot_id"):
        raise BackupError("restic backup finished without creating a snapshot")
    # restic reports each unreadable file as an "error" message (on stdout or stderr,
    # depending on the version).
    unread = [
        str(m.get("item") or "")
        for m in _json_lines(proc.stdout) + _json_lines(proc.stderr)
        if m.get("message_type") == "error"
    ]
    if proc.returncode == RESTIC_PARTIAL and not unread:
        unread = ["(unknown)"]
    # Partial snapshots still count: prune them like any other so the repository stays bounded.
    _restic(
        settings,
        "forget",
        "--host",
        "lectern",
        "--tag",
        "lectern",
        "--keep-last",
        str(cfg.retention),
        "--prune",
    )
    if unread:
        log.warning("backup skipped %d unreadable file(s): %s", len(unread), ", ".join(unread[:5]))
    return {
        "location": f"restic:{summary['snapshot_id'][:8]}",
        "size_bytes": summary.get("total_bytes_processed"),
        "added_bytes": summary.get("data_added"),
        "skipped_files": len(unread),
        "skipped_examples": unread[:5],
    }


def restic_snapshots(settings: Settings) -> list[dict]:
    proc = _restic(settings, "snapshots", "--json", "--host", "lectern", "--tag", "lectern")
    return json.loads(proc.stdout or "[]")


# --- running ----------------------------------------------------------------------------------

_RUN_LOCK = threading.Lock()


def is_running() -> bool:
    return _RUN_LOCK.locked()


def run_backup(settings: Settings, cfg: BackupConfig) -> dict:
    """One backup with ``cfg``. Raises BackupBusy if another backup holds the target, and
    BackupError (or OSError) on failure."""
    target = settings.backup_dir
    if not target.is_dir():
        raise BackupError(f"backup target {target} does not exist (mount LECTERN_BACKUP_PATH)")
    if not os.access(target, os.W_OK | os.X_OK):
        raise BackupError(
            f"backup target {target} is not writable by this user (uid {os.getuid()})"
        )
    with _exclusive(target / ".lock", BackupBusy("another backup or a restore drill is running")):
        staging = target / STAGING_DIR
        # Left by a run that was killed (the server stopped mid-backup): plaintext database and
        # secrets on the target. We hold the target lock, so nothing else is using it.
        shutil.rmtree(staging, ignore_errors=True)
        try:
            links: list[Path] = []
            manifest = build_snapshot(settings, staging / STAGE_NAME, cfg.include_library, links)
            result = _restic_backup(settings, cfg, staging / STAGE_NAME, links)
            result["links_outside"] = _report_links(
                links, (settings.data_dir, settings.library_dir)
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return {
        **result,
        "include_library": cfg.include_library,
        "schema_revision": manifest["schema_revision"],
    }


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def run_and_record(session_factory, settings: Settings, *, reason: str) -> dict:
    """Run a backup with the stored settings, store the outcome, alert owners on failure.
    Returns the new status, with ``skipped: True`` (not stored) when another backup or a
    restore drill was already running, here or in another process; that is not a failure,
    alerts nobody, and does not count as the night's attempt (it is retried on the next tick)."""
    if not _RUN_LOCK.acquire(blocking=False):
        with session_factory() as db:
            return {**load_status(db), "skipped": True}
    try:
        with session_factory() as db:
            cfg = load_config(db)
            before = load_status(db)
            record_status(db, last_attempt_at=_iso(utcnow()), last_reason=reason)
            db.commit()
        started = utcnow()
        try:
            result = run_backup(settings, cfg)
        except BackupBusy:
            log.info("backup skipped: another backup or a restore drill is running on the target")
            # Not an attempt: the nightly backup stays due and runs once the target is free,
            # instead of losing the night to a restore drill (or another process's backup).
            with session_factory() as db:
                status = record_status(
                    db,
                    last_attempt_at=before.get("last_attempt_at"),
                    last_reason=before.get("last_reason"),
                )
                db.commit()
                return {**status, "skipped": True}
        except Exception as exc:  # noqa: BLE001 - every failure must reach the owner
            message = str(exc) or type(exc).__name__
            if not isinstance(exc, BackupError | OSError | sqlite3.Error):
                log.exception("unexpected backup failure")
            log.error("backup failed: %s", message)
            with session_factory() as db:
                status = record_status(
                    db, last_error=message, last_error_at=_iso(utcnow()), last_result="failed"
                )
                day = utcnow().strftime("%Y-%m-%d")
                for o in notify.owners(db):
                    if not notify.recently_sent(
                        db, o.id, "backup.failed", day, timedelta(hours=20)
                    ):
                        notify.notify(
                            db,
                            o,
                            "backup.failed",
                            "Backup failed",
                            f"{message} Check Admin → Backups. Earlier backups are untouched.",
                            severity="warning",
                            data={"key": day},
                        )
                db.commit()
                return status
        took = (utcnow() - started).total_seconds()
        log.info("backup done in %.1f s: %s", took, result["location"])
        with session_factory() as db:
            status = record_status(
                db,
                last_success_at=_iso(utcnow()),
                last_result="ok",
                last_error=None,
                last_duration_s=round(took, 1),
                last_location=result["location"],
                last_size_bytes=result.get("size_bytes"),
                last_skipped_files=result["skipped_files"],
                last_skipped_examples=result["skipped_examples"],
                last_skipped_links_outside=result["links_outside"][:5],
            )
            db.commit()
            return status
    finally:
        _RUN_LOCK.release()


def due(cfg: BackupConfig, status: dict, now: datetime) -> bool:
    """True once today's slot (``hour_utc``) has passed and no attempt was made since it.
    A server that was off at that hour catches up when it starts, once."""
    if not cfg.enabled:
        return False
    slot = now.replace(hour=cfg.hour_utc, minute=0, second=0, microsecond=0)
    if now < slot:
        slot -= timedelta(days=1)
    # Turning backups on starts the schedule; it does not back up on the spot ("Back up now").
    last = max(
        filter(None, (status.get("last_attempt_at"), status.get("enabled_at"))), default=None
    )
    if not last:
        return True
    last_dt = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return last_dt < slot


def tick(session_factory, settings: Settings, now: datetime | None = None) -> bool:
    """The scheduler's check: run the nightly backup if it is due. True if one ran."""
    now = now or utcnow()
    with session_factory() as db:
        cfg, status = load_config(db), load_status(db)
    if not due(cfg, status, now):
        return False
    run_and_record(session_factory, settings, reason="nightly")
    return True


# --- restore ----------------------------------------------------------------------------------


def _find_stage(root: Path) -> Path:
    """The staged snapshot inside a restored tree: ``…/.staging/lectern-backup``. Library
    files are restored next to it, so a library folder named like it cannot be mistaken."""
    hits = [
        Path(d)
        for d, _dirs, files in os.walk(root, followlinks=False)
        if "manifest.json" in files
        and Path(d).name == STAGE_NAME
        and Path(d).parent.name == STAGING_DIR
    ]
    if len(hits) != 1:
        raise BackupError("this does not look like a Lectern backup (no manifest.json)")
    return hits[0]


def _inside(path: Path, root: Path) -> bool:
    """False for a path through a symlink loop: where it leads cannot be known."""
    target = _resolved(path)
    return target is not None and target.is_relative_to(root.resolve())


def _check_revision(revision: str | None) -> None:
    from alembic.script import ScriptDirectory
    from alembic.util import CommandError

    from lectern.migrate import alembic_config

    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    try:
        known = revision is not None and script.get_revision(revision) is not None
    except CommandError:
        known = False
    if not known:
        raise BackupError(
            f"the backup's database is at schema revision {revision!r}, which this Lectern does "
            "not know: it was made by a newer version. Update Lectern first, then restore."
        )


# --- restore journal --------------------------------------------------------------------------
#
# A restore writes its whole plan to <data>/restore-journal.json (fsynced) before it stages a
# single file, so a crash, power loss, or kill -9 at any point leaves enough behind to finish
# it (``lectern restore --resume``) or put everything back (``--undo``). Progress is not
# logged: each step's state is read back from the files themselves, because a rename is atomic
# and survives a power cut as a whole, while a separate progress record could run ahead of it.
# While the journal exists, neither the server nor another restore starts.

JOURNAL_NAME = "restore-journal.json"
JOURNAL_FORMAT = 1


def journal_path(settings: Settings) -> Path:
    return settings.data_dir / JOURNAL_NAME


def interrupted_restore(settings: Settings) -> str | None:
    """Why nothing may use the data directory yet, or None when no restore was interrupted."""
    path = journal_path(settings)
    if not _exists(path):
        return None
    return (
        f"a restore was interrupted ({path} is still there). Finish it with "
        "`lectern restore --resume`, or put everything back as it was before the restore with "
        "`lectern restore --undo`; see docs/backups.md, 'If a restore is interrupted'"
    )


def _exists(p: Path) -> bool:
    return p.exists() or p.is_symlink()


def _fsync(p: Path) -> None:
    fd = os.open(p, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_tree(p: Path) -> None:
    if p.is_symlink():
        return
    if not p.is_dir():
        _fsync(p)
        return
    for here, _dirs, files in os.walk(p):
        for name in files:
            if not os.path.islink(os.path.join(here, name)):
                _fsync(Path(here) / name)
        _fsync(Path(here))


def _write_journal(path: Path, journal: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(journal, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync(path.parent)


def _moving(p: Path) -> Path:
    """Where a move between volumes copies to before renaming the copy to ``p``."""
    return p.with_name(f".{p.name}.lectern-moving")


def _deleting(p: Path) -> Path:
    """Where a move between volumes renames ``p`` once copied, before deleting it."""
    return p.with_name(f".{p.name}.lectern-deleting")


def _discard_leftovers(*paths: Path) -> None:
    """Remove what an interrupted move between volumes left next to ``paths``."""
    for p in paths:
        _discard(_moving(p))
        _discard(_deleting(p))


def _move(src: Path, dst: Path) -> None:
    """Rename ``src`` to ``dst`` (which must not exist). Between volumes (library files kept in
    pre-restore-*/ on a Pi), copy to a temporary name, make the copy durable, rename it to
    ``dst``, and only then delete ``src``: one complete copy always exists under a known name.
    ``src`` is renamed out of the way before it is deleted, so a folder half-deleted by a crash
    is never mistaken for the original."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    tmp = _moving(dst)
    _discard(tmp)
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src, tmp, symlinks=True)
    else:
        shutil.copy2(src, tmp, follow_symlinks=False)
    _fsync_tree(tmp)
    os.rename(tmp, dst)
    _fsync(dst.parent)
    tomb = _deleting(src)
    _discard(tomb)
    os.rename(src, tomb)
    _fsync(src.parent)
    _discard(tomb)


def _step_paths(step: dict) -> tuple[Path, Path, Path | None]:
    return Path(step["dest"]), Path(step["side"]), Path(step["staged"]) if step["staged"] else None


def _step_forward(step: dict) -> None:
    """Do one swap step, or what is left of it: move the original (``had``) aside, then rename
    the staged copy into place. A step without ``staged`` only moves the original aside (a WAL
    the restored database must not see). Safe to repeat after a crash at any point."""
    dest, side, staged = _step_paths(step)
    _discard_leftovers(dest, side)
    if staged is not None and not _exists(staged):
        return  # already in place
    if step["had"]:
        if _exists(side):
            _discard(dest)  # what a move between volumes had not deleted yet
        else:
            _move(dest, side)
    if staged is not None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, dest)


def _step_back(step: dict) -> None:
    """Undo one swap step, whatever part of it happened: the original comes back from the side,
    and the staged or restored copy goes. Safe to repeat after a crash at any point."""
    dest, side, staged = _step_paths(step)
    _discard_leftovers(dest, side)
    if step["had"]:
        if _exists(side):
            _discard(dest)
            _move(side, dest)
    elif staged is not None and not _exists(staged):
        _discard(dest)  # the restored copy; nothing was there before
    if staged is not None:
        _discard(staged)


def _load_journal(settings: Settings) -> dict:
    """The journal, refusing one that names a path outside the data directory and library."""
    path = journal_path(settings)
    try:
        journal = json.loads(path.read_text())
        if journal.get("format") != JOURNAL_FORMAT:
            raise BackupError(f"{path} has an unknown format {journal.get('format')!r}")
        paths = [
            journal["aside"],
            *journal["created"],
            *(p for s in journal["steps"] for p in (s["dest"], s["side"], s["staged"]) if p),
        ]
        roots = (settings.data_dir, settings.library_dir)
        for p in map(Path, paths):
            # Strictly below a root: "<library>/.." has a parent inside the library too.
            if (
                not p.is_absolute()
                or ".." in p.parts
                or not any(_inside(p.parent, r) for r in roots)
            ):
                raise BackupError(
                    f"{path} names {p}, outside the data directory and the library; refusing "
                    "to touch it"
                )
        scratch = journal.get("scratch")
        if scratch and (
            not Path(scratch).name.startswith(".restore-")
            or Path(scratch).parent.resolve()
            not in {settings.backup_dir.resolve(), settings.data_dir.resolve()}
        ):
            raise BackupError(f"{path} names an unexpected scratch folder {scratch}")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise BackupError(f"cannot read the restore journal {path}: {exc}") from None
    return journal


def _close(journal: dict, path: Path) -> None:
    """Make every change durable, then drop the scratch copy (plaintext) and the journal."""
    if journal.get("scratch"):
        _discard(Path(journal["scratch"]))
    os.sync()
    path.unlink()
    _fsync(path.parent)


def _undo(journal: dict, path: Path) -> None:
    if journal["phase"] == "finishing":
        raise BackupError(
            "the restore had already swapped every file in and was tidying up, so it can no "
            "longer be undone; run `lectern restore --resume` to finish it. The files it "
            f"replaced are in {journal['aside']}"
        )
    if journal["phase"] != "undoing":
        journal["phase"] = "undoing"
        _write_journal(path, journal)
    for step in reversed(journal["steps"]):
        _step_back(step)
    _remove_created([Path(d) for d in journal["created"]])
    _remove_empty_tree(Path(journal["aside"]))
    _close(journal, path)


def _finish(journal: dict, path: Path, data: Path) -> None:
    """After the swap: drop replaced library files nobody asked to keep, tidy up."""
    if journal["phase"] != "finishing":
        os.sync()  # every rename durable before anything replaced is deleted
        journal["phase"] = "finishing"
        _write_journal(path, journal)
    for step in journal["steps"]:
        if not step["keep"]:
            _discard(Path(step["side"]))  # a file, or a folder that sat where a file belongs
    _remove_empty_tree(Path(journal["aside"]))
    for name in ("lectern.db", "secret_key", "restored-lectern.env"):
        if (data / name).is_file():
            _private(data / name)
    _close(journal, path)


def _report(journal: dict, data: Path, log_fn) -> dict:
    s, aside = journal["summary"], Path(journal["aside"])
    moved = aside.is_dir()
    config_copy = data / "restored-lectern.env"
    has_config = any(Path(step["dest"]) == config_copy for step in journal["steps"])
    result = {
        "created_at": s.get("created_at"),
        "lectern_version": s.get("lectern_version"),
        "schema_revision": s.get("schema_revision"),
        "moved_aside": str(aside) if moved else None,
        "library_files": s.get("library_files", 0),
        "config_copy": str(config_copy) if has_config else None,
    }
    log_fn(
        f"restored the backup from {result['created_at']} (Lectern {result['lectern_version']}); "
        f"{result['library_files']} library file(s) restored."
    )
    if moved:
        log_fn(f"the files it replaced are in {aside}")
    if has_config:
        log_fn(f"the LECTERN_* settings it ran with are in {config_copy}; compare with your .env")
    return result


def _server_running() -> BackupError:
    return BackupError(
        "Lectern is running on this data directory. Stop it first "
        "(lectern-compose stop lectern), then run the restore again."
    )


def recover(settings: Settings, action: str, *, log_fn=print) -> dict | None:
    """Finish (``resume``) or undo (``undo``) a restore that was interrupted. Undo puts back
    exactly what was there before, byte for byte; resume returns the restore's result."""
    path = journal_path(settings)
    if not _exists(path):
        raise BackupError(f"there is no interrupted restore to {action} (no {path})")
    with _exclusive(settings.data_dir / "lectern.lock", _server_running()):
        journal = _load_journal(settings)
        if action == "undo":
            _undo(journal, path)
            log_fn("the interrupted restore was undone; everything is as it was before it.")
            return None
        if journal["phase"] == "staging":
            raise BackupError(
                "the restore was interrupted while copying files, before it changed anything; "
                "run `lectern restore --undo` to clear what it left, then restore again"
            )
        if journal["phase"] == "undoing":
            raise BackupError(
                "undoing the restore was interrupted; run `lectern restore --undo` again"
            )
        for step in journal["steps"]:
            _step_forward(step)
        _finish(journal, path, settings.data_dir)
        return _report(journal, settings.data_dir, log_fn)


def _regular(p: Path) -> bool:
    """A real file, not a symlink: nothing in a snapshot may point us at a host file."""
    return p.is_file() and not p.is_symlink()


def _links_in(root: Path) -> list[Path]:
    """Every symlink (file or folder) under ``root``, without following any."""
    out = []
    for here, dirs, files in os.walk(root, followlinks=False):
        out += [Path(here) / n for n in (*dirs, *files) if (Path(here) / n).is_symlink()]
    return out


def _missing_dirs(paths: list[Path]) -> list[Path]:
    """Folders among ``paths`` and their parents that do not exist yet, outermost first: the
    ones a restore creates, and removes again when it is undone."""
    out: dict[Path, None] = {}
    for path in paths:
        missing = []
        for d in (path, *path.parents):
            if d.exists():
                break
            missing.append(d)
        out.update(dict.fromkeys(reversed(missing)))
    return list(out)


def _remove_created(created: list[Path]) -> None:
    for d in reversed(created):
        try:
            d.rmdir()  # only if still empty
        except OSError:
            pass


def _discard(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=True)
    else:
        p.unlink(missing_ok=True)


def _remove_empty_tree(root: Path) -> None:
    """Delete ``root`` if it holds folders but no files (an aside nothing was moved into)."""
    if root.is_dir() and not any(p.is_file() or p.is_symlink() for p in root.rglob("*")):
        shutil.rmtree(root, ignore_errors=True)


def _stage_copy(src: Path, staged: Path) -> None:
    """Copy ``src`` to ``staged``, next to its destination (same filesystem, so the swap is a
    rename). Folders are copied file by file through ``_walk``: regular files only, symlinks
    never followed."""
    staged.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        staged.mkdir()
        for f in _walk(src):
            target = staged / f.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
    else:
        shutil.copy2(src, staged)


def _size(p: Path) -> int:
    if p.is_symlink():
        return 0
    if p.is_dir():
        return sum(f.stat().st_size for f in _walk(p))
    return p.stat().st_size if p.is_file() else 0


def _volume(p: Path) -> tuple[int, Path]:
    """(device, nearest existing folder) of ``p``, which may not exist yet."""
    while not p.exists():
        p = p.parent
    return p.stat().st_dev, p


def _free(p: Path) -> int:
    return shutil.disk_usage(p).free


def _mib(n: int) -> str:
    return f"{n / 2**20:.1f} MiB"


def _check_space(items: list[tuple[Path, Path, Path, bool]]) -> None:
    """Refuse before anything is touched when a volume cannot take the restore: every file is
    staged next to its destination, and a replaced file kept in pre-restore-*/ on another
    volume (library files, when the library is not on the data volume) is a copy there."""
    need: dict[int, list] = {}

    def add(dev_at: tuple[int, Path], n: int, what: str) -> None:
        entry = need.setdefault(dev_at[0], [0, dev_at[1], set()])
        entry[0] += n
        entry[2].add(what)

    for src, dest, side, _keep in items:
        vol = _volume(dest.parent)
        add(vol, _size(src), "restored files")
        if dest.exists() and not dest.is_symlink():
            side_vol = _volume(side.parent)
            if side_vol[0] != vol[0]:
                add(side_vol, _size(dest), "pre-restore copies of replaced library files")
    short = []
    for n, at, what in need.values():
        free = _free(at)
        if n + RESTORE_RESERVE_BYTES > free:
            short.append(
                f"{at} needs {_mib(n)} for {' and '.join(sorted(what))} plus "
                f"{_mib(RESTORE_RESERVE_BYTES)} to spare, but has {_mib(free)} free"
            )
    if short:
        raise BackupError(
            "not enough free space; nothing was changed. "
            + "; ".join(short)
            + ". Free some space, restore the data side only (--no-library), or skip the "
            "pre-restore copies of replaced library files (--no-pre-restore-copy, or "
            "LECTERN_RESTORE_KEEP_REPLACED=0)."
        )


@contextmanager
def _scratch(parent: Path) -> Iterator[Path]:
    """A private scratch folder for the snapshot, removed afterwards (it holds plaintext). The
    restore journal records it, so recovery removes it too after a crash."""
    tmp = Path(tempfile.mkdtemp(prefix=".restore-", dir=parent))
    try:
        yield tmp
    finally:
        _discard(tmp)


def restore(
    settings: Settings,
    source: str = "latest",
    *,
    restore_library: bool = True,
    keep_replaced: bool | None = None,
    log_fn=print,
) -> dict:
    """Restore restic snapshot ``source`` (an id, ``latest``, or ``restic:<id>``).

    Refuses while a server runs on this data directory, while an interrupted restore is
    unresolved (``recover``), when the snapshot holds a symlink or a path that would land
    outside the library, and when a volume lacks the free space. Replaced data files go to
    ``<data>/pre-restore-<time>/``; replaced library files go to its ``library/`` folder, or,
    with ``keep_replaced`` off (``LECTERN_RESTORE_KEEP_REPLACED``), are deleted once the
    restore succeeded. Library files are never otherwise deleted. A failure part-way through
    rolls everything back; a crash leaves the journal for ``recover``."""
    data, library = settings.data_dir, settings.library_dir
    if keep_replaced is None:
        keep_replaced = settings.restore_keep_replaced
    data.mkdir(parents=True, exist_ok=True)
    snap = source.removeprefix("restic:") or "latest"
    with _exclusive(data / "lectern.lock", _server_running()):
        if pending := interrupted_restore(settings):
            raise BackupError(pending)
        scratch_parent = settings.backup_dir if os.access(settings.backup_dir, os.W_OK) else data
        with _scratch(scratch_parent) as tmp_path:
            log_fn(f"restoring restic snapshot {snap} into scratch space…")
            _restic(
                settings,
                "restore",
                snap,
                "--host",
                "lectern",
                "--tag",
                "lectern",
                "--target",
                str(tmp_path),
            )
            # Lectern never backs up a symlink, so one in the snapshot means it was tampered
            # with (or made by something else). Refuse rather than guess what it should be.
            links = _links_in(tmp_path)
            if links:
                raise BackupError(
                    f"the backup contains {len(links)} symlink(s), e.g. "
                    f"{links[0].relative_to(tmp_path)}; Lectern never backs up links, so it "
                    "refuses to restore this snapshot"
                )
            stage = _find_stage(tmp_path)
            manifest = json.loads((stage / "manifest.json").read_text())
            if manifest.get("format") != MANIFEST_FORMAT:
                raise BackupError(f"unknown backup format {manifest.get('format')!r}")
            db_copy = stage / "db" / "lectern.db"
            if not _regular(db_copy):
                raise BackupError("the backup has no database")
            con = sqlite3.connect(f"file:{db_copy}?mode=ro", uri=True)
            try:
                ok = con.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                con.close()
            if ok != "ok":
                raise BackupError(f"the backup's database is damaged: {ok}")
            _check_revision(manifest.get("schema_revision"))

            # Library sources: metadata always; files at the absolute path the server backed up,
            # which restic restores under the scratch dir. The manifest is untrusted: the path
            # must stay inside the scratch dir.
            sources = [stage / "library-meta"]
            if manifest.get("include_library"):
                root = str(manifest.get("library_root") or "")
                candidate = tmp_path / root.lstrip("/")
                if (
                    not root.startswith("/")
                    or not _inside(candidate, tmp_path)
                    or candidate.resolve() == tmp_path.resolve()
                    or _inside(stage, candidate)
                ):
                    raise BackupError("the backup's manifest names an invalid library path")
                sources.append(candidate)

            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            tag = f"restore-{stamp}"
            aside = data / f"pre-restore-{stamp}"

            # 1. Plan: (source, destination, where the original goes, keep it afterwards).
            items: list[tuple[Path, Path, Path, bool]] = [
                (db_copy, data / "lectern.db", aside / "lectern.db", True)
            ]
            if _regular(stage / "secrets" / "secret_key"):
                items.append(
                    (
                        stage / "secrets" / "secret_key",
                        data / "secret_key",
                        aside / "secret_key",
                        True,
                    )
                )
            for sub in DATA_SUBDIRS:
                if (stage / "data" / sub).is_dir():
                    items.append((stage / "data" / sub, data / sub, aside / sub, True))
            if _regular(stage / "config" / "lectern.env"):
                env = data / "restored-lectern.env"
                items.append((stage / "config" / "lectern.env", env, aside / env.name, True))
            library_files_n = 0
            if restore_library:
                library.mkdir(parents=True, exist_ok=True)
                seen: set[Path] = set()
                for src_root in sources:
                    for f in _walk(src_root):
                        rel = f.relative_to(src_root)
                        dest = library / rel
                        # Resolving catches a library folder that is a link to elsewhere.
                        if not _inside(dest, library):
                            raise BackupError(
                                f"refusing to restore {rel}: it would be written outside the "
                                f"library, to {_resolved(dest) or 'a symlink loop'}"
                            )
                        if dest in seen:
                            continue
                        seen.add(dest)
                        if _regular(dest) and filecmp.cmp(f, dest, shallow=False):
                            continue  # identical; nothing to replace
                        side = (
                            aside / "library" / rel
                            if keep_replaced
                            else dest.with_name(f".{dest.name}.replaced-{tag}")
                        )
                        items.append((f, dest, side, keep_replaced))
                        library_files_n += 1
            _check_space(items)

            # The swap's steps, in order: the WAL and shared memory the restored database must
            # not see go aside first, then each staged copy replaces its destination.
            steps = [
                {
                    "dest": str(data / n),
                    "staged": None,
                    "side": str(aside / n),
                    "keep": True,
                    "had": True,
                }
                for n in ("lectern.db-wal", "lectern.db-shm")
                if _exists(data / n)
            ]
            for _src, dest, side, keep in items:
                staged = dest.with_name(f".{dest.name}.{tag}")
                steps.append(
                    {
                        "dest": str(dest),
                        "staged": str(staged),
                        "side": str(side),
                        "keep": keep,
                        "had": _exists(dest),
                    }
                )
            journal = {
                "format": JOURNAL_FORMAT,
                "phase": "staging",
                "aside": str(aside),
                "scratch": str(tmp_path),
                "created": [str(d) for d in _missing_dirs([d for _, d, _, _ in items])],
                "steps": steps,
                "summary": {
                    "created_at": manifest.get("created_at"),
                    "lectern_version": manifest.get("lectern_version"),
                    "schema_revision": manifest.get("schema_revision"),
                    "library_files": library_files_n,
                },
            }
            # Outside the journal: a copy the restore needs no matter how it ends.
            pw = restic_password_file(settings)
            if _regular(stage / "secrets" / "restic-password") and not pw.exists():
                shutil.copy2(stage / "secrets" / "restic-password", pw)
                _private(pw)
            jpath = journal_path(settings)
            try:
                _write_journal(jpath, journal)
            except OSError as exc:
                for p in (jpath, jpath.with_name(f".{jpath.name}.tmp")):
                    p.unlink(missing_ok=True)
                raise BackupError(f"cannot write {jpath}; nothing was changed: {exc}") from exc
            try:
                # 2. Stage everything next to its destination. Nothing existing changes yet,
                #    so a full disk or a read error here leaves the server exactly as it was.
                for (src, *_), step in zip(items, steps[-len(items) :], strict=True):
                    _stage_copy(src, Path(step["staged"]))
                os.sync()  # every staged copy durable before the first original moves
                journal["phase"] = "swapping"
                _write_journal(jpath, journal)
                # 3. Swap: move originals aside and staged files in.
                for step in steps:
                    _step_forward(step)
            except BaseException as exc:
                try:
                    _undo(journal, jpath)
                except Exception as rb:  # noqa: BLE001
                    raise BackupError(
                        f"restore failed ({exc}) and could not be fully undone ({rb}); run "
                        "`lectern restore --undo` to finish undoing it, see docs/backups.md, "
                        "'If a restore is interrupted'"
                    ) from exc
                raise BackupError(f"restore failed and was undone; nothing changed: {exc}") from exc

            _finish(journal, jpath, data)
    return _report(journal, data, log_fn)
