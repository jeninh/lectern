"""Optional metadata suggestions from Open Library and Google Books (no API key).

Off by default: it sends a book's ISBN, or its title and author, to those two services. The
owner turns it on in Admin (``instance_settings.metadata_lookup_enabled``). It only ever
*suggests*: the editor shows each suggestion next to the current value and a person opts in
field by field; nothing here writes to a work.

It looks up metadata only. It never searches for, links to, or downloads books.

Outbound safety:

- fixed upstreams: the URL is built here from a constant ``https://`` origin and URL-encoded
  query values; nothing from the client chooses a host, path, or scheme (no SSRF);
- redirects are refused, so a response cannot send the server elsewhere;
- a connect/read timeout and an overall deadline per request, and a hard cap on the body
  read (``MAX_BODY_BYTES``); responses must be JSON;
- a per-user rate limit (in the route), a per-provider global limit, and a 24 h cache, so a
  household cannot hammer the services or be told apart by repeated queries;
- results are reduced to plain-text fields and validated with the same schema as edits.
"""

from __future__ import annotations

import html
import json
import logging
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from typing import Annotated
from urllib.parse import urlencode

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from lectern import __version__, egress
from lectern.ingest.epub import normalize_isbn
from lectern.security.ratelimit import SlidingWindowLimiter
from lectern.workmeta import Metadata, strip_controls

log = logging.getLogger("lectern.lookup")

TIMEOUT_S = 6.0
DEADLINE_S = 10.0
MAX_BODY_BYTES = 1024 * 1024
CACHE_TTL_S = 24 * 3600
CACHE_SIZE = 256
PROVIDER_PER_MINUTE = 30

PROVIDERS = {
    "openlibrary": {"name": "Open Library", "origin": "https://openlibrary.org"},
    "googlebooks": {"name": "Google Books", "origin": "https://www.googleapis.com"},
}
_OL_WORK_KEY = re.compile(r"^/works/OL\d{1,12}W$")
_GB_ID = re.compile(r"^[A-Za-z0-9_-]{4,32}$")
_LANG3 = {
    "eng": "en", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de", "spa": "es", "ita": "it",
    "por": "pt", "dut": "nl", "nld": "nl", "jpn": "ja", "rus": "ru", "chi": "zh", "zho": "zh",
    "swe": "sv", "nor": "no", "dan": "da", "fin": "fi", "pol": "pl", "cze": "cs", "ces": "cs",
    "gre": "el", "ell": "el", "tur": "tr", "ara": "ar", "heb": "he", "kor": "ko", "lat": "la",
}  # fmt: skip


class LookupError_(Exception):  # noqa: N801
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Query(BaseModel):
    model_config = ConfigDict(extra="forbid")

    isbn: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)] | None = None
    title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    author: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    providers: list[str] = Field(default_factory=lambda: list(PROVIDERS), max_length=2)

    @model_validator(mode="after")
    def _check(self) -> Query:
        if self.isbn:
            self.isbn = normalize_isbn(self.isbn)
            if self.isbn is None:
                raise ValueError("not a valid ISBN")
        if not self.isbn and not self.title:
            raise ValueError("an ISBN or a title is required")
        if not set(self.providers) <= set(PROVIDERS) or not self.providers:
            raise ValueError("unknown provider")
        return self

    def sent(self) -> dict:
        """What leaves the server: the ISBN alone when there is one."""
        if self.isbn:
            return {"isbn": self.isbn}
        return {"title": self.title, **({"author": self.author} if self.author else {})}


# --- transport ------------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise LookupError_("redirect_refused")


_OPENER = urllib.request.build_opener(
    _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
)


def http_get(url: str) -> bytes:
    """GET a fixed-origin URL with the safety limits above. Tests replace this function."""
    origin = next(
        (p["origin"] for p in PROVIDERS.values() if url.startswith(p["origin"] + "/")), None
    )
    if origin is None:
        raise LookupError_("bad_upstream")
    req = urllib.request.Request(  # noqa: S310 - https origin checked above
        url,
        headers={
            "User-Agent": f"Lectern/{__version__} (self-hosted reading server; metadata lookup)",
            "Accept": "application/json",
        },
    )
    started = time.monotonic()
    try:
        with _OPENER.open(req, timeout=TIMEOUT_S) as resp:
            if resp.status != 200:
                raise LookupError_("unavailable")
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype:
                raise LookupError_("bad_response")
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                raise LookupError_("too_large")
            body = bytearray()
            while True:
                if time.monotonic() - started > DEADLINE_S:
                    raise LookupError_("timeout")
                block = resp.read(64 * 1024)
                if not block:
                    break
                body += block
                if len(body) > MAX_BODY_BYTES:
                    raise LookupError_("too_large")
            return bytes(body)
    except LookupError_:
        raise
    except urllib.error.HTTPError as exc:
        raise LookupError_("rate_limited" if exc.code == 429 else "unavailable") from None
    except TimeoutError:
        raise LookupError_("timeout") from None
    except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
        refused = isinstance(getattr(exc, "reason", exc), egress.OfflineError)
        raise LookupError_("offline" if refused else "unavailable") from None


# --- providers --------------------------------------------------------------------------------


def _json(fetch: Callable[[str], bytes], url: str) -> dict:
    try:
        data = json.loads(fetch(url))
    except (ValueError, UnicodeDecodeError):
        raise LookupError_("bad_response") from None
    if not isinstance(data, dict):
        raise LookupError_("bad_response")
    return data


def _plain(text: object, limit: int = 20000) -> str | None:
    if not isinstance(text, str):
        return None
    text = re.sub(r"<(?:br|/?p|/?div|/?li)\b[^>]{0,200}>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]{0,200}>", "", text)
    text = " ".join(html.unescape(text).split())
    return text[:limit] or None


def _strs(v: object, n: int = 20, limit: int = 128) -> list[str]:
    if not isinstance(v, list):
        return []
    return [s.strip()[:limit] for s in v if isinstance(s, str) and s.strip()][:n]


_SERIES_NUMBER = re.compile(
    r"^(?P<name>.+?)[\s,;:]*(?:\(|#|\b(?:book|vol\.?|volume|no\.?)\s*#?)\s*"
    r"(?P<n>\d{1,4}(?:\.\d{1,2})?)\)?\s*$",
    re.IGNORECASE,
)


def split_series(text: str | None) -> tuple[str | None, float | None]:
    """'Discworld (3)', 'Discworld #3', 'Discworld, book 3' -> ('Discworld', 3.0)."""
    if not text:
        return None, None
    m = _SERIES_NUMBER.match(text.strip())
    if not m or not m.group("name").strip(" ,;:"):
        return text.strip() or None, None
    return m.group("name").strip(" ,;:"), float(m.group("n"))


def openlibrary(q: Query, fetch: Callable[[str], bytes]) -> list[dict]:
    origin = PROVIDERS["openlibrary"]["origin"]
    params = {
        "limit": "3",
        "fields": "key,title,author_name,first_publish_year,publisher,isbn,language,subject,series",
    }
    if q.isbn:
        params["isbn"] = q.isbn
    else:
        params["title"] = q.title or ""
        if q.author:
            params["author"] = q.author
    data = _json(fetch, f"{origin}/search.json?{urlencode(params)}")
    docs = data.get("docs") if isinstance(data.get("docs"), list) else []
    out = []
    for i, d in enumerate(docs[:3]):
        if not isinstance(d, dict):
            continue
        key = (
            d.get("key") if isinstance(d.get("key"), str) and _OL_WORK_KEY.match(d["key"]) else None
        )
        description = None
        if key and i == 0:
            try:
                w = _json(fetch, f"{origin}{key}.json")
                desc = w.get("description")
                description = _plain(desc.get("value") if isinstance(desc, dict) else desc)
            except LookupError_:
                description = None
        isbns = [normalize_isbn(x) for x in _strs(d.get("isbn"), 50, 20)]
        isbn = q.isbn if q.isbn else next((x for x in isbns if x), None)
        langs = _strs(d.get("language"), 3, 8)
        series, series_index = split_series((_strs(d.get("series"), 1, 256) or [None])[0])
        year = d.get("first_publish_year")
        out.append(
            {
                "source_url": f"https://openlibrary.org{key}"
                if key
                else "https://openlibrary.org/",
                "fields": {
                    "title": _plain(d.get("title"), 512),
                    "authors": [{"name": a} for a in _strs(d.get("author_name"), 10, 256)],
                    "series": series,
                    "series_index": series_index,
                    "description": description,
                    "language": _LANG3.get(langs[0]) if langs else None,
                    "isbn": isbn,
                    "publisher": _strs(d.get("publisher"), 1, 256)[0]
                    if _strs(d.get("publisher"), 1)
                    else None,
                    "published": str(year) if isinstance(year, int) and 0 < year < 3000 else None,
                    "tags": _strs(d.get("subject"), 8, 64),
                },
            }
        )
    return out


def googlebooks(q: Query, fetch: Callable[[str], bytes]) -> list[dict]:
    origin = PROVIDERS["googlebooks"]["origin"]
    if q.isbn:
        term = f"isbn:{q.isbn}"
    else:
        clean = lambda s: re.sub(r'[":+]', " ", s or "").strip()  # noqa: E731
        term = f"intitle:{clean(q.title)}" + (f" inauthor:{clean(q.author)}" if q.author else "")
    params = {
        "q": term,
        "maxResults": "3",
        "printType": "books",
        "fields": "items(id,volumeInfo(title,subtitle,authors,publisher,publishedDate,description,"
        "industryIdentifiers,language,categories))",
    }
    data = _json(fetch, f"{origin}/books/v1/volumes?{urlencode(params)}")
    items = data.get("items") if isinstance(data.get("items"), list) else []
    out = []
    for it in items[:3]:
        if not isinstance(it, dict) or not isinstance(it.get("volumeInfo"), dict):
            continue
        v = it["volumeInfo"]
        vid = it.get("id") if isinstance(it.get("id"), str) and _GB_ID.match(it["id"]) else None
        ids = v.get("industryIdentifiers") if isinstance(v.get("industryIdentifiers"), list) else []
        isbns = [
            normalize_isbn(x.get("identifier", ""))
            for x in ids
            if isinstance(x, dict) and isinstance(x.get("identifier"), str)
        ]
        date = v.get("publishedDate") if isinstance(v.get("publishedDate"), str) else None
        m = re.match(r"\d{4}(-\d{2}(-\d{2})?)?", date or "")
        out.append(
            {
                "source_url": f"https://books.google.com/books?id={vid}"
                if vid
                else "https://books.google.com/",
                "fields": {
                    "title": _plain(v.get("title"), 512),
                    "authors": [{"name": a} for a in _strs(v.get("authors"), 10, 256)],
                    "description": _plain(v.get("description")),
                    "language": v.get("language") if isinstance(v.get("language"), str) else None,
                    "isbn": next((x for x in isbns if x and len(x) == 13), None),
                    "publisher": _plain(v.get("publisher"), 256),
                    "published": m.group(0) if m else None,
                    "tags": _strs(v.get("categories"), 8, 64),
                },
            }
        )
    return out


_IMPLS = {"openlibrary": openlibrary, "googlebooks": googlebooks}


def _validated(fields: dict) -> dict:
    """Keep only fields the edit schema accepts, as the editor would apply them."""
    out = {}
    fields = strip_controls(fields)  # upstream text: drop what XML cannot carry
    for k, v in fields.items():
        if v in (None, "", []):
            continue
        try:
            probe = Metadata.model_validate({"title": fields.get("title") or "x", k: v})
        except ValidationError:
            continue
        out[k] = getattr(probe, k) if k != "authors" else [a.model_dump() for a in probe.authors]
    return out


# --- cache, limits, entry point -------------------------------------------------------------------

_cache: OrderedDict[tuple, tuple[float, list[dict]]] = OrderedDict()
_cache_lock = threading.Lock()
_provider_limits = {
    p: SlidingWindowLimiter(limit=PROVIDER_PER_MINUTE, window_seconds=60) for p in PROVIDERS
}


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
    for lim in _provider_limits.values():
        lim.reset("global")


def lookup(q: Query, fetch: Callable[[str], bytes] | None = None) -> dict:
    """Suggestions from each requested provider, with provenance and per-provider errors."""
    fetch = fetch or http_get
    sent = q.sent()
    suggestions: list[dict] = []
    errors: list[dict] = []
    cached_all = True
    for provider in q.providers:
        key = (provider, tuple(sorted(sent.items())))
        now = time.monotonic()
        with _cache_lock:
            hit = _cache.get(key)
            if hit and hit[0] > now:
                _cache.move_to_end(key)
                results = hit[1]
            else:
                results = None
        if results is None:
            cached_all = False
            if not _provider_limits[provider].allow("global"):
                errors.append({"provider": provider, "error": "rate_limited"})
                continue
            try:
                raw = _IMPLS[provider](q, fetch)
            except LookupError_ as exc:
                log.info("metadata lookup %s failed: %s", provider, exc.code)
                errors.append({"provider": provider, "error": exc.code})
                continue
            results = [
                {
                    "provider": provider,
                    "provider_name": PROVIDERS[provider]["name"],
                    "source_url": r["source_url"],
                    "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "fields": _validated(r["fields"]),
                }
                for r in raw
            ]
            results = [r for r in results if r["fields"]]
            with _cache_lock:
                _cache[key] = (now + CACHE_TTL_S, results)
                while len(_cache) > CACHE_SIZE:
                    _cache.popitem(last=False)
        suggestions += results
    return {
        "sent": sent,
        "suggestions": suggestions,
        "errors": errors,
        "cached": cached_all and not errors,
    }
