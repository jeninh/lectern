"""CSRF defence for cookie-authenticated browser routes.

Two layers: (1) Origin / Sec-Fetch-Site must be same-site for unsafe methods, and
(2) a per-session token must be echoed back in the X-CSRF-Token header or the csrf_token
form field. Token-less API clients (OPDS, KOReader) never use cookies, so this is not
applied to them.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from lectern.config import Settings
from lectern.security.tokens import constant_time_equal

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def _origin_ok(request: Request, settings: Settings) -> bool:
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return False
    origin = request.headers.get("origin")
    if origin is None:
        referer = request.headers.get("referer")
        if referer:
            parts = urlsplit(referer)
            origin = f"{parts.scheme}://{parts.netloc}"
    if origin is None or origin == "null":
        # No origin information at all: browsers always send Origin on POST, so
        # treat its absence as suspicious unless Sec-Fetch-Site already vouched for it.
        return fetch_site in {"same-origin", "none"}
    allowed = {settings.base_url.lower()}
    if settings.insecure_http:
        allowed.add(f"{request.url.scheme}://{request.headers.get('host', '')}".lower())
    return origin.lower() in allowed


async def verify_csrf(request: Request, settings: Settings, session_csrf: str) -> None:
    if request.method not in UNSAFE:
        return
    if not _origin_ok(request, settings):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-site request blocked")
    supplied = request.headers.get("x-csrf-token")
    if supplied is None:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("application/x-www-form-urlencoded") or ctype.startswith(
            "multipart/form-data"
        ):
            form = await request.form()
            value = form.get("csrf_token")
            supplied = value if isinstance(value, str) else None
    if not supplied or not constant_time_equal(supplied, session_csrf):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid CSRF token")
