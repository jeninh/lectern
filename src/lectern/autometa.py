"""Automatic metadata suggestions across the library (the "Fix metadata" pass).

A person asks for a pass over a selection, or the whole library, from the Library. Each work
that is missing one of :data:`FIELDS` gets a ``metadata_suggestions`` row (``queued``); the
job runner thread works through them a few at a time with :func:`lectern.lookup.lookup`, so
its per-provider limit and 24 h cache hold for bulk runs exactly as for the editor. Results are
suggestions to review, field by field, like the editor's; nothing here writes to a work.

- Only fields that are empty *and* that nobody has changed in an edit (``work_revisions``)
  are looked up and suggested: a description someone deliberately cleared stays cleared.
- Lookups happen only while the owner has turned them on (``metadata_lookup_enabled``) and
  offline mode is off; otherwise queued work is closed as ``error`` with the reason and
  nothing leaves the server. Never at read time: only this pass and the editor's button.
- A title search must return the same book (title and author close, as ingest matching
  measures them) before its fields are suggested; an ISBN search is trusted.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from rapidfuzz import fuzz
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from lectern import egress, instance, lookup
from lectern.access import can_see_work
from lectern.ingest.epub import normalize_isbn
from lectern.ingest.matching import AUTHOR_THRESHOLD, author_key, clean_title, sort_title
from lectern.models import MetadataSuggestion, User, Work, WorkRevision, utcnow
from lectern.workmeta import authors_of, metadata_of

log = logging.getLogger("lectern.autometa")

FIELDS = ("description", "series", "series_index", "isbn", "tags")
PROVIDERS = ("openlibrary", "googlebooks")
PER_TICK = 3  # works looked up per runner tick (the provider limit still applies)
MAX_ATTEMPTS = 5  # tries for a work that keeps hitting a rate limit
TITLE_MATCH = 85
OPEN = ("queued",)  # waiting for the runner
REVIEW = ("ready",)  # waiting for a person


def unavailable(db: Session) -> str | None:
    """Why no lookup may go out now, or ``None``."""
    if not instance.get(db, "metadata_lookup_enabled"):
        return "lookup_disabled"
    if egress.offline():
        return "lookup_offline"
    return None


def _empty(v: object) -> bool:
    return v is None or v == "" or v == []


def edited_fields(db: Session, work_id: int) -> set[str]:
    """Fields of :data:`FIELDS` any saved edit (or undo) of this work changed."""
    out: set[str] = set()
    for before, after in db.execute(
        select(WorkRevision.before, WorkRevision.after).where(WorkRevision.work_id == work_id)
    ):
        b, a = (before or {}).get("metadata"), (after or {}).get("metadata")
        if isinstance(b, dict) and isinstance(a, dict):
            out |= {f for f in FIELDS if b.get(f) != a.get(f)}
    return out


def missing_fields(db: Session, work: Work) -> list[str]:
    meta = metadata_of(work)
    edited = edited_fields(db, work.id)
    return [f for f in FIELDS if _empty(meta[f]) and f not in edited]


def queue(db: Session, user: User, ids: list[int], *, library_pass: bool = False) -> list[dict]:
    """Queue a pass for each work; per-work result ``status``: ``queued``, ``already_queued``,
    ``nothing_missing``, ``not_found``, or (library pass only, which leaves reviewed works
    alone) ``waiting_review`` / ``dismissed``. The caller commits."""
    rows = {
        r.work_id: r
        for r in db.scalars(select(MetadataSuggestion).where(MetadataSuggestion.work_id.in_(ids)))
    }
    out = []
    for wid in ids:
        work = db.get(Work, wid)
        row = rows.get(wid)
        if work is None or work.trashed_at is not None or not can_see_work(db, user, wid):
            status = "not_found"
        elif row is not None and row.status in OPEN:
            status = "already_queued"
        elif library_pass and row is not None and row.status in ("ready", "dismissed"):
            status = "waiting_review" if row.status == "ready" else "dismissed"
        elif not missing_fields(db, work):
            status = "nothing_missing"
        else:
            status = "queued"
            if row is None:
                row = MetadataSuggestion(work_id=wid)
                db.add(row)
            row.status, row.fields, row.error, row.attempts = "queued", None, None, 0
            row.requested_by, row.resolved_at, row.created_at = user.id, None, utcnow()
        out.append({"work_id": wid, "status": status})
    db.flush()
    return out


def _query(work: Work) -> lookup.Query | None:
    first = next((a["name"] for a in authors_of(work) if a["name"] != "Unknown Author"), None)
    try:
        return lookup.Query(
            isbn=normalize_isbn(work.isbn) if work.isbn else None,
            title=work.title[:300],
            author=first[:200] if first else None,
            providers=list(PROVIDERS),
        )
    except ValueError:
        return None


def _same_book(work: Work, fields: dict) -> bool:
    title = fields.get("title")
    if not isinstance(title, str):
        return False
    ours = sort_title(clean_title(work.title))
    if fuzz.token_sort_ratio(ours, sort_title(clean_title(title))) < TITLE_MATCH:
        return False
    theirs = [a.get("name", "") for a in fields.get("authors") or []]
    mine = [a["name"] for a in authors_of(work) if a["name"] != "Unknown Author"]
    if not theirs or not mine:
        return True
    return fuzz.token_set_ratio(author_key(mine[0]), author_key(theirs[0])) >= AUTHOR_THRESHOLD


def pick(work: Work, missing: list[str], result: dict, by_isbn: bool) -> dict:
    """One suggestion per missing field: the first provider's top result that is this book
    and has a value, with its provenance. A series number only goes with its own series."""
    tops: dict[str, dict] = {}
    for s in result["suggestions"]:
        tops.setdefault(s["provider"], s)
    out: dict[str, dict] = {}
    for provider in PROVIDERS:
        s = tops.get(provider)
        if s is None or not (by_isbn or _same_book(work, s["fields"])):
            continue
        for f in missing:
            v = s["fields"].get(f)
            if f in out or _empty(v):
                continue
            if f == "series_index":
                series = (work.series or (out.get("series") or {}).get("value") or "").casefold()
                if series != (s["fields"].get("series") or "").casefold():
                    continue
            out[f] = {
                "value": v,
                "provider": provider,
                "provider_name": s["provider_name"],
                "source_url": s["source_url"],
            }
    return out


def run_one(db: Session, row: MetadataSuggestion, fetch: Callable[[str], bytes] | None) -> bool:
    """Look up one queued work. Returns False when a rate limit says to stop this tick."""
    work = db.get(Work, row.work_id)
    if work is None or work.trashed_at is not None:
        row.status, row.error = "error", "not_found"
        db.commit()
        return True
    missing = missing_fields(db, work)
    q = _query(work)
    if not missing or q is None:
        row.status, row.error = ("no_match", None) if q else ("error", "no_title")
        db.commit()
        return True
    row.attempts += 1
    db.commit()  # no transaction stays open while we wait on the network
    result = lookup.lookup(q, fetch)
    fields = pick(work, missing, result, by_isbn=bool(q.isbn))
    codes = [e["error"] for e in result["errors"]]
    if fields:
        row.status, row.fields, row.error = "ready", fields, None
    elif "rate_limited" in codes and row.attempts < MAX_ATTEMPTS:
        db.commit()  # stays queued; the limit window decides when it goes again
        return False
    elif codes and not result["suggestions"]:
        row.status, row.error = "error", "lookup_offline" if "offline" in codes else codes[0]
    else:
        row.status, row.error = "no_match", None
    db.commit()
    return True


def run_pending(session_factory: sessionmaker, fetch: Callable[[str], bytes] | None = None) -> int:
    """The runner's tick: up to :data:`PER_TICK` queued works. Returns how many were done."""
    done = 0
    with session_factory() as db:
        while done < PER_TICK:
            row = db.scalar(
                select(MetadataSuggestion)
                .where(MetadataSuggestion.status.in_(OPEN))
                .order_by(MetadataSuggestion.id)
                .limit(1)
            )
            if row is None:
                break
            if why := unavailable(db):
                # Switched off (or offline) since it was queued: close it, send nothing.
                db.execute(
                    update(MetadataSuggestion)
                    .where(MetadataSuggestion.status.in_(OPEN))
                    .values(status="error", error=why, updated_at=utcnow())
                )
                db.commit()
                break
            try:
                if not run_one(db, row, fetch):
                    break
            except Exception:  # noqa: BLE001 - one work failing must not stop the queue
                db.rollback()
                log.exception("metadata pass for work %s failed", row.work_id)
                db.execute(
                    update(MetadataSuggestion)
                    .where(MetadataSuggestion.id == row.id)
                    .values(status="error", error="internal_error")
                )
                db.commit()
            done += 1
    return done
