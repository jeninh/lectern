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
fails the swap is rolled back. The server migrates the schema on its next start.
"""

from __future__ import annotations

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


def _walk(root: Path, *, skip_inbox: bool = False) -> Iterator[Path]:
    """Regular files under ``root``, never following or yielding symlinks. Skips the inbox
    (optionally) and every work's regenerable ``derived/`` folder."""
    if not root.is_dir():
        return
    for here_s, dirs, files in os.walk(root, followlinks=False):
        here = Path(here_s)
        if skip_inbox and here == root and "_inbox" in dirs:
            dirs.remove("_inbox")
        if "derived" in dirs and (here / "book.json").is_file():
            dirs.remove("derived")
        dirs.sort()
        for name in sorted(files):
            p = here / name
            if not p.is_symlink() and p.is_file():
                yield p


def library_files(library: Path) -> list[Path]:
    """Every library file worth keeping: not the inbox, not ``<work>/derived/``, and not
    symlinks (restore never recreates links)."""
    return list(_walk(library, skip_inbox=True))


def metadata_files(library: Path) -> list[Path]:
    """``book.json`` and cover art of every work: small, and painful to rebuild by hand."""
    return [
        p
        for p in library_files(library)
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


def build_snapshot(settings: Settings, stage: Path, include_library: bool) -> dict:
    """Write the snapshot into ``stage`` (emptied first). Returns the manifest."""
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
            for f in _walk(settings.data_dir / sub):
                dest = stage / "data" / sub / f.relative_to(settings.data_dir / sub)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
        meta = metadata_files(settings.library_dir)
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
        if path.is_file():
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


def _restic_backup(settings: Settings, cfg: BackupConfig, stage: Path) -> dict:
    repo = settings.backup_dir / "restic"
    if not (repo / "config").is_file():
        _restic(settings, "init", create_password=True)
    files = [stage]
    if cfg.include_library:
        files += library_files(settings.library_dir)
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
    with _exclusive(target / ".lock", BackupBusy("another backup is already running")):
        staging = target / STAGING_DIR
        # Left by a run that was killed (the server stopped mid-backup): plaintext database and
        # secrets on the target. We hold the target lock, so nothing else is using it.
        shutil.rmtree(staging, ignore_errors=True)
        try:
            manifest = build_snapshot(settings, staging / STAGE_NAME, cfg.include_library)
            result = _restic_backup(settings, cfg, staging / STAGE_NAME)
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
    Returns the new status, with ``skipped: True`` (not stored) when another backup was
    already running, here or in another process; that is not a failure and alerts nobody."""
    if not _RUN_LOCK.acquire(blocking=False):
        with session_factory() as db:
            return {**load_status(db), "skipped": True}
    try:
        with session_factory() as db:
            cfg = load_config(db)
            record_status(db, last_attempt_at=_iso(utcnow()), last_reason=reason)
            db.commit()
        started = utcnow()
        try:
            result = run_backup(settings, cfg)
        except BackupBusy:
            log.info("backup skipped: another backup is already running on the target")
            with session_factory() as db:
                return {**load_status(db), "skipped": True}
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
    return path.resolve().is_relative_to(root.resolve())


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


class _Swap:
    """Put staged files in place, moving what they replace aside, and undo it all on failure.

    Each step is recorded only after it happened, so ``rollback`` reverses exactly what was
    done: a staged file moved in goes back out, and the original comes back from the aside."""

    def __init__(self, aside: Path) -> None:
        self.aside = aside
        self.steps: list[tuple[Path, Path | None]] = []  # (destination, its original aside)

    def put(self, staged: Path, dest: Path, aside_rel: Path) -> None:
        saved = None
        if dest.exists() or dest.is_symlink():
            saved = self.aside / aside_rel
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(dest, saved)
        self.steps.append((dest, saved))
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, dest)

    def remove(self, dest: Path, aside_rel: Path) -> None:
        """Move ``dest`` aside without replacing it (a WAL the restored DB must not see)."""
        saved = self.aside / aside_rel
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(dest, saved)
        self.steps.append((dest, saved))

    def rollback(self) -> None:
        for dest, saved in reversed(self.steps):
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest)
            else:
                dest.unlink(missing_ok=True)
            if saved is not None:
                shutil.move(saved, dest)
        self.steps.clear()


def _regular(p: Path) -> bool:
    """A real file, not a symlink: nothing in a snapshot may point us at a host file."""
    return p.is_file() and not p.is_symlink()


def _mkparents(path: Path, created: list[Path]) -> None:
    """``mkdir -p path``, recording each folder it creates (outermost first)."""
    missing = []
    for d in (path, *path.parents):
        if d.exists():
            break
        missing.append(d)
    path.mkdir(parents=True, exist_ok=True)
    created += reversed(missing)


def _remove_created(created: list[Path]) -> None:
    for d in reversed(created):
        try:
            d.rmdir()  # only if still empty
        except OSError:
            pass


def _stage_copy(src: Path, dest: Path, tag: str, created: list[Path]) -> Path:
    """Copy ``src`` next to ``dest`` (same filesystem, so the swap is a rename). Folders are
    copied file by file through ``_walk``: regular files only, symlinks never followed."""
    _mkparents(dest.parent, created)
    staged = dest.with_name(f".{dest.name}.{tag}")
    if src.is_dir():
        staged.mkdir()
        for f in _walk(src):
            target = staged / f.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
    else:
        shutil.copy2(src, staged)
    return staged


def restore(
    settings: Settings,
    source: str = "latest",
    *,
    restore_library: bool = True,
    log_fn=print,
) -> dict:
    """Restore restic snapshot ``source`` (an id, ``latest``, or ``restic:<id>``).

    Refuses while a server runs on this data directory. Every replaced file goes to
    ``<data>/pre-restore-<time>/`` (library files under ``library/``); library files are added
    or replaced, never deleted. A failure part-way through rolls everything back."""
    data, library = settings.data_dir, settings.library_dir
    data.mkdir(parents=True, exist_ok=True)
    snap = source.removeprefix("restic:") or "latest"
    with _exclusive(
        data / "lectern.lock",
        BackupError(
            "Lectern is running on this data directory. Stop it first "
            "(lectern-compose stop lectern), then run the restore again."
        ),
    ):
        scratch_parent = settings.backup_dir if os.access(settings.backup_dir, os.W_OK) else data
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=scratch_parent) as tmp:
            tmp_path = Path(tmp)
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
            stage = _find_stage(tmp_path)
            if not _regular(stage / "manifest.json"):
                raise BackupError("this does not look like a Lectern backup (no manifest.json)")
            manifest = json.loads((stage / "manifest.json").read_text())
            if manifest.get("format") != MANIFEST_FORMAT:
                raise BackupError(f"unknown backup format {manifest.get('format')!r}")
            db_copy = stage / "db" / "lectern.db"
            if db_copy.is_symlink() or not db_copy.is_file():
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

            # 1. Stage everything next to its destination. Nothing existing changes yet, so a
            #    full disk or a read error here leaves the server exactly as it was.
            plan: list[tuple[Path, Path, Path]] = []  # (staged, destination, aside path)
            staged_all: list[Path] = []
            created: list[Path] = []  # folders we made, removed again if the restore fails
            try:
                plan.append(
                    (
                        _stage_copy(db_copy, data / "lectern.db", tag, created),
                        data / "lectern.db",
                        Path("lectern.db"),
                    )
                )
                staged_all.append(plan[-1][0])
                if _regular(stage / "secrets" / "secret_key"):
                    s = _stage_copy(
                        stage / "secrets" / "secret_key", data / "secret_key", tag, created
                    )
                    staged_all.append(s)
                    plan.append((s, data / "secret_key", Path("secret_key")))
                for sub in DATA_SUBDIRS:
                    src = stage / "data" / sub
                    if src.is_dir() and not src.is_symlink():
                        s = _stage_copy(src, data / sub, tag, created)
                        staged_all.append(s)
                        plan.append((s, data / sub, Path(sub)))
                library_files_n = 0
                if restore_library:
                    seen: set[Path] = set()
                    for src_root in sources:
                        for f in _walk(src_root):  # regular files only; links are never followed
                            rel = f.relative_to(src_root)
                            dest = library / rel
                            if dest in seen or not _inside(dest.parent, library):
                                continue
                            seen.add(dest)
                            if (
                                dest.is_file()
                                and not dest.is_symlink()
                                and filecmp.cmp(f, dest, shallow=False)
                            ):
                                continue  # identical; nothing to replace
                            s = _stage_copy(f, dest, tag, created)
                            staged_all.append(s)
                            plan.append((s, dest, Path("library") / rel))
                            library_files_n += 1
            except BaseException:
                for s in staged_all:
                    if s.is_dir():
                        shutil.rmtree(s, ignore_errors=True)
                    else:
                        s.unlink(missing_ok=True)
                _remove_created(created)
                raise

            # 2. Swap: move originals aside and staged files in, rolling back on any failure.
            swap = _Swap(aside)
            try:
                for name in ("lectern.db-wal", "lectern.db-shm"):
                    if (data / name).exists():
                        swap.remove(data / name, Path(name))
                for staged, dest, aside_rel in plan:
                    swap.put(staged, dest, aside_rel)
            except BaseException as exc:
                try:
                    swap.rollback()
                except Exception as rb:  # noqa: BLE001
                    raise BackupError(
                        f"restore failed ({exc}) and could not be fully undone ({rb}); the "
                        f"original files are in {aside}"
                    ) from exc
                finally:
                    for s, _d, _a in plan:
                        if s.is_dir():
                            shutil.rmtree(s, ignore_errors=True)
                        else:
                            s.unlink(missing_ok=True)
                _remove_created(created)
                if aside.is_dir() and not any(p.is_file() for p in aside.rglob("*")):
                    shutil.rmtree(aside, ignore_errors=True)
                raise BackupError(f"restore failed and was undone; nothing changed: {exc}") from exc
            _private(data / "lectern.db")
            if (data / "secret_key").is_file():
                _private(data / "secret_key")

            pw = restic_password_file(settings)
            if _regular(stage / "secrets" / "restic-password") and not pw.exists():
                shutil.copy2(stage / "secrets" / "restic-password", pw)
                _private(pw)
            config_copy = None
            if _regular(stage / "config" / "lectern.env"):
                config_copy = data / "restored-lectern.env"
                shutil.copy2(stage / "config" / "lectern.env", config_copy)
                _private(config_copy)
    moved = aside.is_dir()
    result = {
        "created_at": manifest.get("created_at"),
        "lectern_version": manifest.get("lectern_version"),
        "schema_revision": manifest.get("schema_revision"),
        "moved_aside": str(aside) if moved else None,
        "library_files": library_files_n,
        "config_copy": str(config_copy) if config_copy else None,
    }
    log_fn(
        f"restored the backup from {result['created_at']} (Lectern {result['lectern_version']}); "
        f"{library_files_n} library file(s) restored."
    )
    if moved:
        log_fn(f"the files it replaced are in {aside}")
    if config_copy:
        log_fn(f"the LECTERN_* settings it ran with are in {config_copy}; compare with your .env")
    return result
