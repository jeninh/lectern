"""OPDS 1.2 catalogue, authenticated with HTTP Basic + per-device app password."""

from __future__ import annotations

from datetime import UTC, datetime
from xml.sax.saxutils import escape

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from lectern import device_queue
from lectern.access import visible_works
from lectern.models import Work
from lectern.reading_lists import continue_items, up_next_ids
from lectern.routes.library import (
    PAGE_SIZE,
    download_response,
    resolve_cover,
    resolve_file,
    search_works,
)
from lectern.security.deps import DbDep, OpdsUser, SettingsDep
from lectern.security.downloads import Device
from lectern.security.sessions import client_ip
from lectern.workmeta import strip_controls

router = APIRouter(prefix="/opds")

NAV = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ = "application/atom+xml;profile=opds-catalog;kind=acquisition"
OPENSEARCH = "application/opensearchdescription+xml"
EPUB = "application/epub+zip"


def _iso(dt: datetime | None) -> str:
    return (dt or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _xml(body: str, media_type: str) -> Response:
    return Response('<?xml version="1.0" encoding="UTF-8"?>\n' + body, media_type=media_type)


def _text(value: str) -> str:
    """Escaped, and without control characters XML 1.0 forbids: rows written before edits
    were validated (or by a scanner reading odd tags) must not make the whole feed unparsable."""
    return escape(strip_controls(value))


def _entry(work: Work) -> str:
    links = []
    for f in work.files:
        links.append(
            f'<link rel="http://opds-spec.org/acquisition" href="/opds/files/{f.id}/download" '
            f'type="{escape(f.media_type)}" length="{f.size}"/>'
        )
    if work.has_cover:
        links.append(
            f'<link rel="http://opds-spec.org/image" href="/opds/works/{work.id}/cover.jpg" type="image/jpeg"/>'
        )
        links.append(
            f'<link rel="http://opds-spec.org/image/thumbnail" href="/opds/works/{work.id}/cover.jpg" type="image/jpeg"/>'
        )
    summary = (
        f'<summary type="text">{_text(work.description[:1000])}</summary>'
        if work.description
        else ""
    )
    lang = f"<dc:language>{_text(work.language)}</dc:language>" if work.language else ""
    isbn = f"<dc:identifier>urn:isbn:{_text(work.isbn)}</dc:identifier>" if work.isbn else ""
    pub = f"<dc:publisher>{_text(work.publisher)}</dc:publisher>" if work.publisher else ""
    return (
        "<entry>"
        f"<title>{_text(work.title)}</title>"
        f"<id>urn:lectern:work:{work.id}</id>"
        f"<updated>{_iso(work.updated_at)}</updated>"
        f"<author><name>{_text(work.author)}</name></author>"
        f"{lang}{isbn}{pub}{summary}" + "".join(links) + "</entry>"
    )


def _nav_entry(title: str, urn: str, href: str, content: str, rel: str = "subsection") -> str:
    return (
        f"<entry><title>{_text(title)}</title><id>urn:lectern:{urn}</id>"
        f'<updated>{_iso(None)}</updated><content type="text">{_text(content)}</content>'
        f'<link rel="{rel}" href="{href}" type="{ACQ}"/></entry>'
    )


def _books(n: int) -> str:
    return f"{n} book" if n == 1 else f"{n} books"


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
def root(request: Request, user: OpdsUser, db: DbDep) -> Response:
    """Navigation, e-ink first: the three personal lists (Continue, Up next, and this
    device's queue) before the whole library, each with its size, so the book you want is
    one tap away on a slow screen. The order never changes; empty lists say so."""
    pw = request.state.app_password
    n_continue = len(continue_items(db, user, PAGE_SIZE))
    n_next = len(up_next_ids(db, user))
    n_queue = device_queue.pending_count(db, pw.id, user)
    body = (
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">'
        "<id>urn:lectern:root</id><title>Lectern</title>"
        f"<updated>{_iso(None)}</updated>"
        "<author><name>Lectern</name></author>"
        f'<link rel="self" href="/opds" type="{NAV}"/>'
        f'<link rel="start" href="/opds" type="{NAV}"/>'
        f'<link rel="search" href="/opds/search.xml" type="{OPENSEARCH}"/>'
        f'<link rel="search" href="/opds/search?q={{searchTerms}}" type="{ACQ}"/>'
        + _nav_entry(
            "Continue reading",
            "continue",
            "/opds/continue",
            f"{_books(n_continue)} in progress, latest first"
            if n_continue
            else "Nothing in progress",
        )
        + _nav_entry(
            "Up next",
            "up-next",
            "/opds/up-next",
            f"{_books(n_next)} on your shelves, not started"
            if n_next
            else "Nothing on your shelves to start",
        )
        + _nav_entry(
            "Queued for this device",
            "queue",
            "/opds/queue",
            f"{_books(n_queue)} sent to {pw.label}" if n_queue else f"Nothing sent to {pw.label}",
        )
        + _nav_entry("All books", "all", "/opds/all", "Every work in the library")
        + _nav_entry(
            "Recently added",
            "recent",
            "/opds/recent",
            "Newest first",
            "http://opds-spec.org/sort/new",
        )
        + "</feed>"
    )
    return _xml(body, NAV)


def _acquisition(
    title: str,
    self_href: str,
    works: list[Work],
    page: int,
    total: int,
    extra_q: str = "",
    feed_id: str | None = None,
) -> Response:
    pages = max(1, -(-total // PAGE_SIZE))
    links = [
        f'<link rel="self" href="{_text(self_href)}" type="{ACQ}"/>',
        f'<link rel="start" href="/opds" type="{NAV}"/>',
        f'<link rel="up" href="/opds" type="{NAV}"/>',
        f'<link rel="search" href="/opds/search.xml" type="{OPENSEARCH}"/>',
    ]
    base = self_href.split("?")[0]
    if page < pages:
        links.append(
            f'<link rel="next" href="{escape(base)}?page={page + 1}{escape(extra_q)}" type="{ACQ}"/>'
        )
    if page > 1:
        links.append(
            f'<link rel="previous" href="{escape(base)}?page={page - 1}{escape(extra_q)}" type="{ACQ}"/>'
        )
    body = (
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/" '
        'xmlns:opds="http://opds-spec.org/2010/catalog" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        f"<id>{escape(feed_id or f'urn:lectern:{base}')}</id><title>{_text(title)}</title>"
        f"<updated>{_iso(None)}</updated><author><name>Lectern</name></author>"
        f"<opensearch:totalResults>{total}</opensearch:totalResults>"
        f"<opensearch:itemsPerPage>{PAGE_SIZE}</opensearch:itemsPerPage>"
        f"<opensearch:startIndex>{(page - 1) * PAGE_SIZE + 1}</opensearch:startIndex>"
        + "".join(links)
        + "".join(_entry(w) for w in works)
        + "</feed>"
    )
    return _xml(body, ACQ)


def _works_page(db, ids: list[int], page: int) -> list[Work]:
    """The works of one page of ``ids``, in the order of ``ids``."""
    chunk = ids[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    rows = db.scalars(
        select(Work).options(selectinload(Work.files)).where(Work.id.in_(chunk))
    ).all()
    by_id = {w.id: w for w in rows}
    return [by_id[i] for i in chunk if i in by_id]


@router.get("/continue", include_in_schema=False)
def continue_feed(user: OpdsUser, db: DbDep, page: int = 1) -> Response:
    """Works in progress, most recent activity first (lectern.reading_lists)."""
    page = max(1, page)
    ids = [w for w, _, _ in continue_items(db, user, PAGE_SIZE)]
    works = _works_page(db, ids, page)
    return _acquisition("Continue reading", f"/opds/continue?page={page}", works, page, len(ids))


@router.get("/up-next", include_in_schema=False)
def up_next_feed(user: OpdsUser, db: DbDep, page: int = 1) -> Response:
    """Unstarted works on the person's shelves, in shelf order (lectern.reading_lists)."""
    page = max(1, page)
    ids = up_next_ids(db, user)
    return _acquisition(
        "Up next", f"/opds/up-next?page={page}", _works_page(db, ids, page), page, len(ids)
    )


@router.get("/queue", include_in_schema=False)
def queue_feed(request: Request, user: OpdsUser, db: DbDep, page: int = 1) -> Response:
    """This credential's queue, oldest first. Only the device whose app password signed the
    request sees it; an entry leaves once this device downloaded the whole EPUB."""
    page = max(1, page)
    pw = request.state.app_password
    total = device_queue.pending_count(db, pw.id, user)
    rows = db.execute(
        device_queue.pending_query(pw.id, user)
        .options(selectinload(Work.files))
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()
    return _acquisition(
        "Queued for this device",
        f"/opds/queue?page={page}",
        [w for _, w in rows],
        page,
        total,
        feed_id=f"urn:lectern:queue:{pw.id}",
    )


@router.get("/all", include_in_schema=False)
def all_books(user: OpdsUser, db: DbDep, page: int = 1) -> Response:
    page = max(1, page)
    works, total = search_works(db, user, None, page)
    return _acquisition("All books", f"/opds/all?page={page}", works, page, total)


@router.get("/recent", include_in_schema=False)
def recent(user: OpdsUser, db: DbDep, page: int = 1) -> Response:
    page = max(1, page)
    total = db.scalar(select(func.count()).select_from(Work).where(visible_works(user))) or 0
    works = db.scalars(
        select(Work)
        .options(selectinload(Work.files))
        .where(visible_works(user))
        .order_by(Work.created_at.desc(), Work.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()
    return _acquisition("Recently added", f"/opds/recent?page={page}", works, page, total)


@router.get("/search.xml", include_in_schema=False)
def opensearch(user: OpdsUser) -> Response:
    body = (
        '<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">'
        "<ShortName>Lectern</ShortName><Description>Search the Lectern library</Description>"
        "<InputEncoding>UTF-8</InputEncoding><OutputEncoding>UTF-8</OutputEncoding>"
        f'<Url type="{ACQ}" template="/opds/search?q={{searchTerms}}"/>'
        "</OpenSearchDescription>"
    )
    return _xml(body, OPENSEARCH)


@router.get("/search", include_in_schema=False)
def search(user: OpdsUser, db: DbDep, q: str = "", page: int = 1) -> Response:
    from urllib.parse import quote

    page = max(1, page)
    q = q.strip()[:200]
    works, total = search_works(db, user, q or None, page)
    return _acquisition(
        f"Search: {q}",
        f"/opds/search?page={page}&q={quote(q)}",
        works,
        page,
        total,
        f"&q={quote(q)}",
    )


@router.get("/works/{work_id}/cover.jpg", include_in_schema=False)
def cover(user: OpdsUser, db: DbDep, settings: SettingsDep, work_id: int):
    return FileResponse(resolve_cover(db, settings, work_id, user), media_type="image/jpeg")


@router.get("/files/{file_id}/download", include_in_schema=False)
def download(request: Request, user: OpdsUser, db: DbDep, settings: SettingsDep, file_id: int):
    book, path = resolve_file(db, settings, file_id, user)
    device = Device.app_password(request.state.app_password)
    return download_response(
        request, db, settings, book, path, user, device, client_ip(request), "opds"
    )
