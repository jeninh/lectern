"""OPDS 1.2 catalogue, authenticated with HTTP Basic + per-device app password."""

from __future__ import annotations

from datetime import UTC, datetime
from xml.sax.saxutils import escape

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse

from lectern.access import visible_works
from lectern.models import Work
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


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
def root(user: OpdsUser) -> Response:
    body = (
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">'
        "<id>urn:lectern:root</id><title>Lectern</title>"
        f"<updated>{_iso(None)}</updated>"
        "<author><name>Lectern</name></author>"
        f'<link rel="self" href="/opds" type="{NAV}"/>'
        f'<link rel="start" href="/opds" type="{NAV}"/>'
        f'<link rel="search" href="/opds/search.xml" type="{OPENSEARCH}"/>'
        f'<link rel="search" href="/opds/search?q={{searchTerms}}" type="{ACQ}"/>'
        "<entry><title>All books</title><id>urn:lectern:all</id>"
        f'<updated>{_iso(None)}</updated><content type="text">Every work in the library</content>'
        f'<link rel="subsection" href="/opds/all" type="{ACQ}"/></entry>'
        "<entry><title>Recently added</title><id>urn:lectern:recent</id>"
        f'<updated>{_iso(None)}</updated><content type="text">Newest first</content>'
        f'<link rel="http://opds-spec.org/sort/new" href="/opds/recent" type="{ACQ}"/></entry>'
        "</feed>"
    )
    return _xml(body, NAV)


def _acquisition(
    title: str, self_href: str, works: list[Work], page: int, total: int, extra_q: str = ""
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
            f'<link rel="next" href="{escape(base)}?page={page + 1}{extra_q}" type="{ACQ}"/>'
        )
    if page > 1:
        links.append(
            f'<link rel="previous" href="{escape(base)}?page={page - 1}{extra_q}" type="{ACQ}"/>'
        )
    body = (
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/" '
        'xmlns:opds="http://opds-spec.org/2010/catalog" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        f"<id>urn:lectern:{escape(base)}</id><title>{_text(title)}</title>"
        f"<updated>{_iso(None)}</updated><author><name>Lectern</name></author>"
        f"<opensearch:totalResults>{total}</opensearch:totalResults>"
        f"<opensearch:itemsPerPage>{PAGE_SIZE}</opensearch:itemsPerPage>"
        f"<opensearch:startIndex>{(page - 1) * PAGE_SIZE + 1}</opensearch:startIndex>"
        + "".join(links)
        + "".join(_entry(w) for w in works)
        + "</feed>"
    )
    return _xml(body, ACQ)


@router.get("/all", include_in_schema=False)
def all_books(user: OpdsUser, db: DbDep, page: int = 1) -> Response:
    page = max(1, page)
    works, total = search_works(db, user, None, page)
    return _acquisition("All books", f"/opds/all?page={page}", works, page, total)


@router.get("/recent", include_in_schema=False)
def recent(user: OpdsUser, db: DbDep, page: int = 1) -> Response:
    from sqlalchemy import func, select
    from sqlalchemy.orm import selectinload

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
    return download_response(db, settings, book, path, user, device, client_ip(request), "opds")
