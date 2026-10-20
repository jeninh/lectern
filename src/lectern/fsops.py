"""Filesystem primitives for changing the library: one lock, atomic moves, path checks.

Every operation that moves or removes files under the library directory (filing an import,
renaming a work's folder, reordering tracks, trash, restore, purge, inbox ingest, the audio
rescan) runs under :data:`LIBRARY_LOCK`, so the database and the folders never disagree for
another thread to see. Lectern runs as one process; the lock is in-process.

Moves never overwrite: the destination must not exist. Within one filesystem they are a
single ``rename``. Across filesystems (``EXDEV``: a library spread over mounts, or the data
volume and the library) the source is copied to a hidden temporary name next to the
destination, flushed, checked (size and, for files whose digest we track, SHA-256), renamed
into place, and only then removed. A failure at any step leaves the source untouched and
removes the partial copy.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import secrets
import shutil
import threading
from collections.abc import Iterable
from pathlib import Path

from lectern.ingest.hashing import sha256_file

log = logging.getLogger("lectern.fsops")

LIBRARY_LOCK = threading.RLock()
STAGING_DIR = "_staging"
TRASH_DIR = "_trash"


class MoveError(OSError):
    pass


def inside(root: Path, path: Path) -> bool:
    """``path`` resolves to somewhere strictly below ``root`` (symlinks followed)."""
    try:
        return root.resolve() in path.resolve().parents
    except OSError:
        return False


def checked(root: Path, rel: str) -> Path:
    """``root / rel``, refusing absolute paths, ``..``, and anything resolving outside."""
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or not rel:
        raise MoveError(f"unsafe path {rel!r}")
    full = root / p
    if not inside(root, full):
        raise MoveError(f"path escapes {root}: {rel!r}")
    return full


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _copy_file(src: Path, dst: Path) -> None:
    with src.open("rb") as fi, dst.open("xb") as fo:
        shutil.copyfileobj(fi, fo, 1024 * 1024)
        fo.flush()
        os.fsync(fo.fileno())
    shutil.copystat(src, dst)


def _verify(src: Path, dst: Path, sha256: str | None) -> None:
    if src.stat().st_size != dst.stat().st_size:
        raise MoveError(f"size changed while copying {src}")
    if sha256 is not None and sha256_file(dst) != sha256:
        raise MoveError(f"checksum mismatch copying {src}")


def move_file(src: Path, dst: Path, *, sha256: str | None = None) -> None:
    """Move one file to a path that must not exist yet. See the module docstring."""
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)  # atomic "rename unless exists" on one filesystem
        os.unlink(src)
        return
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise FileExistsError(dst) from exc
        if exc.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EMLINK, errno.EACCES):
            raise
        cross_device = exc.errno == errno.EXDEV
    if not cross_device:
        # No hard links here (some network and FUSE filesystems): plain rename, same fs. The
        # caller holds the library lock and checked that ``dst`` does not exist.
        try:
            os.rename(src, dst)
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
    tmp = dst.parent / f".lectern-move-{secrets.token_hex(6)}{dst.suffix}"
    try:
        _copy_file(src, tmp)
        _verify(src, tmp, sha256)
        if dst.exists():
            raise FileExistsError(dst)
        os.rename(tmp, dst)
        _fsync_dir(dst.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    src.unlink()


def move_dir(src: Path, dst: Path, *, digests: dict[str, str] | None = None) -> None:
    """Move a directory to a path that must not exist. ``digests`` maps relative paths of
    files whose SHA-256 must survive a cross-filesystem copy (every book file and track)."""
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)  # dst does not exist (checked under the library lock)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    tmp = dst.parent / f".lectern-move-{secrets.token_hex(6)}"
    try:
        tmp.mkdir()
        for path in sorted(src.rglob("*")):
            rel = path.relative_to(src)
            target = tmp / rel
            if path.is_symlink():
                continue  # never carried along: a link could point anywhere
            if path.is_dir():
                target.mkdir(exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_file(path, target)
            _verify(path, target, (digests or {}).get(rel.as_posix()))
        if dst.exists():
            raise FileExistsError(dst)
        os.rename(tmp, dst)
        _fsync_dir(dst.parent)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    shutil.rmtree(src)


def prune_empty_parents(d: Path, stop: Path) -> None:
    """Remove ``d`` and its parents while they are empty, never ``stop`` itself."""
    stop = stop.resolve()
    d = d.resolve()
    while d != stop and stop in d.parents:
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


def free_dir(library: Path, author: str, title: str, *, keep: str | None = None) -> str:
    """A free ``<author>/<title>`` (``<title> (2)``, ...) relative path. ``keep`` (the work's
    current folder) counts as free, so a rename to the same name is a no-op."""
    base = Path(author) / title
    rel = base
    n = 2
    while (library / rel).exists() and rel.as_posix() != keep:
        rel = base.parent / f"{base.name} ({n})"
        n += 1
    return rel.as_posix()


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def unique_name(directory: Path, name: str, taken: Iterable[str] = ()) -> str:
    """``name`` or ``stem 2.ext``, ``stem 3.ext``, ... not on disk and not in ``taken``."""
    taken = set(taken)
    p = Path(name)
    candidate = name
    n = 2
    while (directory / candidate).exists() or candidate in taken:
        candidate = (p.parent / f"{p.stem} {n}{p.suffix}").as_posix()
        n += 1
    return candidate
