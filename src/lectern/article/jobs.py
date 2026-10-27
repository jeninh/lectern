"""The ``article`` job: fetch a web page, build an EPUB of its article, file it as a new work.

Queued by ``POST /api/articles`` (routes/api/articles.py) with the payload ``{"url"}`` and the
requester in ``created_by``; there is no work yet, so ``work_id`` is NULL. Like ``pdf2epub`` it
runs on the server's own job runner (lectern.jobs.run_pending) and is never offered to external
workers: it needs the internet the server is allowed to reach, nothing a worker has.

Bounded for a Raspberry Pi: the page at most :data:`MAX_HTML_BYTES`, at most
:data:`MAX_IMAGES` images of at most :data:`MAX_IMAGE_BYTES` each and :data:`MAX_TOTAL_BYTES`
together, and :data:`TIME_LIMIT_S` for everything; images that do not fit are left out and
counted. One attempt: a failed save says why and the person saves again.

The EPUB is filed the way an upload is (lectern.imports), as a new work by the requester, with
no review: they asked for this one page. The same article saved again builds the same file, so
it is refused as ``already_saved`` with the work that has it.

While offline mode is on (lectern.egress) nothing is queued (the route refuses) and a job
queued before fails as ``offline`` without touching the network.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lectern import egress, queue
from lectern.article import ARTICLE, build, fetch
from lectern.config import Settings
from lectern.models import JOB_ACTIVE_STATUSES, Job, JobStatus, Role, User, utcnow

log = logging.getLogger("lectern.article")

MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES = 40
MAX_TOTAL_BYTES = 40 * 1024 * 1024
TIME_LIMIT_S = 120.0
IMAGE_TIME_S = 20.0  # one image's share of the deadline at most
MAX_ACTIVE_PER_USER = 10
HTML_TYPES = ("text/html", "application/xhtml+xml")
ACCEPT_HTML = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"
ACCEPT_IMAGE = "image/jpeg,image/png,image/webp,image/gif;q=0.9,*/*;q=0.1"


class SaveError(Exception):
    def __init__(self, code: str, **result) -> None:
        super().__init__(code)
        self.code = code
        self.result = result


def unavailable() -> str | None:
    """Why articles cannot be saved right now (``offline``), or None."""
    return "offline" if egress.offline() else None


def active_count(db: Session, user_id: int) -> int:
    return db.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.kind == ARTICLE, Job.created_by == user_id, Job.status.in_(JOB_ACTIVE_STATUSES))
    )


def enqueue(db: Session, user: User, url: str) -> Job:
    """Queue a save of ``url`` (checked with :func:`lectern.article.fetch.check_url`)."""
    job = Job(
        kind=ARTICLE,
        work_id=None,
        status=JobStatus.QUEUED,
        priority=25,  # after narration (20), before PDF conversions (30)
        max_attempts=1,
        payload={"url": url},
        created_by=user.id,
    )
    db.add(job)
    db.flush()
    return job


def recent(db: Session, user_id: int, limit: int = 20) -> list[Job]:
    return list(
        db.scalars(
            select(Job)
            .where(Job.kind == ARTICLE, Job.created_by == user_id)
            .order_by(Job.id.desc())
            .limit(limit)
        )
    )


def job_json(job: Job) -> dict:
    result = job.result or {}
    return {
        "id": job.id,
        "url": (job.payload or {}).get("url"),
        "status": job.status.value,
        "error": job.error,
        "title": result.get("title"),
        "work_id": result.get("work_id"),
        "images": result.get("images"),
        "skipped_images": result.get("skipped_images"),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _images(article: build.Article, deadline: float, get) -> tuple[dict, int]:
    """Download and normalise the lead image and the article's images within the caps.
    Returns ``({url: (bytes, media type)}, skipped)``."""
    wanted = list(dict.fromkeys([u for u in [article.lead_image, *article.image_urls] if u]))
    got: dict[str, tuple[bytes, str]] = {}
    total = 0
    for url in wanted:
        room = MAX_TOTAL_BYTES - total
        if len(got) >= MAX_IMAGES or room <= 0 or deadline - time.monotonic() < 5:
            continue
        try:
            resp = get(
                url,
                max_bytes=min(MAX_IMAGE_BYTES, room),
                accept=ACCEPT_IMAGE,
                deadline=min(deadline, time.monotonic() + IMAGE_TIME_S),
            )
        except fetch.FetchError as exc:
            if exc.code == "offline":
                raise
            continue
        total += len(resp.data)
        image = build.normalise_image(resp.data)
        if image is not None:
            got[url] = image
    # Only the article's own images count as left out; a missing cover just means none.
    return got, sum(1 for u in article.image_urls if u not in got)


def run(db: Session, settings: Settings, job: Job, get=None) -> None:
    """Run one claimed ``article`` job. ``get``: the fetcher (default
    :func:`lectern.article.fetch.get`)."""
    get = get or fetch.get
    payload = job.payload or {}
    try:
        if egress.offline():
            raise SaveError("offline")
        user = db.get(User, job.created_by or 0)
        if user is None or not user.is_active or user.role not in (Role.OWNER, Role.MEMBER):
            raise SaveError("not_allowed")
        url = fetch.check_url(str(payload.get("url") or ""))
        deadline = time.monotonic() + TIME_LIMIT_S
        page = get(url, max_bytes=MAX_HTML_BYTES, accept=ACCEPT_HTML, deadline=deadline)
        if page.media_type not in HTML_TYPES:
            raise SaveError("not_html")
        article = build.extract(page.data, page.url, page.charset)
        images, skipped = _images(article, deadline, get)
        scratch = settings.data_dir / "tmp" / f"article-{job.id}.epub"
        try:
            built = build.build_epub(article, images, scratch)
            work_id = _file(db, settings, job, user, article, scratch)
        finally:
            scratch.unlink(missing_ok=True)
    except (SaveError, fetch.FetchError, build.ArticleError) as exc:
        _fail(db, job, exc.code, getattr(exc, "result", None))
        log.info("article job %s failed: %s", job.id, exc.code)
        return
    except Exception as exc:  # noqa: BLE001 - recorded on the job; never the page's content
        log.warning("article job %s failed: %s", job.id, type(exc).__name__)
        _fail(db, job, "build_failed", None)
        return
    job = db.get(Job, job.id)
    if job.status == JobStatus.LEASED:
        job.status = JobStatus.SUCCEEDED
        job.result = {
            "work_id": work_id,
            "title": article.title,
            "images": built["images"],
            "skipped_images": skipped,
        }
        job.finished_at = utcnow()
        db.commit()
    log.info("article job %s: saved as work %s", job.id, work_id)


def _fail(db: Session, job: Job, code: str, result: dict | None) -> None:
    db.rollback()
    job = db.get(Job, job.id)
    if job is None or job.status != JobStatus.LEASED:
        return
    job.status = JobStatus.FAILED
    job.error = code
    job.result = result or None
    job.finished_at = utcnow()
    db.commit()


def _file(db: Session, settings: Settings, job: Job, user: User, article, path) -> int:
    """File the built EPUB as a new work of ``user`` through an import (the same staging,
    duplicate check, metadata, and filing as an upload), in one transaction."""
    from lectern import imports
    from lectern.editing import Journal
    from lectern.fsops import LIBRARY_LOCK
    from lectern.ingest import formats
    from lectern.ingest.hashing import sha256_file
    from lectern.ingest.library import safe_component
    from lectern.models import ImportStatus
    from lectern.security import audit

    size, sha256 = path.stat().st_size, sha256_file(path)
    host = urlsplit(article.url).hostname or "article"
    filename = f"{safe_component(article.title, host)[:80]}.epub"
    journal = Journal()
    imp = None
    with LIBRARY_LOCK:
        try:
            imp, _ = imports.create(db, user, article.title, None)
            staged = imports.stage_file(
                db, settings, imp, path, filename=filename, media_type=formats.EPUB,
                size=size, sha256=sha256,
            )  # fmt: skip
            imports.complete(db, settings, user, imp)
            if imp.status == ImportStatus.FAILED:  # every file a duplicate
                raise SaveError("already_saved", work_id=staged.duplicate_work_id)
            decision = imports.Decision(target=imports.Target(action="new"))
            work, _report = imports.file(db, settings, user, imp, decision, journal)
            audit.record(
                db,
                "article.save",
                actor_user_id=user.id,
                target=work.title[:256],
                detail=f"work:{work.id}; job {job.id}",
            )
            db.commit()
        except BaseException:
            db.rollback()
            journal.rollback()
            raise
        finally:
            if imp is not None:
                imports.cleanup_staging(settings, imp)
    return work.id


# The in-process runner claims this kind; never offered to external workers.
queue.register(queue.KindHandler(kind=ARTICLE, capability=lambda _payload: ARTICLE, remote=False))
