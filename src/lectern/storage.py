"""The storage dashboard (issue #74, docs/storage.md): where the disk goes, and safe cleanup.

Sizing
    :func:`scan` walks the data directory, the backup target, and the library once, never
    following a symlink (each one met is counted in ``skipped_links``, not sized), and sorts
    every regular file into a category (:data:`CATEGORIES`) and a part of it. Sizes are file
    sizes (``st_size``); a file reachable twice through hard links counts once. The result,
    with the largest works and a short free-space history, is cached in ``instance_settings``
    (:data:`SCAN_KEY`): the page reads the cache, and a scan runs in the background, every
    :data:`SCAN_INTERVAL` (the maintenance thread), when the owner asks, and
    when a cleanup finishes while the page is open (the page asks for it).
    Free space per volume (:func:`volumes`) is read live: it is one ``statvfs`` per volume.

Cleanup
    Only files Lectern can make again are offered (:data:`ACTIONS`): the Trash (everything or
    what is older than N days; the same purge as the 30-day sweep), data-saver copies of
    audiobooks, generated covers, read-aloud EPUBs, leftovers of killed runs (upload parts,
    staged results, scratch files, finished jobs' artifacts, the backup target's
    ``.staging``), and ``pre-restore-<time>/`` folders, one at a time. Originals, narrated
    audiobooks, OCR copies (the scan's text is built from them), converted editions (the
    book's page removes those), the database, and the restic repository are never offered.

    :func:`plan` lists exactly what an action would remove now and how much it frees (the
    preview). Confirming queues one ``storage-cleanup`` job (server-only, on the existing
    queue) whose payload holds the keys of the previewed items; the runner removes each one
    only if it is still eligible when its turn comes (a book restored meanwhile is skipped),
    records the keys it finished, and so is idempotent and resumable after a restart. Each
    removal takes :data:`lectern.fsops.LIBRARY_LOCK` where the library is touched, never
    follows a symlink, and waits (the job goes back to the queue) while a backup runs or a
    restore was interrupted (:func:`blocked`). The owner who asked and the bytes freed are
    in the audit log (``storage.cleanup`` when asked, ``storage.cleanup.done`` when done).
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import shutil
import stat
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from lectern import queue
from lectern.config import Settings
from lectern.jobs import CLEANUP
from lectern.models import (
    JOB_ACTIVE_STATUSES,
    AudioTrack,
    GeneratedAudiobook,
    InstanceSetting,
    Job,
    JobStatus,
    TrashedFile,
    UploadSession,
    UploadStatus,
    Work,
    utcnow,
)

log = logging.getLogger("lectern.storage")

SCAN_KEY = "storage_scan"
SCAN_INTERVAL = timedelta(hours=6)
HISTORY_MAX = 120  # free-space samples kept for the trend (at most one an hour)
TOP_WORKS = 10
BACKUP_WAIT = timedelta(minutes=5)
TMP_MAX_AGE_S = 24 * 3600  # scratch files under <data>/tmp older than this are leftovers
PRE_RESTORE_RE = re.compile(r"^pre-restore-\d{8}T\d{6}Z$")

CATEGORIES = (
    "originals",
    "generated_audio",
    "ocr",
    "converted",
    "covers",
    "derived_text",
    "trash",
    "backups",
    "other",
)
ACTIONS = ("trash", "renditions", "covers", "read_aloud", "leftovers", "pre_restore")
# Actions that may be limited to one work (``work_id``).
PER_WORK = frozenset({"renditions", "covers", "read_aloud"})

# Top-level entries of the data directory: (category, part).
_DATA = {
    "align": ("derived_text", "sentence_maps"),
    "revisions": ("covers", "undo_backups"),
    "uploads": ("other", "upload_parts"),
    "logs": ("other", "logs"),
    "tmp": ("other", "scratch"),
    "jobs": ("other", "scratch"),
    "staged-covers": ("other", "scratch"),
    "ocr": ("derived_text", "ocr_scan_cache"),
    "models": ("other", "models"),
    "tts": ("other", "models"),
}


# --- walking ----------------------------------------------------------------------------------


def _files(
    root: Path, skip: set[Path], links: list[int]
) -> Iterator[tuple[tuple[str, ...], os.stat_result]]:
    """``(parts relative to root, lstat)`` of every regular file under ``root``. Symlinks are
    never followed or sized (``links[0]`` counts them); folders in ``skip`` are left out."""
    try:
        if root.is_symlink() or not root.is_dir():
            return
    except OSError:
        return
    for here_s, dirs, files in os.walk(root, followlinks=False):
        here = Path(here_s)
        keep = []
        for d in dirs:
            p = here / d
            if p.is_symlink():
                links[0] += 1
            elif p not in skip:
                keep.append(d)
        dirs[:] = keep
        rel = here.relative_to(root).parts
        for name in files:
            try:
                st = os.lstat(here / name)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                links[0] += 1
            elif stat.S_ISREG(st.st_mode):
                yield (*rel, name), st


def tree_size(path: Path) -> tuple[int, int]:
    """``(bytes, files)`` of the regular files at or under ``path``, links not followed."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0, 0
    if stat.S_ISREG(st.st_mode):
        return st.st_size, 1
    if not stat.S_ISDIR(st.st_mode):
        return 0, 0
    total = n = 0
    for _parts, fst in _files(path, set(), [0]):
        total, n = total + fst.st_size, n + 1
    return total, n


# --- volumes ----------------------------------------------------------------------------------


def _roots(settings: Settings) -> list[tuple[str, Path]]:
    return [
        ("data", settings.data_dir),
        ("library", settings.library_dir),
        ("backups", settings.backup_dir),
    ]


def volumes(settings: Settings) -> list[dict]:
    """Every volume Lectern stores on, live: the roots on one device share an entry, named
    after its first root (``data``, ``library``, ``backups``). ``low``: free space below
    ``LECTERN_STORAGE_WARN_FREE_PERCENT`` (to be shared with the health page, #75)."""
    out: dict[int, dict] = {}
    for role, path in _roots(settings):
        try:
            dev = path.stat().st_dev
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        if dev not in out:
            free_pct = 100.0 * usage.free / usage.total if usage.total else 0.0
            out[dev] = {
                "id": role,
                "roles": [],
                "paths": [],
                "total": usage.total,
                "used": usage.total - usage.free,
                "free": usage.free,
                "free_percent": round(free_pct, 1),
                "low": free_pct < settings.storage_warn_free_percent,
            }
        out[dev]["roles"].append(role)
        out[dev]["paths"].append(str(path))
    return list(out.values())


def _devices(settings: Settings) -> dict[int, str]:
    """Device number -> volume id, for the roots that exist."""
    out: dict[int, str] = {}
    for role, path in _roots(settings):
        try:
            out.setdefault(path.stat().st_dev, role)
        except OSError:
            continue
    return out


# --- the scan ---------------------------------------------------------------------------------


class _Tally:
    def __init__(self, devices: dict[int, str]) -> None:
        self.devices = devices
        self.cats = {c: {"bytes": 0, "files": 0, "parts": {}, "volumes": {}} for c in CATEGORIES}
        self.works: dict[int, dict] = {}
        self.pre_restore: dict[str, int] = {}
        self.seen: set[tuple[int, int]] = set()

    def add(
        self, st: os.stat_result, cat: str, part: str, fallback: str, work_id: int | None = None
    ) -> None:
        key = (st.st_dev, st.st_ino)
        if key in self.seen:
            return
        self.seen.add(key)
        n = st.st_size
        c = self.cats[cat]
        c["bytes"] += n
        c["files"] += 1
        c["parts"][part] = c["parts"].get(part, 0) + n
        vol = self.devices.get(st.st_dev, fallback)
        c["volumes"][vol] = c["volumes"].get(vol, 0) + n
        if work_id is not None:
            w = self.works.setdefault(work_id, {"bytes": 0, "categories": {}})
            w["bytes"] += n
            w["categories"][cat] = w["categories"].get(cat, 0) + n


def _work_part(sub: tuple[str, ...], narrated: set[str]) -> tuple[str, str]:
    """Category and part of a file in a work's folder (``sub``: its path in the folder)."""
    from lectern.pdf2epub.build import CONVERTED_NAME
    from lectern.pdf2epub.jobs import PREVIEW_DIR

    name = sub[-1]
    if sub[0] == "derived" and len(sub) > 1:
        if sub[1] == "audio" and len(sub) > 2:
            return "generated_audio", "data_saver"
        if sub[1] == PREVIEW_DIR or name.startswith("pdf2epub-"):
            return "converted", "conversion_files"
        if name.startswith("ocr-"):
            return "ocr", "ocr_copies"
        if name.startswith("cover-"):
            return "covers", "generated"
        if name.startswith("pdf-text-"):
            return "derived_text", "pdf_text"
        if name.startswith("read-aloud."):
            return "converted", "read_aloud"
        return "derived_text", "other_derived"
    rel = "/".join(sub)
    if rel == CONVERTED_NAME:
        return "converted", "pdf_editions"
    if rel in narrated:
        return "generated_audio", "narrated"
    return "originals", "books"


def _context(db: Session) -> dict:
    works = {
        w.dir_path: (w.id, w.title, w.author, w.trashed_at is not None)
        for w in db.scalars(select(Work))
    }
    generated = set(db.scalars(select(GeneratedAudiobook.work_id)))
    narrated: dict[int, set[str]] = {}
    if generated:
        for wid, rel in db.execute(
            select(AudioTrack.work_id, AudioTrack.rel_path).where(AudioTrack.work_id.in_(generated))
        ):
            narrated.setdefault(wid, set()).add(rel)
    return {"works": works, "narrated": narrated}


def scan(settings: Settings, context: dict) -> dict:
    """Walk everything once and total it (see the module docstring). Reads only."""
    from lectern.fsops import STAGING_DIR as IMPORTS_DIR
    from lectern.fsops import TRASH_DIR

    started = time.monotonic()
    devices = _devices(settings)
    tally = _Tally(devices)
    links = [0]
    roots = {p.absolute() for _r, p in _roots(settings)}
    works: dict[str, tuple] = context["works"]
    narrated: dict[int, set[str]] = context["narrated"]

    # The data directory.
    data = settings.data_dir.absolute()
    for parts, st in _files(data, roots - {data}, links):
        top = parts[0]
        if len(parts) == 1 and (top.startswith("lectern.db") or top == "lectern.lock"):
            cat, part = "other", "database"
        elif len(parts) == 1 and top.startswith("search.db"):
            cat, part = "derived_text", "search_index"
        elif PRE_RESTORE_RE.match(top) and len(parts) > 1:
            cat, part = "backups", "pre_restore"
            tally.pre_restore[top] = tally.pre_restore.get(top, 0) + st.st_size
        elif top in _DATA and len(parts) > 1:
            cat, part = _DATA[top]
        else:
            cat, part = "other", "misc"
        tally.add(st, cat, part, "data")

    # The backup target.
    target = settings.backup_dir.absolute()
    for parts, st in _files(target, roots - {target}, links):
        top = parts[0]
        part = {"restic": "restic", ".staging": "staging"}.get(top, "other")
        tally.add(st, "backups", part, "backups")

    # The library.
    library = settings.library_dir.absolute()
    inbox = settings.inbox_path.absolute()
    inbox_parts = inbox.relative_to(library).parts if inbox.is_relative_to(library) else None
    for parts, st in _files(library, roots - {library}, links):
        top = parts[0]
        if top == TRASH_DIR:
            tally.add(st, "trash", parts[1] if len(parts) > 2 else "other", "library")
            continue
        if inbox_parts and parts[: len(inbox_parts)] == inbox_parts:
            tally.add(st, "other", "inbox", "library")
            continue
        if top == queue.STAGING_DIR:
            tally.add(st, "other", "staging", "library")
            continue
        if top == IMPORTS_DIR:
            tally.add(st, "other", "imports_waiting", "library")
            continue
        found = None
        for k in (2, 1, 3):
            if len(parts) > k and (w := works.get("/".join(parts[:k]))) is not None:
                found = (k, w)
                break
        if found is None:
            tally.add(st, "originals", "unfiled", "library")
            continue
        k, (wid, *_rest) = found
        cat, part = _work_part(parts[k:], narrated.get(wid, set()))
        tally.add(st, cat, part, "library", wid)
    if inbox_parts is None:  # an inbox outside the library
        for _parts, st in _files(inbox, roots, links):
            tally.add(st, "other", "inbox", "library")

    by_id = {w[0]: w for w in works.values()}
    largest = sorted(tally.works.items(), key=lambda kv: kv[1]["bytes"], reverse=True)
    return {
        "scanned_at": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_s": round(time.monotonic() - started, 2),
        "categories": tally.cats,
        "total_bytes": sum(c["bytes"] for c in tally.cats.values()),
        "largest_works": [
            {
                "id": wid,
                "title": by_id[wid][1],
                "author": by_id[wid][2],
                "trashed": by_id[wid][3],
                "bytes": info["bytes"],
                "categories": info["categories"],
            }
            for wid, info in largest[:TOP_WORKS]
            if wid in by_id
        ],
        "pre_restore": [
            {"name": name, "bytes": n} for name, n in sorted(tally.pre_restore.items())
        ],
        "skipped_links": links[0],
    }


# --- the cache ----------------------------------------------------------------------------------


def load(db: Session) -> dict:
    row = db.get(InstanceSetting, SCAN_KEY)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def _save(db: Session, result: dict, vols: list[dict]) -> None:
    history = list(load(db).get("history") or [])
    sample = {
        "at": result["scanned_at"],
        "volumes": {v["id"]: {"free": v["free"], "total": v["total"]} for v in vols},
    }
    if history:
        try:
            last = datetime.strptime(history[-1]["at"], "%Y-%m-%dT%H:%M:%SZ")
            now = datetime.strptime(sample["at"], "%Y-%m-%dT%H:%M:%SZ")
            if now - last < timedelta(hours=1):
                history.pop()  # at most one sample an hour: a manual rescan replaces it
        except (KeyError, ValueError):
            history = []
    history = [*history, sample][-HISTORY_MAX:]
    value = {"result": result, "history": history}
    row = db.get(InstanceSetting, SCAN_KEY)
    if row is None:
        db.add(InstanceSetting(key=SCAN_KEY, value=value))
    else:
        row.value = value
    db.commit()


_SCAN_LOCK = threading.Lock()


def scanning() -> bool:
    return _SCAN_LOCK.locked()


def run_scan(session_factory: sessionmaker, settings: Settings) -> dict | None:
    """Scan now and store the result; ``None`` when a scan is already running."""
    if not _SCAN_LOCK.acquire(blocking=False):
        return None
    try:
        with session_factory() as db:
            context = _context(db)
        result = scan(settings, context)
        with session_factory() as db:
            _save(db, result, volumes(settings))
        log.info("storage scan: %d bytes in %.1f s", result["total_bytes"], result["duration_s"])
        return result
    finally:
        _SCAN_LOCK.release()


def start_scan(session_factory: sessionmaker, settings: Settings) -> bool:
    """Scan in a background thread. False when one is running already."""
    if scanning():
        return False

    def go() -> None:
        try:
            run_scan(session_factory, settings)
        except Exception:  # noqa: BLE001
            log.exception("storage scan failed")

    threading.Thread(target=go, name="lectern-storage-scan", daemon=True).start()
    return True


def tick(
    session_factory: sessionmaker,
    settings: Settings,
    now: datetime | None = None,
    *,
    background: bool = False,
) -> bool:
    """The maintenance thread's check: scan when the last scan is older than SCAN_INTERVAL.
    ``background`` starts the scan in its own thread, so the walk never delays the backup
    check that shares the maintenance thread."""
    now = now or utcnow()
    with session_factory() as db:
        at = (load(db).get("result") or {}).get("scanned_at")
    if at:
        last = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=now.tzinfo)
        if now - last < SCAN_INTERVAL:
            return False
    if background:
        return start_scan(session_factory, settings)
    return run_scan(session_factory, settings) is not None


# --- backups and restores block cleanup -----------------------------------------------------


def blocked(settings: Settings) -> str | None:
    """``backup_running`` while a backup holds the target (here or in another process),
    ``restore_interrupted`` while a restore's journal is unresolved; else None. (A restore
    itself cannot run while the server does: it needs the data directory's lock.)"""
    from lectern import backups

    if backups.is_running():
        return "backup_running"
    if backups.interrupted_restore(settings):
        return "restore_interrupted"
    lock = settings.backup_dir / ".lock"
    try:
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return "backup_running"
    finally:
        os.close(fd)
    return None


# --- planning (the preview) -----------------------------------------------------------------


def _item(key: str, label: str, size: tuple[int, int], **extra) -> dict:
    return {"key": key, "label": label, "bytes": size[0], "files": size[1], **extra}


def _plain_files(folder: Path, match: Callable[[str], bool]) -> list[Path]:
    """Regular files directly in ``folder`` whose names ``match``; nothing when ``folder`` is
    a link or missing, and never a link itself."""
    try:
        if folder.is_symlink() or not folder.is_dir():
            return []
        return [
            p
            for p in sorted(folder.iterdir())
            if match(p.name) and not p.is_symlink() and p.is_file()
        ]
    except OSError:
        return []


def _sum(paths: list[Path]) -> tuple[int, int]:
    total = 0
    for p in paths:
        try:
            total += p.lstat().st_size
        except OSError:
            continue
    return total, len(paths)


def _rendition_files(settings: Settings, work: Work) -> list[Path]:
    from lectern.jobs import EXT

    exts = tuple(f".{e}" for e in EXT.values())
    folder = settings.library_dir / work.dir_path / "derived" / "audio"
    return _plain_files(folder, lambda n: n.endswith(exts) or n.endswith((".sha256", ".tmp")))


def _cover_files(settings: Settings, work: Work) -> list[Path]:
    from lectern.covergen import CACHE_PREFIX

    folder = settings.library_dir / work.dir_path / "derived"
    return _plain_files(folder, lambda n: n.startswith(CACHE_PREFIX) and n.endswith(".jpg"))


def _read_aloud_files(settings: Settings, work: Work) -> list[Path]:
    from lectern.readaloud.cache import OUTPUT_NAME, SIDECAR_NAME

    folder = settings.library_dir / work.dir_path / "derived"
    # The sidecar first: without it the EPUB is never served as current.
    return sorted(
        _plain_files(folder, lambda n: n in (OUTPUT_NAME, SIDECAR_NAME)),
        key=lambda p: p.name != SIDECAR_NAME,
    )


_WORK_FILES = {
    "renditions": _rendition_files,
    "covers": _cover_files,
    "read_aloud": _read_aloud_files,
}


def _cutoff(params: dict) -> datetime | None:
    days = params.get("older_than_days")
    return utcnow() - timedelta(days=int(days)) if days else None


def _trash_items(db: Session, settings: Settings, cutoff: datetime | None) -> list[dict]:
    from lectern.fsops import TRASH_DIR, inside

    library = settings.library_dir
    items = []
    stmt = select(Work).where(Work.trashed_at.is_not(None))
    if cutoff is not None:
        stmt = stmt.where(Work.trashed_at < cutoff)
    trashed_works = set()
    for w in db.scalars(stmt.order_by(Work.trashed_at)):
        trashed_works.add(w.id)
        folder = library / w.dir_path
        size = tree_size(folder) if inside(library / TRASH_DIR, folder) else (0, 0)
        for tf in db.scalars(
            select(TrashedFile).where(
                TrashedFile.work_id == w.id, TrashedFile.restored_at.is_(None)
            )
        ):
            extra = tree_size(library / tf.trash_path)
            size = (size[0] + extra[0], size[1] + extra[1])
        items.append(_item(f"work:{w.id}", f"{w.title} — {w.author}", size, work_id=w.id))
    stmt = select(TrashedFile).where(TrashedFile.restored_at.is_(None))
    if cutoff is not None:
        stmt = stmt.where(TrashedFile.trashed_at < cutoff)
    for tf in db.scalars(stmt.order_by(TrashedFile.trashed_at)):
        if tf.work_id in trashed_works:
            continue  # goes with its book
        work = db.get(Work, tf.work_id)
        label = Path(tf.rel_path).name + (f" (from {work.title})" if work else "")
        items.append(
            _item(f"file:{tf.id}", label, tree_size(library / tf.trash_path), work_id=tf.work_id)
        )
    return items


def _leftover_items(db: Session, settings: Settings) -> list[dict]:
    from lectern import uploads

    items = []
    now = time.time()
    live = set(
        db.scalars(
            select(UploadSession.public_id).where(
                UploadSession.status == UploadStatus.ACTIVE, UploadSession.expires_at >= utcnow()
            )
        )
    )
    for p in _plain_files(uploads.uploads_dir(settings), lambda n: n.endswith(".part")):
        try:
            young = now - p.lstat().st_mtime < uploads.ORPHAN_GRACE_S
        except OSError:
            continue
        if p.stem not in live and not young:
            items.append(_item(f"upload:{p.name}", f"Upload part {p.name}", _sum([p])))
    staging = settings.library_dir / queue.STAGING_DIR
    for d in _entries(staging):
        if _staging_stale(db, d, now):
            items.append(_item(f"staging:{d.name}", f"Staged result {d.name}", tree_size(d)))
    for d in _entries(settings.data_dir / "tmp"):
        try:
            old = now - d.lstat().st_mtime > TMP_MAX_AGE_S
        except OSError:
            continue
        if old:
            items.append(_item(f"tmp:{d.name}", f"Scratch {d.name}", tree_size(d)))
    for d in _entries(settings.data_dir / "jobs"):
        if d.name.isdigit() and _artifacts_stale(db, int(d.name)):
            items.append(
                _item(f"jobs:{d.name}", f"Artifacts of finished job {d.name}", tree_size(d))
            )
    from lectern.backups import STAGING_DIR as BACKUP_STAGING

    left = settings.backup_dir / BACKUP_STAGING
    if left.is_dir() and not left.is_symlink():
        items.append(
            _item("backup-staging", "Backup staging left by a killed run", tree_size(left))
        )
    return items


def _entries(folder: Path) -> list[Path]:
    try:
        if folder.is_symlink() or not folder.is_dir():
            return []
        return sorted(folder.iterdir())
    except OSError:
        return []


def _staging_stale(db: Session, d: Path, now: float) -> bool:
    """As :func:`lectern.queue.sweep_staging`: its job finished, or it is older than 6 h."""
    job_id = d.name.split("-", 1)[0]
    job = db.get(Job, int(job_id)) if job_id.isdigit() else None
    try:
        young = now - d.lstat().st_mtime < queue.STAGING_MAX_AGE.total_seconds()
    except OSError:
        return False
    return not (young and job is not None and job.status in JOB_ACTIVE_STATUSES)


def _artifacts_stale(db: Session, job_id: int) -> bool:
    """As :func:`lectern.queue.sweep_artifacts`: the job finished or is gone, and its kind
    does not keep artifacts for a later retry."""
    job = db.get(Job, job_id)
    if job is None:
        return True
    found = queue.handler(job.kind)
    if found is not None and found.keeps_artifacts:
        return False
    return job.status not in JOB_ACTIVE_STATUSES


def _pre_restore_items(settings: Settings, name: str | None) -> list[dict]:
    out = []
    for d in _entries(settings.data_dir):
        if not PRE_RESTORE_RE.match(d.name) or d.is_symlink() or not d.is_dir():
            continue
        if name is not None and d.name != name:
            continue
        out.append(_item(f"pre-restore:{d.name}", d.name, tree_size(d), created_at=_stamp(d.name)))
    return out


def _stamp(name: str) -> str | None:
    try:
        at = datetime.strptime(name.removeprefix("pre-restore-"), "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def plan(
    db: Session,
    settings: Settings,
    action: str,
    params: dict,
    busy: Callable[[int], bool] = lambda _id: False,
) -> dict:
    """What ``action`` would remove right now (the preview): items with their sizes, the
    totals, and whether a backup or restore would make the job wait."""
    if action not in ACTIONS:
        raise ValueError(action)
    skipped: list[dict] = []
    if action == "trash":
        items = _trash_items(db, settings, _cutoff(params))
    elif action == "leftovers":
        items = _leftover_items(db, settings)
    elif action == "pre_restore":
        items = _pre_restore_items(settings, params.get("name"))
    else:
        stmt = select(Work).where(Work.trashed_at.is_(None))
        if params.get("work_id") is not None:
            stmt = stmt.where(Work.id == params["work_id"])
        if action == "renditions":
            stmt = stmt.where(Work.id.in_(select(AudioTrack.work_id)))
        items = []
        for w in db.scalars(stmt.order_by(Work.sort_author, Work.sort_title)):
            if action == "read_aloud" and busy(w.id):
                skipped.append({"work_id": w.id, "label": w.title, "reason": "generating"})
                continue
            found = _WORK_FILES[action](settings, w)
            if found:
                items.append(_item(f"work:{w.id}", f"{w.title} — {w.author}", _sum(found),
                                   work_id=w.id))  # fmt: skip
    return {
        "action": action,
        "params": params,
        "items": items,
        "skipped": skipped,
        "total_bytes": sum(i["bytes"] for i in items),
        "total_files": sum(i["files"] for i in items),
        "blocked": blocked(settings),
    }


# --- the job ----------------------------------------------------------------------------------


def active_job(db: Session) -> Job | None:
    return db.scalar(
        select(Job).where(Job.kind == CLEANUP, Job.status.in_(JOB_ACTIVE_STATUSES)).limit(1)
    )


def enqueue(db: Session, preview: dict, keys: list[str], user_id: int) -> Job:
    """Queue the removal of the previewed items named in ``keys``. Caller commits."""
    chosen = [i for i in preview["items"] if i["key"] in set(keys)]
    params = dict(preview["params"])
    if preview["action"] == "trash" and params.get("older_than_days"):
        # The cutoff is fixed when the owner confirms, not when the job runs.
        params["cutoff"] = _cutoff(params).strftime("%Y-%m-%dT%H:%M:%SZ")
    job = Job(
        kind=CLEANUP,
        status=JobStatus.QUEUED,
        priority=5,
        max_attempts=3,
        payload={
            "action": preview["action"],
            "params": params,
            "keys": [i["key"] for i in chosen],
            "expected_bytes": sum(i["bytes"] for i in chosen),
        },
        result={"done": [], "freed_bytes": 0, "errors": [], "skipped": 0},
        created_by=user_id,
    )
    db.add(job)
    db.flush()
    return job


def _remove_paths(paths: list[Path]) -> int:
    freed = 0
    for p in paths:
        try:
            st = p.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue  # a link (never followed) or something else
        p.unlink(missing_ok=True)
        freed += st.st_size
    return freed


def _remove_tree(path: Path) -> int:
    """Delete a file or folder; a link is removed itself, never what it points to."""
    try:
        st = path.lstat()
    except OSError:
        return 0
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        path.unlink(missing_ok=True)
        return st.st_size if stat.S_ISREG(st.st_mode) else 0
    size = tree_size(path)[0]
    shutil.rmtree(path)  # unlinks links inside, never descends into them
    return size


def _remove(
    db: Session, settings: Settings, action: str, key: str, params: dict, busy
) -> int | None:
    """Remove one previewed item if it is still eligible. Returns the bytes freed, or None
    when it was skipped (no longer eligible)."""
    from lectern import trash
    from lectern.fsops import LIBRARY_LOCK, TRASH_DIR, inside

    kind, _, ident = key.partition(":")
    library = settings.library_dir
    cutoff = params.get("cutoff")
    cutoff_dt = (
        datetime.strptime(cutoff, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=utcnow().tzinfo)
        if cutoff
        else None
    )
    if action == "trash":
        with LIBRARY_LOCK:
            if kind == "work":
                w = db.get(Work, int(ident))
                if w is None or w.trashed_at is None or (cutoff_dt and w.trashed_at >= cutoff_dt):
                    return None
                folder = library / w.dir_path
                size = tree_size(folder)[0] if inside(library / TRASH_DIR, folder) else 0
                for tf in db.scalars(
                    select(TrashedFile).where(
                        TrashedFile.work_id == w.id, TrashedFile.restored_at.is_(None)
                    )
                ):
                    size += tree_size(library / tf.trash_path)[0]
                trash.purge_work(db, settings, w)
                db.commit()
                return size
            tf = db.get(TrashedFile, int(ident))
            if tf is None or tf.restored_at is not None:
                return None
            if cutoff_dt and tf.trashed_at >= cutoff_dt:
                return None
            size = tree_size(library / tf.trash_path)[0]
            trash._remove_trashed_file(library, tf)
            db.delete(tf)
            db.commit()
            return size
    if action in PER_WORK:
        w = db.get(Work, int(ident))
        if w is None or w.trashed_at is not None:
            return None
        if action == "renditions":
            from lectern.jobs import delete_renditions

            freed = delete_renditions(db, settings, w)  # cancels its transcode, takes the lock
            db.commit()
            return freed
        if action == "read_aloud" and busy(w.id):
            return None  # being built right now
        with LIBRARY_LOCK:
            db.refresh(w)
            return _remove_paths(_WORK_FILES[action](settings, w))
    if action == "pre_restore":
        from lectern import backups

        d = settings.data_dir / ident
        if kind != "pre-restore" or not PRE_RESTORE_RE.match(ident) or d.is_symlink():
            return None
        if backups.interrupted_restore(settings) or not d.is_dir():
            return None
        return _remove_tree(d)
    # leftovers
    now = time.time()
    if kind == "upload":
        from lectern import uploads

        p = uploads.uploads_dir(settings) / ident
        session = db.scalar(
            select(UploadSession).where(UploadSession.public_id == Path(ident).stem)
        )
        if (
            session is not None
            and session.status == UploadStatus.ACTIVE
            and (session.expires_at >= utcnow())
        ):
            return None
        return _remove_paths([p])
    if kind == "staging":
        d = library / queue.STAGING_DIR / ident
        with LIBRARY_LOCK:
            if not _staging_stale(db, d, now):
                return None
            return _remove_tree(d)
    if kind == "tmp":
        d = settings.data_dir / "tmp" / ident
        try:
            if now - d.lstat().st_mtime <= TMP_MAX_AGE_S:
                return None
        except OSError:
            return None
        return _remove_tree(d)
    if kind == "jobs":
        if not ident.isdigit() or not _artifacts_stale(db, int(ident)):
            return None
        return _remove_tree(settings.data_dir / "jobs" / ident)
    if key == "backup-staging":
        from lectern import backups

        target = settings.backup_dir
        try:
            with backups._exclusive(target / ".lock", backups.BackupBusy("busy")):
                return _remove_tree(target / backups.STAGING_DIR)
        except backups.BackupBusy:
            return None
    return None


def _requeue(db: Session, job: Job, why: str) -> None:
    job.status = JobStatus.QUEUED
    job.lease_token_hash = job.lease_expires_at = job.heartbeat_at = None
    job.run_after = utcnow() + BACKUP_WAIT
    job.error = {
        "backup_running": "Waiting for the backup to finish.",
        "restore_interrupted": "Waiting: a restore was interrupted (lectern restore --resume "
        "or --undo).",
    }.get(why, why)
    db.commit()


def run(db: Session, settings: Settings, job: Job, busy=lambda _id: False) -> None:
    """Run a leased cleanup job: each previewed item still eligible, one at a time."""
    from lectern.security import audit

    payload = job.payload or {}
    action, params = payload.get("action"), payload.get("params") or {}
    state = dict(job.result or {})
    done = set(state.get("done") or [])
    freed = int(state.get("freed_bytes") or 0)
    errors = list(state.get("errors") or [])
    skipped = int(state.get("skipped") or 0)
    for key in payload.get("keys") or []:
        if key in done:
            continue
        db.refresh(job)
        if job.status != JobStatus.LEASED:
            return  # cancelled
        if why := blocked(settings):
            log.info("cleanup job %s waits: %s", job.id, why)
            _requeue(db, job, why)
            return
        try:
            n = _remove(db, settings, action, key, params, busy)
        except Exception as exc:  # noqa: BLE001 - recorded, the next item goes on
            db.rollback()
            log.warning("cleanup job %s: %s failed: %s", job.id, key, exc)
            errors = [*errors, f"{key}: {exc}"[:300]][-20:]
            n = None
        if n is None:
            skipped += 1
        else:
            freed += n
        done.add(key)
        job = db.get(Job, job.id)
        job.result = {
            "done": sorted(done),
            "freed_bytes": freed,
            "errors": errors,
            "skipped": skipped,
        }
        job.heartbeat_at = utcnow()
        job.error = None
        db.commit()
    db.refresh(job)
    if job.status != JobStatus.LEASED:
        return
    keys = payload.get("keys") or []
    job.status = JobStatus.FAILED if keys and len(errors) >= len(keys) else JobStatus.SUCCEEDED
    job.error = "; ".join(errors[:3]) or None
    job.finished_at = utcnow()
    job.lease_token_hash = job.lease_expires_at = None
    audit.record(
        db,
        "storage.cleanup.done",
        actor_user_id=job.created_by,
        target=f"job:{job.id}",
        detail=f"{action}: {len(done) - skipped} removed, {skipped} skipped, {freed} bytes freed"
        + (f", {len(errors)} errors" if errors else ""),
    )
    db.commit()
    log.info("cleanup job %s (%s): %d bytes freed", job.id, action, freed)


def job_json(job: Job) -> dict:
    payload, result = job.payload or {}, job.result or {}
    return {
        "id": job.id,
        "action": payload.get("action"),
        "status": job.status.value,
        "total": len(payload.get("keys") or []),
        "done": len(result.get("done") or []),
        "skipped": result.get("skipped") or 0,
        "expected_bytes": payload.get("expected_bytes") or 0,
        "freed_bytes": result.get("freed_bytes") or 0,
        "errors": result.get("errors") or [],
        "error": job.error,
        "run_after": job.run_after.isoformat().replace("+00:00", "Z") if job.run_after else None,
        "created_at": job.created_at.isoformat().replace("+00:00", "Z"),
        "finished_at": job.finished_at.isoformat().replace("+00:00", "Z")
        if job.finished_at
        else None,
    }


# Never offered to external workers: it deletes files on this server.
queue.register(queue.KindHandler(kind=CLEANUP, capability=lambda _payload: CLEANUP, remote=False))
