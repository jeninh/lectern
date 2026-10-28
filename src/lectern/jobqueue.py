"""The job queue page (issue #87): every background job, grouped, with the owner's controls.

Jobs are grouped running (leased) / waiting / paused / failed / done (recent succeeded and
cancelled). A job queued by a bulk request (``payload.batch``, the selection bar's Convert
to EPUB, #61) is listed inside its batch, one row with per-book status.

Controls, none of which needs a schema change:

- **Cancel** any kind (``lectern.tts.jobs.cancel`` for narration, ``lectern.align.jobs.cancel_job``
  for the rest, as ``POST /api/jobs/{id}/cancel``). A worker's next heartbeat or upload
  answers ``409 lease_lost``, which stops it and frees it. Local OCR, alignment, and
  narration stop at their next lease check; a local transcode stops between tracks; a local
  conversion (``pdf2epub``) runs to the end of its current stage and then publishes nothing. Partial uploads are removed at once (:func:`drop_partial`), except a
  narration's, kept so a Retry resumes (the tts sweep removes them after 7 days).
- **Pause / resume** a waiting job: it stays ``queued`` with ``run_after`` at
  :data:`lectern.queue.PAUSED_UNTIL`, which no claim takes. **Pause the queue**:
  ``instance_settings.queue_paused``, checked by every claim; running jobs finish.
- **Reorder**: ``priority`` (lower runs first; every claim orders by it). Top is one ahead of
  every other queued job, bottom one behind. A job still runs only where its capability can.
- **Retry** a failed job: the same row queued again (same payload, attempts reset), so a
  worker resumes from what it already uploaded.

Owners see and manage every job. Members and readers see only jobs they started, may cancel
them (and their own batches), and see a book's title only while they may see the book.
"""

from __future__ import annotations

import shutil
from datetime import datetime

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from lectern import limits, queue
from lectern.access import visible_works
from lectern.models import (
    JOB_ACTIVE_STATUSES,
    Job,
    JobStatus,
    Role,
    User,
    Work,
    WorkerCredential,
    utcnow,
)

CLEANUP = "storage-cleanup"  # lectern.jobs.CLEANUP: names files and sizes, owner only
RECENT_FAILED = 50
RECENT_DONE = 30
# Waiting jobs that get the full "why" (lectern.queue.explain_wait, a few queries each); the
# rest of a long queue just shows its place. Keeps a 10 s poll cheap on a Pi.
EXPLAINED = 30
LIMIT_REACHED = (limits.FEATURE_LIMIT, limits.GLOBAL_LIMIT)
GROUPS = ("running", "waiting", "paused", "failed", "done")


class Refused(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


def scope(stmt, user: User):
    """Owners: every job. Everyone else: the jobs they started (never a storage cleanup)."""
    if user.role == Role.OWNER:
        return stmt
    return stmt.where(Job.created_by == user.id, Job.kind != CLEANUP)


def may_see(job: Job, user: User) -> bool:
    """:func:`scope` for one job; also who may cancel it, here and on a book's page."""
    return user.role == Role.OWNER or (job.created_by == user.id and job.kind != CLEANUP)


def shows_error(user: User, sees_book: bool) -> bool:
    """Whether a job's error text is shown: it can hold library paths (Author/Title) and engine
    logs, so never to readers, and to members only on a job whose book they can see."""
    return user.role == Role.OWNER or (user.role != Role.READER and sees_book)


def batch_of(job: Job) -> str | None:
    batch = (job.payload or {}).get("batch")
    return batch if isinstance(batch, str) else None


def group_of(job: Job) -> str:
    if job.status == JobStatus.LEASED:
        return "running"
    if job.status == JobStatus.QUEUED:
        return "paused" if queue.is_paused(job) else "waiting"
    return "failed" if job.status == JobStatus.FAILED else "done"


def batch_jobs(db: Session, batch: str, user: User | None = None) -> list[Job]:
    stmt = select(Job).where(Job.payload["batch"].as_string() == batch)
    if user is not None:
        stmt = scope(stmt, user)
    return list(db.scalars(stmt.order_by(Job.id)).all())


# --- listing --------------------------------------------------------------------------------------


def listing(db: Session, user: User, server_caps: set[str] | None) -> dict:
    owner = user.role == Role.OWNER
    active = db.scalars(
        scope(select(Job).where(Job.status.in_(JOB_ACTIVE_STATUSES)), user).order_by(
            Job.priority, Job.created_at, Job.id
        )
    ).all()
    failed = db.scalars(
        scope(select(Job).where(Job.status == JobStatus.FAILED), user)
        .order_by(Job.id.desc())
        .limit(RECENT_FAILED)
    ).all()
    done = db.scalars(
        scope(select(Job).where(Job.status.in_((JobStatus.SUCCEEDED, JobStatus.CANCELLED))), user)
        .order_by(Job.id.desc())
        .limit(RECENT_DONE)
    ).all()
    rows = [*active, *failed, *done]
    batch_ids = sorted({b for j in rows if (b := batch_of(j))})
    in_batches: dict[str, list[Job]] = {b: [] for b in batch_ids}
    if batch_ids:
        for j in db.scalars(
            scope(select(Job).where(Job.payload["batch"].as_string().in_(batch_ids)), user)
            .order_by(Job.id)
        ).all():  # fmt: skip
            in_batches[batch_of(j)].append(j)
    every = {j.id: j for j in rows} | {j.id: j for js in in_batches.values() for j in js}

    work_ids = {j.work_id for j in every.values() if j.work_id is not None}
    titles = dict(
        db.execute(select(Work.id, Work.title).where(Work.id.in_(work_ids), visible_works(user)))
        .all()
    ) if work_ids else {}  # fmt: skip
    workers = dict(db.execute(select(WorkerCredential.id, WorkerCredential.label)).all())
    blocked = {f["feature"]: f["blocked"] for f in limits.GOVERNOR.snapshot()["features"]}
    paused_queue = queue.queue_paused(db)

    # Place in line per capability, in claim order (the order ``active`` is in).
    ahead: dict[int, int] = {}
    seen: dict[str, int] = {}
    for j in active:
        if group_of(j) == "waiting":
            ahead[j.id] = seen.get(j.capability, 0)
            seen[j.capability] = ahead[j.id] + 1
    explained = {j.id for j in active if j.id in ahead and ahead[j.id] < EXPLAINED}
    now = utcnow()

    def one(j: Job) -> dict:
        group = group_of(j)
        payload, result = j.payload or {}, j.result or {}
        feature = limits.feature_of(j.kind, payload)
        limit = blocked.get(feature) if group == "waiting" and feature else None
        title = titles.get(j.work_id)
        show_error = shows_error(user, title is not None)
        summary = None
        if group == "running":
            holder = workers.get(j.leased_by_id) if j.leased_by_id is not None else None
            summary = f"Running on {holder or 'this server'}"
        elif group == "paused":
            summary = "Paused"
        elif group == "waiting":
            if paused_queue:
                summary = "Queue paused"
            elif limit in LIMIT_REACHED:
                summary = "Waiting: limit reached"
            elif j.id in explained:
                wait = queue.explain_wait(db, j, server_caps=server_caps, for_owner=owner,
                                          show_error=show_error, now=now)  # fmt: skip
                summary = wait["summary"] if wait else None
            else:
                summary = f"Waiting ({ahead[j.id]} ahead)"
        return {
            "id": j.id,
            "kind": j.kind,
            "detail": _detail(j),
            "capability": j.capability,
            "work_id": j.work_id,
            "title": title,
            "status": j.status.value,
            "group": group,
            "priority": j.priority,
            "runs_on": (
                (workers.get(j.leased_by_id) or "a worker")
                if j.leased_by_id is not None
                else "this server"
            )
            if group == "running"
            else None,
            "progress": _progress(result) if group == "running" else None,
            "waiting": summary,
            "limit": limit if limit in LIMIT_REACHED else None,
            "feature": feature,
            "ahead": ahead.get(j.id),
            "attempts": j.attempts,
            "max_attempts": j.max_attempts,
            "error": j.error if show_error else None,
            "batch": batch_of(j),
            "mine": j.created_by == user.id,
            "created_at": _iso(j.created_at),
            "started_at": _iso(j.started_at),
            "finished_at": _iso(j.finished_at),
        }

    jobs = [one(j) for j in rows if batch_of(j) is None]
    batches = []
    for b in batch_ids:
        members = [one(j) for j in in_batches[b]]
        if not members:
            continue
        counts = {g: sum(1 for m in members if m["group"] == g) for g in GROUPS}
        batches.append({
            "id": b,
            "kind": members[0]["kind"],
            "group": next((g for g in GROUPS if counts[g]), "done"),
            "counts": counts,
            "total": len(members),
            "created_at": members[0]["created_at"],
            "mine": all(m["mine"] for m in members),
            "jobs": members,
        })  # fmt: skip
    return {
        "queue_paused": paused_queue,
        "can_manage": owner,
        "jobs": jobs,
        "batches": batches,
    }


def _detail(job: Job) -> str | None:
    payload = job.payload or {}
    if job.kind == "tts":
        return payload.get("engine")
    if job.kind == "pdf2epub":
        return payload.get("mode") or "convert"
    if job.kind == "align":
        return payload.get("model")
    return None


def _progress(result: dict) -> float | None:
    p = result.get("progress")
    if isinstance(p, int | float):
        return max(0.0, min(1.0, float(p)))
    done, total = result.get("done"), result.get("total")
    if isinstance(done, int) and isinstance(total, int) and total > 0:
        return min(1.0, done / total)
    return None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None


# --- actions (callers check permissions and commit) ---------------------------------------------


def cancel(db: Session, job: Job) -> bool:
    """Cancel a live job of any kind; ``False`` when it is no longer live."""
    from lectern.align import jobs as align_jobs
    from lectern.tts import jobs as tts_jobs

    if job.kind == tts_jobs.KIND:
        return tts_jobs.cancel(db, job)
    return align_jobs.cancel_job(db, job)


def drop_partial(data_dir, job: Job) -> None:
    """After a cancel commits: remove what the job uploaded so far, unless its kind keeps
    artifacts for a Retry to resume from (narration)."""
    found = queue.handler(job.kind)
    if found is None or not found.keeps_artifacts:
        shutil.rmtree(queue.artifacts_dir(data_dir, job.id), ignore_errors=True)


def _update(db: Session, job: Job, code: str, *where, **values) -> None:
    """A conditional ``UPDATE`` of the row, not this session's copy: a claim, finish, or
    cancel that committed meanwhile wins, and the caller gets ``409 code``."""
    done = db.execute(
        update(Job)
        .where(Job.id == job.id, *where)
        .values(updated_at=utcnow(), **values)
        .execution_options(synchronize_session=False)
    ).rowcount
    if done != 1:
        raise Refused(409, code)
    db.expire(job)


def pause(db: Session, job: Job) -> None:
    _update(db, job, "not_waiting", Job.status == JobStatus.QUEUED, run_after=queue.PAUSED_UNTIL)


def resume(db: Session, job: Job) -> None:
    _update(db, job, "not_paused", Job.status == JobStatus.QUEUED,
            Job.run_after >= queue.PAUSED_UNTIL, run_after=utcnow())  # fmt: skip


def move(db: Session, job: Job, to: str) -> None:
    """``top``: ahead of every other queued job; ``bottom``: behind them all."""
    others = select(Job.priority).where(Job.status == JobStatus.QUEUED, Job.id != job.id)
    priority = job.priority
    if to == "top":
        lo = db.scalar(others.order_by(Job.priority).limit(1))
        if lo is not None:
            priority = min(priority, lo - 1)
    else:
        hi = db.scalar(others.order_by(Job.priority.desc()).limit(1))
        if hi is not None:
            priority = max(priority, hi + 1)
    _update(db, job, "not_waiting", Job.status == JobStatus.QUEUED, priority=priority)


def retry(db: Session, job: Job) -> None:
    """Queue a failed job again, in place: same payload and priority, attempts reset."""
    from sqlalchemy.exc import IntegrityError

    from lectern.align import jobs as align_jobs
    from lectern.models import AlignmentStatus

    if job.status != JobStatus.FAILED:
        raise Refused(409, "not_failed")
    if job.work_id is not None:
        work = db.get(Work, job.work_id)
        if work is None or work.trashed_at is not None:
            raise Refused(409, "work_gone")
        live = exists().where(
            Job.work_id == job.work_id,
            Job.kind == job.kind,
            Job.status.in_(JOB_ACTIVE_STATUSES),
        )
        if db.scalar(select(live)):
            raise Refused(409, "already_running")
    # After running out of memory the book's own Retry picks a smaller model.
    if job.kind == align_jobs.KIND_ALIGN and align_jobs.out_of_memory(job.error):
        raise Refused(409, "retry_from_book")
    kind, work_id = job.kind, job.work_id
    try:
        _update(db, job, "not_failed", Job.status == JobStatus.FAILED,
                status=JobStatus.QUEUED, attempts=0, crashes=0, error=None, result=None,
                run_after=utcnow(), started_at=None, finished_at=None, leased_by_id=None,
                lease_token_hash=None, lease_expires_at=None, heartbeat_at=None)  # fmt: skip
    except IntegrityError:  # another live job of the work and kind appeared meanwhile
        db.rollback()
        raise Refused(409, "already_running") from None
    if kind == align_jobs.KIND_ALIGN:
        alignment = align_jobs.work_alignment(db, work_id)
        if alignment is not None and alignment.status in (
            AlignmentStatus.FAILED,
            AlignmentStatus.RUNNING,
        ):
            alignment.status = AlignmentStatus.PENDING
            alignment.error = None
    db.flush()
