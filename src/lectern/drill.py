"""The restore drill (issue #76, docs/backups.md "Restore drill"): proof that the latest backup
restores, without touching anything live.

Every ``LECTERN_RESTORE_DRILL_DAYS`` (0 = off, the default), in the hours right after a nightly
backup, the maintenance thread restores parts of the latest restic snapshot into a scratch
folder on the backup volume (``<target>/.drill``), checks them, reports on the Health page,
and deletes the folder. It reads the repository with ``--no-lock`` and writes nothing there;
it never takes ``<data>/lectern.lock`` the way a restore does, and never writes to the data
directory, the library, or the live database (only its status row, like the backup's).

Checks, in order; the first that fails names the drill's failure:

``repository``  ``restic check``, reading the next 1/N of the pack data each run
                (``LECTERN_RESTORE_DRILL_READ_DATA_PARTS``), so N drills read all of it;
``snapshot``    the latest snapshot's listing: one staged Lectern backup, no symlinks or other
                odd entries (the same untrusted-snapshot rules as ``lectern restore``);
``space``       the scratch volume has room for what is restored, plus a reserve;
``restore``     ``restic restore --verify`` of the manifest, database, secrets, config, and a
                sample of book.json/covers, sentence maps/cover backups, and library files;
``manifest``    format, secrets and config present, metadata count as recorded;
``database``    opens read-only, ``PRAGMA integrity_check`` is ok, the schema revision is the
                manifest's and known here, row counts are plausible against the snapshot;
``files``       sampled files have their recorded sizes; library files their recorded SHA-256.

A drill skips (not a failure) while a backup holds the target lock or a restore holds or left
the data directory; it runs at idle I/O and lowest CPU priority, with a time cap. The scratch
folder is removed on success and failure; one left by a killed run is removed by the next
tick. restic runs hold the target lock too, so one orphaned by a killed server keeps the next
drill (and backup) away from the scratch folder until it ends.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from sqlalchemy.orm import Session

from lectern import backups, health
from lectern.backups import MANIFEST_FORMAT, STAGE_NAME, STAGING_DIR, BackupError
from lectern.config import Settings
from lectern.models import utcnow

log = logging.getLogger("lectern.drill")

STATUS_KEY = "restore_drill"
CHECK_ID = "backups.restore_drill"
SCRATCH_DIR = ".drill"  # on the backup target, next to (never inside) the restic repository
LIBRARY_SAMPLE_BYTES = 512 * 2**20  # library files are sampled, never restored wholesale
AFTER_BACKUP = timedelta(hours=6)  # the window after a backup in which a drill may start
RETRY_AFTER = timedelta(minutes=30)  # after a skipped drill
SLACK = timedelta(hours=12)  # so a weekly drill lands on the same night every week
TITLES = {
    "repository": "Repository check",
    "snapshot": "Latest snapshot",
    "space": "Free space",
    "restore": "Restore into scratch space",
    "manifest": "Manifest, secrets, and config",
    "database": "Database",
    "files": "Sampled files",
}
_TS = "%Y-%m-%dT%H:%M:%SZ"


class DrillSkipped(Exception):
    """A backup or restore is using the target or the data directory: not a failure."""


class DrillFailed(Exception):
    def __init__(self, check: str, message: str) -> None:
        super().__init__(message)
        self.check = check


# --- status -------------------------------------------------------------------------------------


def load_status(db: Session) -> dict:
    return backups._get(db, STATUS_KEY)


def record_status(db: Session, **fields) -> dict:
    status = {**load_status(db), **fields}
    backups._put(db, STATUS_KEY, status)
    return status


def _iso(dt: datetime) -> str:
    return dt.strftime(_TS)


def _parse(value: object) -> datetime | None:
    return health._parse(value)


# --- locks and scratch space --------------------------------------------------------------------


@contextlib.contextmanager
def _locks(settings: Settings) -> Iterator[int]:
    """The backup target's lock (the one every backup takes), exclusively; and a shared hold
    on ``<data>/lectern.lock`` when it exists, so a restore cannot start mid-drill. Neither
    waits: a busy lock is a skip. Yields the target lock's descriptor."""
    fd = os.open(settings.backup_dir / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    data_fd = None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DrillSkipped("a backup (or another drill) is running") from None
        with contextlib.suppress(FileNotFoundError):
            data_fd = os.open(settings.data_dir / "lectern.lock", os.O_RDONLY)  # never created
        if data_fd is not None:
            try:
                fcntl.flock(data_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DrillSkipped("a restore is running") from None
        yield fd
    finally:
        if data_fd is not None:
            os.close(data_fd)
        os.close(fd)


def _remove(path: Path) -> bool:
    """Delete ``path`` (a scratch folder, possibly with read-only folders restored into it)
    without following any symlink. True when it is gone."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        with contextlib.suppress(OSError):
            path.chmod(0o700)
        for here, dirs, _files in os.walk(path):
            for d in dirs:
                p = os.path.join(here, d)
                if not os.path.islink(p):
                    with contextlib.suppress(OSError):
                        os.chmod(p, 0o700)
        shutil.rmtree(path, ignore_errors=True)
    return not os.path.lexists(path)


def scratch_dir(settings: Settings) -> Path:
    return settings.backup_dir / SCRATCH_DIR


def sweep(settings: Settings) -> bool:
    """Remove a scratch folder left by a killed drill (it holds a plaintext database and the
    secrets). True if one was removed. Does nothing while a backup or drill holds the target."""
    scratch = scratch_dir(settings)
    if not os.path.lexists(scratch) or _RUN_LOCK.locked():
        return False
    try:
        with _locks(settings):
            removed = _remove(scratch)
    except (DrillSkipped, OSError):
        return False
    if removed:
        log.info("removed scratch space left by an interrupted restore drill")
    return removed


# --- restic -------------------------------------------------------------------------------------


def _low_priority() -> list[str]:
    """Idle I/O class and the lowest CPU priority for restic: the Pi keeps serving."""
    prefix = []
    if ionice := shutil.which("ionice"):
        prefix += [ionice, "-t", "-c", "3"]
    if nice := shutil.which("nice"):
        prefix += [nice, "-n", "19"]
    return prefix


class _Restic:
    """restic against the backup repository, read-only (``--no-lock``, no cache), with the
    drill's deadline, its temporary files in the scratch folder, and the target lock passed
    down so an orphaned restic still holds it."""

    def __init__(self, settings: Settings, scratch: Path, lock_fd: int, deadline: float) -> None:
        self.settings, self.scratch, self.lock_fd, self.deadline = (
            settings,
            scratch,
            lock_fd,
            deadline,
        )

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        exe = shutil.which(self.settings.restic_path)
        if exe is None:
            raise BackupError("restic is not installed on the server (it ships in the image)")
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise BackupError("the drill ran out of time (LECTERN_RESTORE_DRILL_TIMEOUT_MINUTES)")
        tmp = self.scratch / "tmp"
        tmp.mkdir(mode=0o700, exist_ok=True)
        env = {**backups._restic_env(self.settings, create=False), "TMPDIR": str(tmp)}
        try:
            proc = subprocess.run(  # noqa: S603 - fixed executable, arguments built here
                [*_low_priority(), exe, "--no-cache", "--no-lock", *args],
                env=env,
                capture_output=True,
                text=True,
                timeout=left,
                check=False,
                pass_fds=(self.lock_fd,),
            )
        except subprocess.TimeoutExpired:
            raise BackupError(
                f"restic {args[0]} ran out of time (LECTERN_RESTORE_DRILL_TIMEOUT_MINUTES)"
            ) from None
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
            raise BackupError(f"restic {args[0]} failed: {' '.join(tail)[:400]}")
        return proc


def _snapshot_time(value: str) -> str | None:
    """restic's snapshot time (nanoseconds, the backing-up host's offset) in UTC, or None."""
    try:
        when = datetime.fromisoformat(re.sub(r"(\.\d+)", "", value.replace("Z", "+00:00")))
    except ValueError:
        return None
    return _iso(when.astimezone(UTC)) if when.tzinfo else None


def _pattern(path: str) -> str:
    """A snapshot path as a restic ``--include`` pattern matching only itself."""
    return re.sub(r"([*?\[\]\\])", r"\\\1", path)


# --- the drill ----------------------------------------------------------------------------------


class _Drill:
    def __init__(self, settings: Settings, restic: _Restic, scratch: Path, part: int | None):
        self.settings, self.restic, self.scratch, self.part = settings, restic, scratch, part
        self.out = scratch / "restore"
        self.checks: list[dict] = []
        self.info: dict = {}
        self.rng = random.SystemRandom()

    def step(self, check: str, fn) -> None:
        try:
            note = fn()
        except DrillFailed:
            raise
        except (BackupError, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            raise DrillFailed(check, str(exc) or type(exc).__name__) from exc
        self.checks.append({"id": check, "title": TITLES[check], "ok": True, "note": note or ""})

    # repository
    def check_repository(self) -> str:
        n = self.settings.restore_drill_read_data_parts
        args = ["check"]
        if n and self.part:
            args.append(f"--read-data-subset={self.part}/{n}")
        self.restic(*args)
        self.info["read_data"] = f"{self.part}/{n}" if n and self.part else None
        return f"pack data {self.part}/{n} read" if n and self.part else "structure only"

    # snapshot
    def list_snapshot(self) -> str:
        proc = self.restic("ls", "--json", "--host", "lectern", "--tag", "lectern", "latest")
        lines = backups._json_lines(proc.stdout)
        snap = next((m for m in lines if m.get("struct_type") == "snapshot"), None)
        if not snap or not snap.get("id"):
            raise BackupError("the repository has no Lectern snapshot")
        files: dict[str, int] = {}
        odd = []
        for node in lines:
            if node.get("struct_type") != "node":
                continue
            path = str(node.get("path") or "")
            kind = node.get("type")
            if not path.startswith("/") or ".." in PurePosixPath(path).parts:
                odd.append(path or "(no path)")
            elif kind == "file":
                files[path] = int(node.get("size") or 0)
            elif kind != "dir":
                odd.append(path)
        if odd:
            raise BackupError(
                f"the snapshot holds {len(odd)} symlink(s) or other entries Lectern never backs "
                f"up, e.g. {odd[0]}"
            )
        stages = [
            PurePosixPath(p).parent
            for p in files
            if PurePosixPath(p).name == "manifest.json"
            and PurePosixPath(p).parent.name == STAGE_NAME
            and PurePosixPath(p).parent.parent.name == STAGING_DIR
        ]
        if len(stages) != 1:
            raise BackupError("the snapshot does not look like a Lectern backup (no manifest.json)")
        self.files, self.stage = files, stages[0]
        self.info["snapshot_id"] = str(snap.get("short_id") or snap["id"][:8])
        self.info["snapshot_time"] = _snapshot_time(str(snap.get("time") or ""))
        return f"{self.info['snapshot_id']}, {len(files)} files"

    def under(self, *parts: str) -> list[str]:
        root = str(self.stage.joinpath(*parts)) + "/"
        return sorted(p for p in self.files if p.startswith(root))

    def sample(self, paths: list[str]) -> list[str]:
        n = self.settings.restore_drill_sample
        return sorted(self.rng.sample(paths, min(n, len(paths))))

    def restored(self, path: str) -> Path:
        return self.out / path.lstrip("/")

    def restore(self, paths: list[str]) -> None:
        """Restore ``paths`` (files in the snapshot) into the scratch folder, after checking
        there is room for them, and refuse anything restic made that is not a regular file."""
        need = sum(self.files[p] for p in paths)
        free = shutil.disk_usage(self.scratch).free
        if need + backups.RESTORE_RESERVE_BYTES > free:
            raise DrillFailed(
                "space",
                f"the backup volume needs {backups._mib(need)} for the drill plus "
                f"{backups._mib(backups.RESTORE_RESERVE_BYTES)} to spare, but has "
                f"{backups._mib(free)} free",
            )
        args = ["restore", self.info["snapshot_id"], "--target", str(self.out), "--verify"]
        for p in paths:
            args += ["--include", _pattern(p)]
        try:
            self.restic(*args)
        except BackupError as exc:
            raise DrillFailed("restore", str(exc)) from exc
        links = backups._links_in(self.out)
        if links:
            raise DrillFailed("restore", f"the restore made {len(links)} symlink(s)")
        for p in paths:
            f = self.restored(p)
            if not backups._regular(f):
                raise DrillFailed("restore", f"{p} was not restored")
            if f.stat().st_size != self.files[p]:
                raise DrillFailed("files", f"{p} has {f.stat().st_size} bytes, not {self.files[p]}")

    # the staged backup, with samples of its bulky parts
    def restore_stage(self) -> str:
        essentials = [
            str(self.stage / "manifest.json"),
            *self.under("db"),
            *self.under("secrets"),
            *self.under("config"),
        ]
        self.meta = self.sample(self.under("library-meta"))
        self.data = self.sample(self.under("data"))
        paths = essentials + self.meta + self.data
        self.restore(paths)  # checks the free space first
        return f"{len(paths)} files restored and verified"

    def check_manifest(self) -> str:
        stage = self.restored(str(self.stage))
        manifest = json.loads((stage / "manifest.json").read_text())
        if not isinstance(manifest, dict) or manifest.get("format") != MANIFEST_FORMAT:
            raise BackupError(f"unknown backup format {manifest.get('format')!r}")
        self.manifest = manifest
        for name in ("lectern.env", "settings.json"):
            if not backups._regular(stage / "config" / name):
                raise BackupError(f"the backup has no config/{name}")
        json.loads((stage / "config" / "settings.json").read_text())
        kept = manifest.get("secrets")
        if not isinstance(kept, list):
            raise BackupError("the manifest does not list the secrets")
        for name in kept:
            if name not in ("secret_key", "restic-password"):
                raise BackupError(f"the manifest lists an unknown secret {name!r}")
            if not backups._regular(stage / "secrets" / name):
                raise BackupError(f"the secret {name} is missing from the backup")
        meta = self.under("library-meta")
        if manifest.get("library_meta_files") != len(meta):
            raise BackupError(
                f"the manifest records {manifest.get('library_meta_files')} book.json and cover "
                f"files, but the snapshot has {len(meta)}"
            )
        self.library_root = None
        if manifest.get("include_library"):
            root = PurePosixPath(str(manifest.get("library_root") or ""))
            if (
                not root.is_absolute()
                or ".." in root.parts
                or len(root.parts) < 2
                or self.stage.is_relative_to(root)
            ):
                raise BackupError("the manifest names an invalid library path")
            self.library_root = root
        return f"{len(kept)} secret(s), {len(meta)} metadata files"

    def check_database(self) -> str:
        db_file = self.restored(str(self.stage / "db" / "lectern.db"))
        if not backups._regular(db_file):
            raise BackupError("the backup has no database")
        con = sqlite3.connect(f"file:{db_file}?mode=ro&immutable=1", uri=True)
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()[0]
            if ok != "ok":
                raise BackupError(f"the database is damaged: {ok}")
            revision = con.execute("SELECT version_num FROM alembic_version").fetchone()
            revision = revision[0] if revision else None
            if revision != self.manifest.get("schema_revision"):
                raise BackupError(
                    f"the database is at schema revision {revision!r}, the manifest says "
                    f"{self.manifest.get('schema_revision')!r}"
                )
            backups._check_revision(revision)
            users = con.execute("SELECT count(*) FROM users").fetchone()[0]
            works = con.execute("SELECT count(*) FROM works").fetchone()[0]
            self.library_rows = []
            if self.library_root is not None:
                self.library_rows = con.execute(
                    "SELECT w.dir_path, f.rel_path, f.size, f.sha256 FROM book_files f "
                    "JOIN works w ON w.id = f.work_id "
                    "UNION ALL SELECT w.dir_path, t.rel_path, t.size, t.sha256 FROM audio_tracks t "
                    "JOIN works w ON w.id = t.work_id"
                ).fetchall()
        finally:
            con.close()
        if users < 1:
            raise BackupError("the database has no users")
        books = sum(1 for p in self.under("library-meta") if p.endswith("/book.json"))
        if books * 10 < works * 9:
            raise BackupError(
                f"the database has {works} books, but the backup has book.json for only {books}"
            )
        return f"integrity ok, schema {revision}, {users} user(s), {works} book(s)"

    def check_files(self) -> str:
        for p in self.meta:
            if p.endswith("/book.json") and not isinstance(
                json.loads(self.restored(p).read_text()), dict
            ):
                raise BackupError(f"{p} is not a book.json")
        checked = len(self.meta) + len(self.data)
        if self.library_root is None or not self.library_rows:
            return f"{checked} files"
        rows = list(self.library_rows)
        self.rng.shuffle(rows)
        picked, budget = [], LIBRARY_SAMPLE_BYTES
        for dir_path, rel_path, size, sha in rows:
            if len(picked) >= self.settings.restore_drill_sample:
                break
            if size > budget:
                continue  # large files are sampled only while the budget allows
            path = str(self.library_root / dir_path / rel_path)
            if ".." in PurePosixPath(path).parts:
                raise BackupError(f"the database names an invalid library path {path}")
            if path not in self.files:
                raise BackupError(f"{path} is in the database but missing from the backup")
            if self.files[path] != size:
                raise BackupError(
                    f"{path} has {self.files[path]} bytes in the backup, the database says {size}"
                )
            picked.append((path, sha))
            budget -= size
        if picked:
            try:
                self.restore([p for p, _ in picked])
            except DrillFailed as exc:
                if exc.check == "space":
                    raise
                raise DrillFailed("files", str(exc)) from exc
        for path, sha in picked:
            with self.restored(path).open("rb") as f:
                digest = hashlib.file_digest(f, "sha256").hexdigest()
            if digest != sha:
                raise BackupError(f"{path} does not match the SHA-256 the database recorded")
        return f"{checked + len(picked)} files, {len(picked)} library file(s) hashed"

    def run(self) -> None:
        self.step("repository", self.check_repository)
        self.step("snapshot", self.list_snapshot)
        self.step("restore", self.restore_stage)
        self.step("manifest", self.check_manifest)
        self.step("database", self.check_database)
        self.step("files", self.check_files)


def run_drill(settings: Settings, *, read_data_part: int | None = 1) -> dict:
    """One drill against the latest snapshot. Returns its result (``result`` is ``ok`` or
    ``failed``, with ``check`` and ``error`` for a failure); raises DrillSkipped while a
    backup or restore is using the target or the data directory."""
    target = settings.backup_dir
    started = time.monotonic()
    result: dict = {"result": "ok", "error": None, "check": None, "checks": []}
    if pending := backups.interrupted_restore(settings):
        raise DrillSkipped(pending)
    if backups.is_running():
        raise DrillSkipped("a backup is running")
    if not target.is_dir() or not os.access(target, os.W_OK | os.X_OK):
        return {
            **result,
            "result": "failed",
            "check": "repository",
            "error": f"the backup folder {target} is missing or not writable",
            "duration_s": 0.0,
        }
    if not (target / "restic" / "config").is_file():
        return {
            **result,
            "result": "failed",
            "check": "repository",
            "error": "there is no restic repository in the backup folder yet",
            "duration_s": 0.0,
        }
    deadline = started + settings.restore_drill_timeout_minutes * 60
    with _locks(settings) as lock_fd:
        scratch = scratch_dir(settings)
        _remove(scratch)  # left by a killed drill; we hold the target lock
        drill = None
        try:
            scratch.mkdir(mode=0o700)
            drill = _Drill(
                settings, _Restic(settings, scratch, lock_fd, deadline), scratch, read_data_part
            )
            drill.run()
        except DrillFailed as exc:
            result.update(result="failed", check=exc.check, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - every failure must reach the owner
            log.exception("unexpected restore drill failure")
            result.update(result="failed", check=None, error=str(exc) or type(exc).__name__)
        finally:
            cleaned = _remove(scratch)
        if drill is not None:
            result["checks"] = drill.checks
            result.update(drill.info)
        if result["check"]:
            result["checks"].append(
                {
                    "id": result["check"],
                    "title": TITLES[result["check"]],
                    "ok": False,
                    "note": result["error"],
                }
            )
        if not cleaned:
            log.error("the restore drill could not remove its scratch folder %s", scratch)
            if result["result"] == "ok":
                result.update(
                    result="failed",
                    error=f"could not remove the scratch folder {scratch}; delete it by hand",
                )
    result["duration_s"] = round(time.monotonic() - started, 1)
    return result


# --- schedule and reporting ---------------------------------------------------------------------

_RUN_LOCK = threading.Lock()


def is_running() -> bool:
    return _RUN_LOCK.locked()


def run_and_record(session_factory, settings: Settings, *, reason: str) -> dict:
    """Run a drill, store its outcome, and refresh Health at once (a failed drill alerts the
    owners there, in the app and through push alerts). A skip is stored as such and retried
    after :data:`RETRY_AFTER`; it alerts nobody."""
    if not _RUN_LOCK.acquire(blocking=False):
        with session_factory() as db:
            return {**load_status(db), "skipped": True}
    try:
        with session_factory() as db:
            status = record_status(db, last_attempt_at=_iso(utcnow()), last_reason=reason)
            db.commit()
        parts = settings.restore_drill_read_data_parts
        part = (int(status.get("read_data_next") or 1) - 1) % parts + 1 if parts else None
        try:
            result = run_drill(settings, read_data_part=part)
        except DrillSkipped as exc:
            log.info("restore drill skipped: %s", exc)
            with session_factory() as db:
                status = record_status(
                    db, last_skipped_at=_iso(utcnow()), last_skipped_reason=str(exc)
                )
                db.commit()
            return {**status, "skipped": True}
        now = _iso(utcnow())
        fields: dict = {
            "last_result": result["result"],
            "last_duration_s": result["duration_s"],
            "last_checks": result["checks"],
            "last_snapshot_id": result.get("snapshot_id"),
            "last_snapshot_time": result.get("snapshot_time"),
            "last_read_data": result.get("read_data"),
            "last_failed_check": result["check"],
            "last_error": result["error"],
            "last_skipped_reason": None,
        }
        if result["result"] == "ok":
            fields["last_success_at"] = now
            log.info("restore drill passed in %.1f s", result["duration_s"])
        else:
            fields["last_error_at"] = now
            log.error("restore drill failed: %s", result["error"])
        if part:
            fields["read_data_next"] = part % parts + 1
        with session_factory() as db:
            status = record_status(db, **fields)
            db.commit()
        try:
            with session_factory() as db:
                health.refresh(db, settings)
        except Exception:  # noqa: BLE001 - the next poll reports it anyway
            log.exception("health refresh after the restore drill failed")
        return status
    finally:
        _RUN_LOCK.release()


def due(settings: Settings, backup_cfg, backup_status: dict, status: dict, now: datetime) -> bool:
    """True in the hours right after a backup (never during one, and never while the nightly
    backup is still due) once ``LECTERN_RESTORE_DRILL_DAYS`` have passed since the last drill
    that ran to the end. A skipped drill is retried after :data:`RETRY_AFTER`."""
    days = settings.restore_drill_days
    if days <= 0 or not backup_cfg.enabled or not backup_status.get("last_success_at"):
        return False
    if backups.is_running() or backups.due(backup_cfg, backup_status, now):
        return False
    backed_up = _parse(backup_status.get("last_attempt_at"))
    if backed_up is None or not timedelta(0) <= now - backed_up <= AFTER_BACKUP:
        return False
    attempt = _parse(status.get("last_attempt_at"))
    if attempt is not None and now - attempt < RETRY_AFTER:
        return False
    done = [
        d for d in map(_parse, (status.get("last_success_at"), status.get("last_error_at"))) if d
    ]
    return not done or now - max(done) >= timedelta(days=days) - SLACK


def tick(session_factory, settings: Settings, now: datetime | None = None) -> bool:
    """The maintenance thread's check, right after the backup's: clear what a killed drill
    left, then run a drill if one is due. True if one ran."""
    sweep(settings)
    now = now or utcnow()
    with session_factory() as db:
        cfg, bstatus, status = (
            backups.load_config(db),
            backups.load_status(db),
            load_status(db),
        )
    if not due(settings, cfg, bstatus, status, now):
        return False
    run_and_record(session_factory, settings, reason="scheduled")
    return True


# --- Health -------------------------------------------------------------------------------------


def health_check(db: Session, settings: Settings, now: datetime) -> health.Check:
    """The drill's line on the Health page, from what the last drill recorded."""
    st = load_status(db)
    days = settings.restore_drill_days
    failed = st.get("last_result") == "failed"
    last_ok = _parse(st.get("last_success_at"))
    detail: dict = {
        "every_days": days,
        "running": is_running(),
        "last_result": st.get("last_result"),
        "last_success_at": st.get("last_success_at"),
        "last_error": st.get("last_error") if failed else None,
        "last_error_at": st.get("last_error_at") if failed else None,
        "duration_s": st.get("last_duration_s"),
        "snapshot_id": st.get("last_snapshot_id"),
        "snapshot_time": st.get("last_snapshot_time"),
        "read_data": st.get("last_read_data"),
        "checks": st.get("last_checks") or [],
    }
    if st.get("last_skipped_reason"):
        detail["reason"] = f"The last drill was skipped: {st['last_skipped_reason']}"
    if days <= 0:
        level, summary = health.INFO, "Off (LECTERN_RESTORE_DRILL_DAYS in .env)"
    elif not backups.load_config(db).enabled:
        level, summary = health.INFO, "Waiting for nightly backups to be turned on"
    elif failed:
        what = TITLES.get(st.get("last_failed_check") or "", "an unexpected error")
        level, summary = health.CRITICAL, f"The last drill failed: {what.lower()}"
    elif last_ok:
        age = now - last_ok
        if age > timedelta(days=2 * days + 1):
            level, summary = health.WARNING, f"Overdue: last passed {health._ago(age)} ago"
        else:
            level, summary = health.OK, f"Passed {health._ago(age)} ago"
            snap = _parse(st.get("last_snapshot_time"))
            if snap:
                summary += f" (backup {health._ago(last_ok - snap)} old)"
    else:
        level, summary = health.INFO, "Waiting for the first drill, after a nightly backup"
    return health.Check(CHECK_ID, "backups", "Restore drill", level, summary, detail)


def register() -> None:
    health.register_check("restore_drill", health_check)


register()  # also on every create_app(): whoever runs a drill reports it on Health
