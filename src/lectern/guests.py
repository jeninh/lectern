"""Guest accounts (issue #77): friends and family who read part of the library over the tailnet.

A guest is a reader (``Role.READER``) with a :class:`~lectern.models.GuestProfile`. Everything a
reader may see is still decided by :mod:`lectern.access` (``visible_works``: only works on shelves
granted to them, and a guest's grants are VIEW only). Three more layers make a guest narrower:

1. **Deny by default.** A guest reaches only the routes listed in :data:`SESSION_ROUTES` (browser
   session) and :data:`BASIC_ROUTES` (an app password, for OPDS readers). :func:`enforce` runs
   inside the shared authentication dependencies (``security.deps``), so a route added later is
   closed to guests until it is listed here; ``tests/test_guests.py`` checks every registered
   route. Sync keys (KOReader), AI keys (MCP), device pairing, and Send to device are never open
   to guests: those credentials are refused outright.
2. **No owner data in responses.** The routes a guest may call serialise only the guest's own
   state and the works they can see. Places that would name other people or count more than
   they see (shelf owners, grant counts, household size) are blanked for a guest.
3. **Expiry.** An expired guest is not :func:`usable <account_usable>` anywhere an account is
   loaded (sessions, app passwords, sync keys, AI keys, sign-in), by comparing the clock, so no
   sweep has to run in time. :func:`revoke_credentials` also revokes the rows themselves.

Limits an owner can set: ``allow_opds`` (file downloads, app passwords, and OPDS). Web reading
and listening are always on; the reader needs the book file, so ``/library/files/{id}/view`` and
``/library/tracks/{id}/stream`` stay open even without ``allow_opds``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lectern.models import (
    AIKey,
    AppPassword,
    DeviceBootstrap,
    DevicePairingCode,
    GuestProfile,
    Shelf,
    SyncKey,
    User,
    utcnow,
)
from lectern.models import Session as LoginSession

log = logging.getLogger("lectern.guests")

# What a browser session may do. Keys are (method, route template); every other combination is
# refused with 403 ``guest_forbidden``. Only reads, plus a guest's own reading position and
# preferences. Nothing here creates, edits, or removes anything in the library.
SESSION_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/me"),
        ("GET", "/api/export"),
        ("POST", "/api/me/preferences"),
        ("PUT", "/api/me/reader-settings"),
        ("GET", "/api/library"),
        ("GET", "/api/library/facets"),
        ("GET", "/api/library/fields"),
        ("GET", "/api/library/count"),
        ("GET", "/api/continue"),
        ("GET", "/api/listening"),
        ("GET", "/api/next-in-series"),
        ("GET", "/api/shelves"),
        ("GET", "/api/shelves/{shelf_id}"),
        ("GET", "/api/search/text"),
        ("GET", "/api/works/{work_id}"),
        ("GET", "/api/works/{work_id}/tracks"),
        ("GET", "/api/works/{work_id}/sentences"),
        ("GET", "/api/works/{work_id}/progress"),
        ("PUT", "/api/works/{work_id}/progress"),
        ("GET", "/api/works/{work_id}/rewind-point"),
        ("POST", "/api/works/{work_id}/listen"),
        ("GET", "/api/works/{work_id}/text/{spine_index}"),
        ("GET", "/api/works/{work_id}/read-aloud"),
        ("GET", "/api/works/{work_id}/reader-settings"),
        ("PUT", "/api/works/{work_id}/reader-settings"),
        ("DELETE", "/api/works/{work_id}/reader-settings"),
        ("GET", "/api/files/{file_id}/pages"),
        ("GET", "/api/files/{file_id}/pages/{n}"),
        ("PUT", "/api/files/{file_id}/progress"),
        ("GET", "/api/files/{file_id}/text"),
        ("GET", "/api/files/{file_id}/sentences"),
        ("GET", "/library/works/{work_id}/cover.jpg"),
        ("GET", "/library/works/{work_id}/book.epub"),
        ("GET", "/library/files/{file_id}/view"),
        ("GET", "/library/tracks/{track_id}/stream"),
        # The web app itself (static shell; every byte of data comes from /api).
        ("GET", "/"),
        ("GET", "/app"),
        ("GET", "/app/{path:path}"),
        ("GET", "/app/doc-media/{name}"),
        ("GET", "/app/doc-text"),
    }
)

# Only with ``allow_opds``: whole-file downloads and the app passwords OPDS readers sign in with.
DOWNLOAD_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/library/files/{file_id}/download"),
        ("GET", "/library/tracks/{track_id}/download"),
        ("GET", "/library/works/{work_id}/read-aloud.epub"),
        ("GET", "/api/devices"),
        ("POST", "/api/devices/app-passwords"),
        ("POST", "/api/devices/app-passwords/{key_id}/revoke"),
    }
)

# What an app password (HTTP Basic) may do, also only with ``allow_opds``. Not the device queue,
# the want-to-read list, or the KOReader plugin's status and report routes.
BASIC_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/opds"),
        ("GET", "/opds/"),
        ("GET", "/opds/continue"),
        ("GET", "/opds/up-next"),
        ("GET", "/opds/next-in-series"),
        ("GET", "/opds/all"),
        ("GET", "/opds/recent"),
        ("GET", "/opds/shelves"),
        ("GET", "/opds/shelves/none"),
        ("GET", "/opds/shelves/{shelf_id}"),
        ("GET", "/opds/shelves/{shelf_id}/books"),
        ("GET", "/opds/search.xml"),
        ("GET", "/opds/search"),
        ("GET", "/opds/works/{work_id}/cover.jpg"),
        ("GET", "/opds/files/{file_id}/download"),
        ("GET", "/library/works/{work_id}/cover.jpg"),
        ("GET", "/library/files/{file_id}/download"),
        ("GET", "/library/tracks/{track_id}/download"),
        ("GET", "/library/works/{work_id}/read-aloud.epub"),
    }
)


def is_guest(user: User) -> bool:
    return user.guest is not None


def expired(user: User, now: datetime | None = None) -> bool:
    g = user.guest
    return g is not None and g.expires_at is not None and g.expires_at <= (now or utcnow())


def account_usable(user: User | None, now: datetime | None = None) -> bool:
    """Whether ``user`` may sign in or use a credential now: active, and not an expired guest.
    Every place that turns a credential into a user asks this, not ``user.is_active``."""
    return user is not None and user.is_active and not expired(user, now)


def allows_opds(user: User) -> bool:
    return user.guest is not None and user.guest.allow_opds


def _forbidden() -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, "guest_forbidden")


def _route_key(request: Request) -> tuple[str, str] | None:
    """(method, full route template), e.g. ``("GET", "/api/works/{work_id}")``. The matched route
    knows only its own part of the path (an included router's prefix is not in it), so the
    prefix is what is left of the request path once the route's filled-in part is removed. If
    that does not work out, there is no key and the guest is refused."""
    route = request.scope.get("route")
    fmt = getattr(route, "path_format", None)
    if not fmt:
        return None
    filled = fmt
    for name, value in request.scope.get("path_params", {}).items():
        filled = filled.replace("{" + name + "}", str(value))
    actual = request.scope.get("path", "")
    if not actual.endswith(filled):
        return None
    return request.method.upper(), actual[: len(actual) - len(filled)] + route.path


def enforce(request: Request, user: User, channel: str) -> None:
    """Refuse a guest the request unless this method and route are listed for ``channel``:
    ``session`` (browser cookie) or ``basic`` (app password). Any other channel (sync key, AI
    key) is refused for guests without looking at the route. A non-guest is never affected."""
    if user.guest is None:
        return
    server = request.scope.get("server")
    lan_port = request.app.state.settings.lan_port
    if lan_port and server and server[1] == lan_port:
        # The home Wi-Fi listener answers household e-readers only, never a guest.
        log.warning("guest %s refused on the LAN listener", user.id)
        raise _forbidden()
    key = _route_key(request)
    if key is not None:
        if channel == "session" and (
            key in SESSION_ROUTES or (key in DOWNLOAD_ROUTES and allows_opds(user))
        ):
            return
        if channel == "basic" and key in BASIC_ROUTES and allows_opds(user):
            return
    log.warning("guest %s refused: %s %s via %s", user.id, *(key or ("?", "?")), channel)
    raise _forbidden()


def revoke_credentials(db: Session, user: User) -> None:
    """Sign a guest out everywhere and revoke every credential they hold. Caller commits."""
    now = utcnow()
    db.execute(
        update(LoginSession)
        .where(LoginSession.user_id == user.id, LoginSession.revoked.is_(False))
        .values(revoked=True)
    )
    for model in (SyncKey, AppPassword, AIKey):
        db.execute(
            update(model)
            .where(model.user_id == user.id, model.revoked_at.is_(None))
            .values(revoked_at=now)
        )
    db.execute(
        update(DevicePairingCode)
        .where(DevicePairingCode.user_id == user.id, DevicePairingCode.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    db.execute(
        update(DeviceBootstrap)
        .where(DeviceBootstrap.user_id == user.id, DeviceBootstrap.used_at.is_(None))
        .values(used_at=now)
    )


def sweep_expired(db: Session) -> None:
    """Revoke the credentials of guests whose end date has passed. Idempotent; caller commits.
    Access is already refused by :func:`account_usable`; this makes the rows say so too."""
    now = utcnow()
    for user in db.scalars(
        select(User).join(GuestProfile).where(GuestProfile.expires_at <= now)
    ).unique():
        revoke_credentials(db, user)


def grantable_shelf(shelf: Shelf | None) -> bool:
    """Guests are given ordinary, hand-made shelves only: not smart or generated ones, whose
    contents are computed from the whole library."""
    return shelf is not None and shelf.rule is None and shelf.auto_kind is None


def check_guest_shelves(db: Session, shelf_ids: list[int]) -> list[int]:
    """The distinct shelves a guest may be given, or 422/409: each must exist and be a hand-made
    shelf (never a smart or generated one)."""
    ids = list(dict.fromkeys(shelf_ids))
    for sid in ids:
        shelf = db.get(Shelf, sid)
        if shelf is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown_shelf")
        if not grantable_shelf(shelf):
            raise HTTPException(status.HTTP_409_CONFLICT, "guest_shelf_kind")
    return ids


def as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value
