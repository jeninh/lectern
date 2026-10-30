"""What the KOReader plugin reports about its device, for the Devices dashboard (issue #86).

The plugin sends a small report with its quiet sync (``PUT /koplugin/report``, the device's
catalogue password): battery level and charging state, free storage, its own and KOReader's
version, and the Lectern books on the device (by work id for files it downloaded from Lectern,
by KOReader document digest for other books with a sidecar), each with its position. Only the
latest report is kept per device (:class:`lectern.models.DeviceReport`); a report older than
the one stored (sent late from the plugin's outbox) changes nothing.

Reading it
    The books are kept as sent and resolved whenever the dashboard is read: a digest becomes
    the work of the library file with that partial MD5, and a book the device's user can no
    longer see is shown as hidden, never by title. Books no longer in the library are only
    counted. Who may read a device's report follows the queues (lectern.device_queue): the
    owner sees every device, everyone else only their own.

Nothing here leaves the server, and nothing is looked up elsewhere when a report is read.
"""

from __future__ import annotations

import functools
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern.access import visible_works
from lectern.koplugin import PLUGIN_DIR
from lectern.models import AppPassword, BookFile, DeviceReport, User, Work, utcnow

MAX_BOOKS = 2000
MAX_AGE = timedelta(days=30)


@functools.cache
def bundled_plugin_version() -> str | None:
    """The ``version`` in the bundled plugin's ``_meta.lua``, if it declares one."""
    try:
        text = (PLUGIN_DIR / "_meta.lua").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"""\bversion\s*=\s*["']([^"']{1,32})["']""", text)
    return m.group(1) if m else None


def _version_key(v: str | None) -> tuple[int, ...] | None:
    parts = [int(p) for p in re.findall(r"\d+", v or "")[:6]]
    if not parts:
        return None
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()  # 1.10 and 1.10.0 are the same version
    return tuple(parts)


def update_available(reported: str | None, bundled: str | None = None) -> bool:
    """The device runs an older plugin than the one this server hands out. Unknown or
    unreadable versions on either side never claim an update."""
    have, latest = _version_key(reported), _version_key(bundled or bundled_plugin_version())
    return have is not None and latest is not None and have < latest


def store(
    db: Session,
    pw: AppPassword,
    *,
    age: int,
    battery: int | None,
    charging: bool | None,
    free_bytes: int | None,
    plugin_version: str | None,
    koreader_version: str | None,
    books: list[dict],
) -> bool:
    """Keep this report unless a newer one is already stored. Returns whether it was kept."""
    now = utcnow()
    as_of = now - min(timedelta(seconds=max(age, 0)), MAX_AGE)
    row = db.get(DeviceReport, pw.id)
    if row is not None and row.as_of > as_of:
        return False
    if row is None:
        row = DeviceReport(app_password_id=pw.id)
        db.add(row)
    row.reported_at, row.as_of = now, as_of
    row.battery, row.charging, row.free_bytes = battery, charging, free_bytes
    row.plugin_version, row.koreader_version = plugin_version, koreader_version
    row.books = books[:MAX_BOOKS]
    return True


def books_on_device(db: Session, report: DeviceReport | None, user: User) -> dict:
    """The report's books resolved against the library as ``user`` (the device's user) sees
    it: ``{"books": [(work or None if hidden, percentage)], "not_in_library": n}``, one entry
    per work, in the order reported."""
    entries = list(report.books or []) if report else []
    ids = {e["w"] for e in entries if isinstance(e.get("w"), int)}
    digests = {e["d"] for e in entries if isinstance(e.get("d"), str)}
    by_digest: dict[str, int] = {}
    if digests:
        by_digest = dict(
            db.execute(
                select(BookFile.partial_md5, BookFile.work_id).where(
                    BookFile.partial_md5.in_(digests)
                )
            ).all()
        )
    wanted = ids | set(by_digest.values())
    existing = set(db.scalars(select(Work.id).where(Work.id.in_(wanted)))) if wanted else set()
    visible = {
        w.id: w for w in db.scalars(select(Work).where(Work.id.in_(existing), visible_works(user)))
    }
    out: list[tuple[Work | None, float | None]] = []
    seen: set[int] = set()
    missing = 0
    for e in entries:
        work_id = e.get("w") if isinstance(e.get("w"), int) else None
        if work_id not in existing:
            work_id = by_digest.get(e.get("d"))  # type: ignore[arg-type]
        if work_id not in existing:
            missing += 1
            continue
        if work_id in seen:
            continue
        seen.add(work_id)
        p = e.get("p")
        out.append((visible.get(work_id), float(p) if isinstance(p, int | float) else None))
    return {"books": out, "not_in_library": missing}
