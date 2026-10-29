"""Server health (issue #75, docs/health.md): one owner-only page, one overall status, and alerts
when something goes wrong or recovers.

:func:`evaluate` runs every check against what Lectern already records (backup status, the
job queue, worker presence, the schema revision) and the free space of the data, library, and
backup volumes. Each :class:`Check` has a level: ``ok``, ``info`` (nothing to do, e.g. backups
off), ``warning``, or ``critical``; the overall level is the worst one.

:func:`refresh` evaluates, stores the result in ``instance_settings["health"]``, and alerts
every owner on transitions: a check getting worse (ok → warning, warning → critical) and, once
it is back to ok, a recovery. Each check alerts at most once a day per level (like the backup
failure alert), and a recovery follows only an alert that was sent. Alerts are in-app
notifications; with a push target in ``.env`` they are also pushed (:mod:`lectern.push_alerts`)
with the check's title, level, and ``summary`` only, which is why summaries never hold book
titles, usernames, worker names, or paths (``detail`` is for the page).

The maintenance thread refreshes every :data:`POLL_S`; the page shows that stored result while
it is younger than :data:`STALE_S` (so polling it is a row read), and evaluates by itself only
without background jobs or on "Check now".

Extension point (#76, restore drills): :func:`register_check` adds a provider whose checks
are evaluated, shown (in their ``group``), and alerted like the built-in ones.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from sqlalchemy import false, func, select, text
from sqlalchemy.orm import Session

from lectern import egress, notify, push_alerts, queue
from lectern.config import Settings
from lectern.models import InstanceSetting, Job, JobStatus, Work, WorkerCredential, utcnow

log = logging.getLogger("lectern.health")

OK, INFO, WARNING, CRITICAL = "ok", "info", "warning", "critical"
RANK = {OK: 0, INFO: 0, WARNING: 1, CRITICAL: 2}
GROUPS = ("backups", "disk", "jobs", "workers", "other")
STATE_KEY = "health"
KIND = "health.alert"
POLL_S = 300.0
STALE_S = POLL_S + 60.0  # the page refreshes by itself only when the thread has not
ALERT_EVERY = timedelta(hours=20)  # "at most once a day", as the backup failure alert
BACKUP_LATE = timedelta(hours=26)  # a nightly backup a couple of hours past its night
BACKUP_CRITICAL = timedelta(hours=72)
JOB_DAYS = 7
STUCK_QUEUED = timedelta(hours=24)  # claimable for a day and nobody took it
STUCK_LEASED = timedelta(hours=1)  # held without a heartbeat
#: Job kinds the page watches (saving a web article fails on bad pages: not a server problem).
JOB_LABELS = {
    "align": "Alignment",
    "tts": "Narration",
    "ocr": "Text recognition",
    "pdf2epub": "PDF conversion",
    "transcode": "Data-saver copy",
}
WORKER_CAPS = (
    (queue.TTS_KOKORO, "Kokoro narration"),
    (queue.TTS_PIPER, "Piper narration"),
    (queue.ALIGN, "Alignment"),
)
_TS = "%Y-%m-%dT%H:%M:%SZ"
_lock = threading.Lock()


@dataclass
class Check:
    id: str  # stable: transitions are tracked by it
    group: str  # one of GROUPS
    title: str
    level: str
    summary: str  # short, and pushed: no titles, usernames, worker names, or paths
    detail: dict = field(default_factory=dict)  # for the page only


Provider = Callable[[Session, Settings, datetime], "Check | Iterable[Check] | None"]
_PROVIDERS: dict[str, Provider] = {}


def register_check(name: str, provider: Provider) -> None:
    """Add checks from another module (e.g. the restore drill, #76). ``provider(db, settings,
    now)`` returns a :class:`Check`, several, or None; it must be quick (it runs on every
    refresh) and read state its own job recorded rather than doing the work itself."""
    _PROVIDERS[name] = provider


def unregister_check(name: str) -> None:
    _PROVIDERS.pop(name, None)


# --- helpers ------------------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).strftime(_TS) if dt else None


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, _TS).replace(tzinfo=UTC)
    except ValueError:
        return None


def _ago(delta: timedelta) -> str:
    s = max(0, int(delta.total_seconds()))
    if s < 3600:
        return f"{max(1, s // 60)} min"
    if s < 2 * 86400:
        return f"{s // 3600} h"
    return f"{s // 86400} days"


def _gib(n: int) -> str:
    return f"{n / 2**30:.1f} GiB"


def worst(levels: Iterable[str]) -> str:
    out = OK
    for lv in levels:
        if RANK.get(lv, 0) > RANK[out]:
            out = lv
    return out


# --- backups ------------------------------------------------------------------------------------


def _backup_checks(db: Session, settings: Settings, now: datetime) -> list[Check]:
    from lectern import backups

    cfg, st = backups.load_config(db), backups.load_status(db)
    last_ok = _parse(st.get("last_success_at"))
    failed = st.get("last_result") == "failed"
    detail = {
        "enabled": cfg.enabled,
        "hour_utc": cfg.hour_utc,
        "running": backups.is_running(),
        "last_success_at": st.get("last_success_at"),
        "last_result": st.get("last_result"),
        "last_error": st.get("last_error") if failed else None,
        "last_error_at": st.get("last_error_at") if failed else None,
    }
    if not cfg.enabled:
        level, summary = INFO, "Off (Admin → Nightly backups)"
    else:
        target = backups.target_info(settings)
        detail["target"] = {k: target[k] for k in ("exists", "writable", "free_bytes")}
        since = last_ok or _parse(st.get("enabled_at"))
        age = now - since if since else None
        never = "No successful backup yet"
        if not target["writable"]:
            level, summary = CRITICAL, "The backup folder is missing or not writable"
        elif not target["restic_available"]:
            level, summary = CRITICAL, "restic is not installed on the server"
        elif age is not None and age > BACKUP_CRITICAL:
            level, summary = CRITICAL, f"No backup for {_ago(age)}" if last_ok else never
        elif age is not None and age > BACKUP_LATE:
            level, summary = WARNING, f"Overdue: last backup {_ago(age)} ago" if last_ok else never
        elif failed:
            level, summary = WARNING, "The last backup failed"
        elif last_ok:
            level, summary = OK, f"Last backup {_ago(now - last_ok)} ago"
        else:
            level, summary = OK, "Waiting for the first night"
    checks = [Check("backups.nightly", "backups", "Nightly backups", level, summary, detail)]

    integrity_failed = failed and "integrity check" in (st.get("last_error") or "")
    if integrity_failed:
        lv, msg = CRITICAL, "The database copy failed its integrity check"
    elif last_ok:
        lv, msg = OK, "Passed in the last backup"
    else:
        lv, msg = INFO, "Checked by every backup; none yet"
    checks.append(
        Check(
            "database.integrity",
            "other",
            "Database integrity",
            lv,
            msg,
            {"checked_at": st.get("last_success_at")},
        )
    )
    return checks


# --- disk ---------------------------------------------------------------------------------------


def disk_level(free: int, total: int, settings: Settings) -> str:
    """The level of a volume with ``free`` of ``total`` bytes free (shared with the storage
    dashboard, #74)."""
    if total <= 0:
        return OK
    pct = 100.0 * free / total
    if pct < settings.health_disk_critical_percent:
        return CRITICAL
    if pct < settings.health_disk_warn_percent:
        return WARNING
    return OK


def _disk_checks(settings: Settings) -> list[Check]:
    volumes: dict[int, dict] = {}
    for key, label, path in (
        ("data", "Data", settings.data_dir),
        ("library", "Library", settings.library_dir),
        ("backups", "Backups", settings.backup_dir),
    ):
        try:
            dev = os.stat(path).st_dev
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        if dev in volumes:
            volumes[dev]["labels"].append(label.lower())
            volumes[dev]["paths"].append(str(path))
        else:
            volumes[dev] = {"key": key, "labels": [label], "paths": [str(path)], "usage": usage}
    checks = []
    for v in volumes.values():
        free, total = v["usage"].free, v["usage"].total
        pct = 100.0 * free / total if total else 100.0
        checks.append(
            Check(
                f"disk.{v['key']}",
                "disk",
                f"{' and '.join(v['labels'])} volume".capitalize(),
                disk_level(free, total, settings),
                f"{pct:.0f}% free ({_gib(free)} of {_gib(total)})",
                {
                    "paths": v["paths"],
                    "free_bytes": free,
                    "total_bytes": total,
                    "warn_percent": settings.health_disk_warn_percent,
                    "critical_percent": settings.health_disk_critical_percent,
                },
            )
        )
    return checks


# --- jobs ---------------------------------------------------------------------------------------


def _job_item(j: Job, titles: dict[int, str], now: datetime) -> dict:
    return {
        "id": j.id,
        "kind": j.kind,
        "label": JOB_LABELS.get(j.kind, j.kind),
        "work_id": j.work_id,
        "work_title": titles.get(j.work_id) if j.work_id else None,
        "error": (j.error or "")[:300] or None,
        "at": _iso(j.finished_at or j.heartbeat_at or j.run_after or j.created_at),
    }


def _counts(items: list[dict]) -> str:
    by: dict[str, int] = {}
    for i in items:
        by[i["label"]] = by.get(i["label"], 0) + 1
    return ", ".join(f"{label} {n}" for label, n in sorted(by.items()))


def _job_checks(db: Session, now: datetime) -> list[Check]:
    kinds = list(JOB_LABELS)
    since = now - timedelta(days=JOB_DAYS)
    # Newest live or successful job per (work, kind): a failure before it was dealt with.
    later = {
        (w, k): m
        for w, k, m in db.execute(
            select(Job.work_id, Job.kind, func.max(Job.id))
            .where(
                Job.kind.in_(kinds),
                Job.status.in_((JobStatus.SUCCEEDED, JobStatus.QUEUED, JobStatus.LEASED)),
            )
            .group_by(Job.work_id, Job.kind)
        )
    }
    failed: dict[tuple, Job] = {}
    for j in db.scalars(
        select(Job)
        .where(
            Job.kind.in_(kinds),
            Job.status == JobStatus.FAILED,
            func.coalesce(Job.finished_at, Job.updated_at) >= since,
        )
        .order_by(Job.id.desc())
        .limit(500)
    ):
        key = (j.work_id, j.kind)
        if key not in failed and later.get(key, 0) < j.id:
            failed[key] = j
    # A paused queue holds its queued jobs on purpose, so only a silent lease is stuck then.
    # Running again, a queued job counts only from when the queue was resumed (resuming leaves
    # run_after alone), recorded as the ``queue_paused`` setting's updated_at.
    leased_stuck = (Job.status == JobStatus.LEASED) & (
        func.coalesce(Job.heartbeat_at, Job.started_at) < now - STUCK_LEASED
    )
    stuck_clause = leased_stuck
    if not queue.queue_paused(db):
        row = db.get(InstanceSetting, "queue_paused")
        queued_stuck = (Job.status == JobStatus.QUEUED) & (Job.run_after < now - STUCK_QUEUED)
        if row is not None and row.updated_at >= now - STUCK_QUEUED:
            queued_stuck = false()
        stuck_clause = queued_stuck | leased_stuck
    stuck = list(
        db.scalars(select(Job).where(Job.kind.in_(kinds), stuck_clause).order_by(Job.id).limit(200))
    )
    ids = {j.work_id for j in [*failed.values(), *stuck] if j.work_id}
    titles: dict[int, str] = {}
    trashed: set[int] = set()
    if ids:
        for wid, title, gone in db.execute(
            select(Work.id, Work.title, Work.trashed_at).where(Work.id.in_(ids))
        ):
            titles[wid] = title
            if gone is not None:
                trashed.add(wid)
    fail_items = [
        _job_item(j, titles, now)
        for j in sorted(failed.values(), key=lambda j: -j.id)
        if j.work_id not in trashed
    ]
    stuck_items = [_job_item(j, titles, now) for j in stuck if j.work_id not in trashed]
    n, m = len(fail_items), len(stuck_items)
    return [
        Check(
            "jobs.failed",
            "jobs",
            "Failed jobs",
            WARNING if n else OK,
            f"{n} failed in the last {JOB_DAYS} days ({_counts(fail_items)})"
            if n
            else f"None in the last {JOB_DAYS} days",
            {"items": fail_items[:50], "total": n, "days": JOB_DAYS},
        ),
        Check(
            "jobs.stuck",
            "jobs",
            "Stuck jobs",
            WARNING if m else OK,
            f"{m} waiting or silent for too long ({_counts(stuck_items)})" if m else "None",
            {"items": stuck_items[:50], "total": m},
        ),
    ]


# --- workers ------------------------------------------------------------------------------------


def _seen(c: WorkerCredential) -> datetime | None:
    return c.status_at or c.last_used_at


def presence(c: WorkerCredential, now: datetime) -> str:
    """``ready``/``busy``/``paused`` while it calls in; ``off`` (it said it stopped) or
    ``offline`` (silent for :data:`lectern.queue.WORKER_SEEN_S`) otherwise."""
    seen = _seen(c)
    if seen is None or now - seen > timedelta(seconds=queue.WORKER_SEEN_S):
        return "offline"
    return c.state or "ready"


def workers(db: Session, now: datetime | None = None) -> list[dict]:
    """Each worker credential that is not revoked: for the page, with its owner-chosen name."""
    now = now or utcnow()
    out = []
    for c in db.scalars(
        select(WorkerCredential)
        .where(WorkerCredential.revoked_at.is_(None))
        .order_by(WorkerCredential.id)
    ):
        info = c.info or {}
        out.append(
            {
                "id": c.id,
                "label": c.label,
                "state": presence(c, now),
                "seen_at": _iso(_seen(c)),
                "capabilities": c.advertised if c.advertised is not None else [],
                "unavailable": info.get("unavailable") or {},
                "version": info.get("version"),
            }
        )
    return out


def _worker_checks(db: Session, settings: Settings, now: datetime) -> list[Check]:
    from lectern.routes.api.align import server_capabilities

    local = server_capabilities(db, settings)
    creds = list(db.scalars(select(WorkerCredential).where(WorkerCredential.revoked_at.is_(None))))
    grace = timedelta(minutes=settings.health_worker_grace_minutes)
    checks = []
    for cap, title in WORKER_CAPS:
        offering, withheld = [], {}
        for c in creds:
            advertised = c.advertised if c.advertised is not None else [queue.ALIGN]
            if cap in queue.claimable(advertised, c):
                offering.append(c)
            reason = ((c.info or {}).get("unavailable") or {}).get(cap)
            if reason:
                withheld[c.id] = (c, str(reason)[:200])
        states = [presence(c, now) for c in offering]
        recent_withheld = [r for c, r in withheld.values() if presence(c, now) != "offline"]
        known = offering + [c for c, _r in withheld.values()]
        detail: dict = {"local": cap in local, "workers": len(offering)}
        if cap in local:
            level, summary = OK, "Runs on this server"
        elif any(s in ("ready", "busy") for s in states):
            level, summary = OK, "Worker online"
        elif "paused" in states:
            level, summary = INFO, "Worker paused"
        elif recent_withheld:
            level, summary = WARNING, "The worker's self-test failed"
            detail["reason"] = recent_withheld[0]
        elif known:
            seen = max((s for s in map(_seen, known) if s), default=None)
            detail["seen_at"] = _iso(seen)
            if seen is None or now - seen > grace:
                level = WARNING
                summary = f"Worker not seen for {_ago(now - seen)}" if seen else "Worker never seen"
            else:
                level, summary = INFO, "Worker stopped a few minutes ago"
        elif cap == queue.TTS_KOKORO and settings.tts_kokoro_local:
            level, summary = WARNING, "Turned on here, but Kokoro is not installed"
        else:
            level, summary = INFO, "Not set up"
        checks.append(Check(f"workers.{cap}", "workers", title, level, summary, detail))
    return checks


# --- other --------------------------------------------------------------------------------------


@lru_cache
def _schema_heads() -> frozenset[str]:
    from alembic.script import ScriptDirectory

    from lectern.migrate import alembic_config

    return frozenset(ScriptDirectory.from_config(alembic_config("sqlite://")).get_heads())


def _other_checks(db: Session, settings: Settings) -> list[Check]:
    try:
        current = frozenset(db.scalars(text("SELECT version_num FROM alembic_version")))
    except Exception:  # noqa: BLE001 - a database without the table is not at head either
        current = frozenset()
    heads = _schema_heads()
    at_head = current == heads
    checks = [
        Check(
            "database.migrations",
            "other",
            "Database schema",
            OK if at_head else CRITICAL,
            "Up to date" if at_head else "Not at the latest revision",
            {"current": sorted(current), "head": sorted(heads)},
        )
    ]
    offline = egress.offline()
    checks.append(
        Check(
            "offline.mode",
            "other",
            "Offline mode",
            INFO if offline else OK,
            "On: no internet connections, push alerts paused" if offline else "Off",
            {"on": offline, "forced": settings.offline},
        )
    )
    push = push_alerts.describe(settings)
    last = push["last"]
    if push["state"] == "off":
        level, summary = INFO, "Off (set in .env)"
    elif push["state"] == "not_configured":
        level, summary = WARNING, "Not set up correctly: check .env"
    elif push["state"] == "offline":
        level, summary = INFO, "Paused by offline mode"
    elif last and not last.get("ok") and last.get("error") != "offline":
        level, summary = WARNING, "The last push was not delivered"
    else:
        level, summary = OK, f"On ({push['kind']})"
    checks.append(Check("alerts.push", "other", "Push alerts", level, summary, push))
    return checks


# --- evaluation and transitions -----------------------------------------------------------------


def evaluate(db: Session, settings: Settings, now: datetime | None = None) -> list[Check]:
    now = now or utcnow()
    checks: list[Check] = []
    parts: list[tuple[str, Callable[[], Iterable[Check]]]] = [
        ("backups", lambda: _backup_checks(db, settings, now)),
        ("disk", lambda: _disk_checks(settings)),
        ("jobs", lambda: _job_checks(db, now)),
        ("workers", lambda: _worker_checks(db, settings, now)),
        ("other", lambda: _other_checks(db, settings)),
    ]
    for name, provider in _PROVIDERS.items():
        parts.append((name, lambda p=provider: _as_list(p(db, settings, now))))
    for name, part in parts:
        try:
            checks.extend(_valid(part()))
        except Exception:  # noqa: BLE001 - one broken check must not hide the others
            log.exception("health check %s failed", name)
            checks.append(
                Check(f"check.{name}", "other", f"Check: {name}", WARNING, "Could not run")
            )
    return checks


def _valid(found: Iterable[Check]) -> list[Check]:
    """Only well-formed checks, with JSON-safe details (they are stored); raises otherwise, so
    a broken provider is shown as "Could not run" instead of failing the whole refresh."""
    out = []
    for c in found:
        if not isinstance(c, Check) or c.level not in RANK or not isinstance(c.detail, dict):
            raise TypeError(f"not a health check: {type(c).__name__}")
        c.detail = json.loads(json.dumps(c.detail))  # TypeError on a datetime, a set, ...
        c.id, c.group, c.title, c.summary = str(c.id), str(c.group), str(c.title), str(c.summary)
        out.append(c)
    return out


def _as_list(result: Check | Iterable[Check] | None) -> list[Check]:
    if result is None:
        return []
    return [result] if isinstance(result, Check) else list(result)


def _alert(db: Session, owners: list, check: Check, level: str, key: str) -> bool:
    """One alert for every owner (each at most once per ALERT_EVERY); True if any was sent."""
    recovered = level == OK
    title = f"{check.title}: {'back to OK' if recovered else level}"
    body = check.summary if recovered else f"{check.summary}. See Health."
    data = {
        "key": key,
        "check": check.id,
        "level": level,
        "event": secrets.token_hex(8),
        "push": {"title": f"Lectern: {title}", "message": check.summary, "level": level},
    }
    sent = False
    for o in owners:
        if notify.recently_sent(db, o.id, KIND, key, ALERT_EVERY):
            continue
        notify.notify(
            db, o, KIND, title, body, severity="info" if recovered else "warning", data=data
        )
        sent = True
    return sent


def _transitions(db: Session, previous: dict, checks: list[Check], now: datetime) -> dict:
    owners = notify.owners(db)
    state = {}
    for c in checks:
        prev = previous.get(c.id) or {"level": OK, "since": _iso(now), "alerted": False}
        level, was = RANK.get(c.level, 0), RANK.get(prev.get("level"), 0)
        alerted = bool(prev.get("alerted"))
        # Worse than before, or still bad and not alerted yet (its alert was deduped after a
        # flap): try again on every refresh; the dedupe caps it at one a day per level.
        if level > was or (level > 0 and not alerted):
            alerted = _alert(db, owners, c, c.level, f"{c.id}:{c.level}") or alerted
        elif level == 0 and was > 0 and alerted:
            _alert(db, owners, c, OK, f"{c.id}:{OK}")
            alerted = False
        since = prev.get("since") if level == was else _iso(now)
        state[c.id] = {"level": OK if level == 0 else c.level, "since": since, "alerted": alerted}
    return state


def _stored(db: Session) -> dict:
    row = db.get(InstanceSetting, STATE_KEY)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def refresh(
    db: Session, settings: Settings, now: datetime | None = None, *, max_age_s: float = 0
) -> dict:
    """Evaluate, alert on transitions, store, and commit; returns the stored view. With
    ``max_age_s``, a stored result at most that old is returned as is (without waiting for a
    refresh another thread is running, which a hung backup mount could hold up)."""
    now = now or utcnow()
    if max_age_s and _fresh(_stored(db), now, max_age_s):
        return _stored(db)
    with _lock:
        stored = _stored(db)
        if max_age_s and _fresh(stored, now, max_age_s):
            return stored
        checks = evaluate(db, settings, now)
        view = {
            "checked_at": _iso(now),
            "level": worst(c.level for c in checks),
            "checks": [asdict(c) for c in checks],
            "state": _transitions(db, stored.get("state") or {}, checks, now),
        }
        row = db.get(InstanceSetting, STATE_KEY)
        if row is None:
            db.add(InstanceSetting(key=STATE_KEY, value=view))
        else:
            row.value = view
        try:
            db.commit()  # pushes are handed over only now (lectern.push_alerts)
        except Exception:
            db.rollback()
            raise
        return view


def _fresh(stored: dict, now: datetime, max_age_s: float) -> bool:
    checked = _parse(stored.get("checked_at"))
    return checked is not None and 0 <= (now - checked).total_seconds() <= max_age_s


def summary(db: Session) -> dict:
    """The overall level from the last refresh (the top bar's badge): one row read."""
    stored = _stored(db)
    return {"level": stored.get("level"), "checked_at": stored.get("checked_at")}


def tick(session_factory, settings: Settings, now: datetime | None = None) -> dict:
    with session_factory() as db:
        return refresh(db, settings, now)
