"""PDF → EPUB conversion of a work (issue #28), as a job (docs/pdf-epub.md).

- ``GET    /api/works/{id}/pdf-epub``  eligibility, the latest job, the converted edition, the report
- ``POST   /api/works/{id}/pdf-epub/analyse``  queue an analysis (structure, confidence, previews)
- ``GET    /api/works/{id}/pdf-epub/analysis``  the review sheet: detected rules and evidence
- ``POST   /api/works/{id}/pdf-epub/preview``  converted text of some pages under given rules
- ``GET    /api/works/{id}/pdf-epub/preview/{page}.jpg``  a sample page of the PDF, rendered
- ``POST   /api/works/{id}/pdf-epub``  queue a conversion (optionally with confirmed rules, or
  a re-conversion of an existing edition; a scan once OCR has given it text; again, replacing
  the edition, when it is ``outdated``)
- ``POST   /api/works/{id}/pdf-epub/retry``  queue the last failed one again
- ``DELETE /api/works/{id}/pdf-epub``  remove the converted edition (the PDF stays)
- ``POST   /api/works/pdf-epub``  queue conversions for many works (the selection bar, #61)
- ``GET    /api/pdf-epub/jobs?ids=``  the state of those jobs, for per-book progress

Job management (list, cancel) is the shared ``/api/jobs`` (``routes/api/align.py``).
"""

from __future__ import annotations

import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from lectern.access import visible_works
from lectern.ingest import formats
from lectern.models import BookFile, Job, JobStatus, Role, Work
from lectern.pdf2epub import build
from lectern.pdf2epub import jobs as pj
from lectern.pdf2epub.rules import Rules, defaults
from lectern.pdftext import store as pdf_store
from lectern.routes.api.readalong import visible_work
from lectern.routes.api.selection import Selection
from lectern.routes.api.selection import resolve as resolve_selection
from lectern.security import audit
from lectern.security.deps import (
    AuthContext,
    DbDep,
    SettingsDep,
    require_role,
    require_role_read,
)
from lectern.security.sessions import client_ip

router = APIRouter()

Manager = Annotated[AuthContext, Depends(require_role(Role.OWNER, Role.MEMBER))]
Viewer = Annotated[AuthContext, Depends(require_role_read(Role.OWNER, Role.MEMBER))]


def _wake(request: Request) -> None:
    runner = getattr(request.app.state, "jobs", None)
    if runner is not None:
        runner.wake.set()


def _job_json(job) -> dict | None:
    if job is None:
        return None
    result = job.result or {}
    payload = job.payload or {}
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status.value,
        "error": job.error,
        "mode": payload.get("mode") or "convert",
        "pdf_sha256": payload.get("pdf_sha256"),
        "sections": result.get("report", {}).get("sections") if result else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


@router.get("/works/{work_id}/pdf-epub")
def state(auth: Viewer, db: DbDep, settings: SettingsDep, work_id: int) -> dict:
    work = visible_work(db, auth.user, work_id)
    reason = pj.eligibility(db, settings, work)
    job = pj.latest_job(db, work_id)
    edition = pj.converted_file(db, work.id)
    report = _report(settings, work) if edition else None
    if report is None and edition is not None:  # the edition's own report, not a later attempt's
        made = pj.edition_job(db, edition)
        report = (made.result or {}).get("report") if made else None
    if report is None and job is not None:
        report = (job.result or {}).get("report")
    pair = pdf_store.text_pdf(db, work.id)
    row = pair[1] if pair else None
    return {
        "work_id": work.id,
        "eligible": reason is None,
        "reason": reason,
        # where the text to convert comes from: the PDF's text layer, or OCR of a scan
        "source": ("ocr" if row.ocr_sha256 else "text_layer") if row else None,
        "outdated": edition is not None and pj.outdated(db, work.id, edition),
        "job": _job_json(job),
        "edition": (
            {
                "file": edition.rel_path,
                "size": edition.size,
                "sha256": edition.sha256,
                "added_at": edition.added_at.isoformat(),
            }
            if edition
            else None
        ),
        "report": report,
        "analysed": _analysis(db, settings, work) is not None,
    }


def _analysing(job) -> bool:
    """Whether a pdf2epub job is an analysis (the review step), not a conversion."""
    return (job.payload or {}).get("mode") == "analyse"


def _report(settings, work) -> dict | None:
    import json

    path = settings.library_dir / work.dir_path / "derived" / build.REPORT_NAME
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def _analysis(db, settings, work) -> tuple[dict, dict] | None:
    """(text document, structure) while the stored analysis is of the current text."""
    pair = pdf_store.text_pdf(db, work.id)
    if pair is None:
        return None
    book, row = pair
    st = pj.load_structure(settings, work, row.text_sha256)
    if st is None:
        return None
    doc = pdf_store.load_document(settings.library_dir, work, book, row)
    if doc is None or doc.get("status") != "ready":
        return None
    return doc, st


def _need_analysis(db, settings, work) -> tuple[dict, dict]:
    found = _analysis(db, settings, work)
    if found is None:
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": "not_analysed"})
    return found


def _ranges(labels: list[str]) -> list[dict]:
    """Runs of page labels in one numbering scheme: "i–xii, then 1–340"."""

    def scheme(lb: str) -> str:
        return (
            "arabic"
            if lb.isdigit()
            else "roman"
            if re.fullmatch(r"[ivxlcdm]+", lb, re.I)
            else "other"
        )

    out: list[dict] = []
    for i, lb in enumerate(labels):
        kind = scheme(lb)
        prev = out[-1] if out else None
        follows = (
            prev is not None
            and prev["scheme"] == kind
            and (kind != "arabic" or int(lb) == int(prev["last"]) + 1)
        )
        if follows:
            prev["last"], prev["pages"][1] = lb, i + 1
        else:
            out.append({"scheme": kind, "first": lb, "last": lb, "pages": [i + 1, i + 1]})
    return out[:50]


def analysis_json(doc: dict, st: dict, rules: Rules, default: Rules) -> dict:
    words = None
    headings: dict[float, dict] = {}
    sections = doc.get("sections") or []
    for key, size in (st.get("headings") or {}).items():
        s, b = (int(v) for v in key.split(":"))
        sec = sections[s]
        blocks = sec["blocks"]
        start = int(blocks[b][0])
        end = int(blocks[b + 1][0]) if b + 1 < len(blocks) else len(sec["text"])
        h = headings.setdefault(float(size), {"size": float(size), "count": 0, "example": ""})
        h["count"] += 1
        h["example"] = h["example"] or sec["text"][start:end].strip()[:80]
    ordered = sorted(headings.values(), key=lambda h: -h["size"])
    for k, h in enumerate(ordered):
        h["level"] = 2 + min(2, k)
    notes = st.get("notes") or []
    if notes:
        from lectern.pdf2epub.structure import doc_words

        words = doc_words(doc)
    return {
        "summary": pj.summary(st),
        "body_size": st.get("body_size"),
        "page_numbers": {
            "source": "page_labels" if any(p.get("label") for p in doc["pages"]) else "physical",
            "ranges": _ranges(build.page_labels(doc)),
        },
        "running_heads_removed": (doc.get("stats") or {}).get("margin_lines_removed", 0),
        "columns_pages": (doc.get("stats") or {}).get("columns", 0),
        "chapters": [
            {
                "index": i,
                "title": s.get("title") or f"Section {i + 1}",
                "page_start": int(s["page_start"]) + 1,
                "page_end": int(s["page_end"]) + 1,
                "from_outline": bool(s.get("from_outline")),
            }
            for i, s in enumerate(sections)
        ],
        "headings": ordered,
        "footnotes": [
            {
                "page": n["page"] + 1,
                "marker": n["marker"],
                "text": " ".join(words[g].text for g in n["words"][1:])[:160],
            }
            for n in notes[:30]
        ]
        if words
        else [],
        "zones": [
            {
                "id": z["id"],
                "page": z["page"] + 1,
                "kind": z["kind"],
                "complex": bool(z.get("complex")),
                "photo": bool(z.get("photo")),
            }
            for z in (st.get("zones") or [])[:500]
        ],
        "matter": st.get("matter") or [],
        "pages": st.get("pages") or [],
        "preview_pages": pj.sample_pages(st),
        "rules": rules.model_dump(),
        "defaults": default.model_dump(),
    }


class ConvertIn(BaseModel):
    rules: Rules | None = None
    reconvert: bool = Field(False, description="Replace the existing converted edition.")


class PreviewIn(BaseModel):
    rules: Rules | None = None
    pages: list[Annotated[int, Field(ge=1, le=10_000)]] = Field(min_length=1, max_length=12)


@router.post("/works/{work_id}/pdf-epub/analyse", status_code=status.HTTP_202_ACCEPTED)
def analyse(
    request: Request, auth: Manager, db: DbDep, settings: SettingsDep, work_id: int
) -> dict:
    work = visible_work(db, auth.user, work_id)
    reason = pj.eligibility(db, settings, work)
    if reason is not None and reason != "converted":
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": reason})
    if pj.active_job(db, work.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": "already_running"})
    job = pj.enqueue(db, settings, work, auth.user.id, mode="analyse")
    audit.record(db, "pdf2epub.analyse", actor_user_id=auth.user.id, target=f"work:{work.id}",
                 ip=client_ip(request), detail=f"job {job.id}")  # fmt: skip
    db.commit()
    _wake(request)
    return {"status": job.status.value, "job_id": job.id}


@router.get("/works/{work_id}/pdf-epub/analysis")
def analysis(auth: Viewer, db: DbDep, settings: SettingsDep, work_id: int) -> dict:
    work = visible_work(db, auth.user, work_id)
    doc, st = _need_analysis(db, settings, work)
    default = defaults(st)
    return analysis_json(doc, st, pj.saved_rules(settings, work) or default, default)


@router.post("/works/{work_id}/pdf-epub/preview")
def preview(auth: Viewer, db: DbDep, settings: SettingsDep, work_id: int, body: PreviewIn) -> dict:
    work = visible_work(db, auth.user, work_id)
    doc, st = _need_analysis(db, settings, work)
    rules = body.rules or pj.saved_rules(settings, work) or defaults(st)
    pages = build.preview(doc, st, rules, sorted(set(body.pages)))
    have = settings.library_dir / work.dir_path / "derived" / pj.PREVIEW_DIR
    for p in pages:
        p["image"] = (have / f"page-{p['page']}.jpg").is_file()
    return {"pages": pages}


@router.get("/works/{work_id}/pdf-epub/preview/{page}.jpg")
def preview_image(
    auth: Viewer, db: DbDep, settings: SettingsDep, work_id: int, page: int
) -> Response:
    work = visible_work(db, auth.user, work_id)
    _need_analysis(db, settings, work)  # renders of an older text (or PDF) are not served
    path = settings.library_dir / work.dir_path / "derived" / pj.PREVIEW_DIR / f"page-{page}.jpg"
    if not 1 <= page <= 10_000 or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"detail": "no_preview"})
    return Response(path.read_bytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "private, no-cache",
                             "X-Content-Type-Options": "nosniff"})  # fmt: skip


@router.post("/works/{work_id}/pdf-epub", status_code=status.HTTP_202_ACCEPTED)
def convert(
    request: Request,
    auth: Manager,
    db: DbDep,
    settings: SettingsDep,
    work_id: int,
    body: ConvertIn | None = None,
) -> dict:
    body = body or ConvertIn()
    work = visible_work(db, auth.user, work_id)
    reason = pj.eligibility(db, settings, work)
    if reason == "converted" and not body.reconvert:
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": "converted"})
    if reason is not None and reason != "converted":
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": reason})
    active = pj.active_job(db, work.id)
    # A running analysis must finish first; a running conversion is returned as it is, unless
    # new rules came with this request (they would be silently ignored).
    if active is not None and (_analysing(active) or body.rules is not None):
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": "already_running"})
    job = pj.enqueue(db, settings, work, auth.user.id, rules=body.rules)
    audit.record(db, "pdf2epub.request", actor_user_id=auth.user.id, target=f"work:{work.id}",
                 ip=client_ip(request), detail=f"job {job.id}")  # fmt: skip
    db.commit()
    _wake(request)
    return {"status": job.status.value, "job_id": job.id}


@router.post("/works/{work_id}/pdf-epub/retry", status_code=status.HTTP_202_ACCEPTED)
def retry(request: Request, auth: Manager, db: DbDep, settings: SettingsDep, work_id: int) -> dict:
    work = visible_work(db, auth.user, work_id)
    if pj.active_job(db, work.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": "already_running"})
    last = pj.latest_job(db, work.id)
    if last is None or last.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": "nothing_to_retry"})
    reason = pj.eligibility(db, settings, work)
    if reason is not None and reason != "converted":
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": reason})
    payload = last.payload or {}  # the same run again: an analysis, or a conversion's rules
    rules = Rules.model_validate(payload["rules"]) if payload.get("rules") else None
    job = pj.enqueue(db, settings, work, auth.user.id, mode=payload.get("mode") or "convert",
                     rules=rules)  # fmt: skip
    audit.record(db, "pdf2epub.retry", actor_user_id=auth.user.id, target=f"work:{work.id}",
                 ip=client_ip(request), detail=f"job {job.id}")  # fmt: skip
    db.commit()
    _wake(request)
    return {"status": job.status.value, "job_id": job.id}


@router.delete("/works/{work_id}/pdf-epub")
def remove_edition(
    request: Request, auth: Manager, db: DbDep, settings: SettingsDep, work_id: int
) -> dict:
    work = visible_work(db, auth.user, work_id)
    if pj.active_job(db, work.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": "already_running"})
    if not pj.remove(db, settings, work):
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"detail": "no_converted_edition"})
    audit.record(db, "pdf2epub.delete", actor_user_id=auth.user.id, target=f"work:{work.id}",
                 ip=client_ip(request))  # fmt: skip
    db.commit()
    return {"removed": True}


# --- many works at once (issue #61) ----------------------------------------------------------------

log = logging.getLogger("lectern.pdf2epub")

# Each conversion runs in the server, one at a time (lectern.jobs.run_pending claims them
# serially), so a big selection queues work rather than running it all at once; the cap keeps
# a single request (and the queue it leaves behind) bounded on a Raspberry Pi.
CONVERT_MANY_MAX = 200
JOB_IDS_MAX = CONVERT_MANY_MAX


class ConvertManyIn(BaseModel):
    work_ids: list[int] = Field(default_factory=list, max_length=CONVERT_MANY_MAX)
    selection: Selection | None = None


def _skip_reason(db, settings, work: Work) -> str | None:
    """Why a selected work is not converted: it already has an EPUB edition (``already_epub``,
    an uploaded one, a Kindle book converted at ingest, or an earlier conversion), it has no
    PDF at all (``unsupported``: comics, audiobooks), or its PDF has no usable text yet
    (``no_text_pdf``: a scan that needs OCR, or text still being read)."""
    media = set(db.scalars(select(BookFile.media_type).where(BookFile.work_id == work.id)))
    if formats.EPUB in media:
        return "already_epub"
    if formats.PDF not in media:
        return "unsupported"
    return pj.eligibility(db, settings, work)


@router.post("/works/pdf-epub")
def convert_many(
    request: Request, auth: Manager, db: DbDep, settings: SettingsDep, body: ConvertManyIn
) -> dict:
    """Queue a PDF → EPUB conversion for every selected work that can have one. One result per
    work, in order, each its own transaction (a failure never stops the rest): ``ok`` with
    ``status`` ``queued`` or ``already_queued`` (and the ``job_id``), or not ``ok`` with
    ``status`` ``skipped`` or ``failed`` and an ``error`` (:func:`_skip_reason`, ``analysing``
    while the work's PDF is being analysed for review, ``not_found``, ``internal_error``); each with the work's ``title`` when it was found. At most
    ``CONVERT_MANY_MAX`` works per request."""
    if not request.app.state.limiters["bulk_jobs"].allow(f"user:{auth.user.id}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited")
    ids = resolve_selection(db, auth.user, body.work_ids, body.selection, limit=CONVERT_MANY_MAX)
    results, queued = [], []
    for wid in ids:
        item: dict = {"work_id": wid}
        try:
            work = visible_work(db, auth.user, wid)
            item["title"] = work.title
            if (job := pj.active_job(db, work.id)) is not None and _analysing(job):
                # an analysis (the review step) is no conversion: it would finish "succeeded"
                # with no edition
                item.update(ok=False, status="skipped", error="analysing")
            elif job is not None:
                item.update(ok=True, status="already_queued", job_id=job.id)
            elif (reason := _skip_reason(db, settings, work)) is not None:
                item.update(ok=False, status="skipped", error=reason)
            else:
                job = pj.enqueue(db, settings, work, auth.user.id)
                db.commit()
                queued.append(job.id)
                item.update(ok=True, status="queued", job_id=job.id)
        except HTTPException:
            db.rollback()
            item.update(ok=False, status="failed", error="not_found")
        except Exception:  # noqa: BLE001 - one work failing must not stop the rest
            log.exception("bulk pdf2epub: work %s failed", wid)
            db.rollback()
            item.update(ok=False, status="failed", error="internal_error")
        results.append(item)
    audit.record(db, "pdf2epub.bulk_request", actor_user_id=auth.user.id,
                 target=f"{len(queued)}/{len(ids)} works", ip=client_ip(request),
                 detail=("jobs " + ",".join(map(str, queued)))[:500] if queued else None)  # fmt: skip
    db.commit()
    if queued:
        _wake(request)
    return {"results": results}


@router.get("/pdf-epub/jobs")
def jobs_state(
    auth: Viewer, db: DbDep, ids: str = Query(default="", max_length=JOB_IDS_MAX * 12)
) -> dict:
    """Where each of these conversion jobs is (``ids``: comma-separated, at most
    ``JOB_IDS_MAX``): the selection bar's per-book progress. Jobs of works the caller may not
    see, and jobs of other kinds, are left out."""
    try:
        wanted = list(dict.fromkeys(int(x) for x in ids.split(",") if x.strip()))
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "bad_ids") from None
    if len(wanted) > JOB_IDS_MAX:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "too_many_ids")
    rows = db.scalars(
        select(Job)
        .join(Work, Work.id == Job.work_id)
        .where(Job.id.in_(wanted), Job.kind == build.PDF2EPUB, visible_works(auth.user))
    ).all()
    by_id = {j.id: j for j in rows if not _analysing(j)}  # conversions only
    return {
        "jobs": [{**_job_json(by_id[i]), "work_id": by_id[i].work_id} for i in wanted if i in by_id]
    }
