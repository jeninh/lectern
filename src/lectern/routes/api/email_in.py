"""Email-in settings (issue #67, docs/ingest-paths.md).

Everyone who may upload (owners and members):

- ``GET    /api/email-in``           whether it runs (``reason``: ``off``, ``not_configured``,
  ``offline``), the person's address and destination shelf, the sender filter's mode, the
  last poll
- ``POST   /api/email-in/address``   make the address, or a new one (the old one stops working)
- ``DELETE /api/email-in/address``   turn email-in off for this person
- ``PUT    /api/email-in/shelf``     ``{"shelf_id": n | null}``: the shelf emailed books go on

The owner also sees which ``.env`` keys are missing or invalid (names only), the filter's
entries, and why the last poll failed, and sets the filter unless ``.env`` does:

- ``PUT    /api/email-in/senders``   ``{"mode", "entries", "acknowledge_open"}``; switching to
  ``blocklist`` (anyone not listed may send files) needs ``acknowledge_open: true``

The mailbox itself (host, user, app password) is configured only in ``.env``; nothing here
shows or changes it.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from lectern import email_in
from lectern.access import can_edit_shelf
from lectern.models import EmailInbox, Role, Shelf
from lectern.security import audit
from lectern.security.deps import (
    AuthContext,
    DbDep,
    SettingsDep,
    require_role,
    require_role_read,
)
from lectern.security.sessions import client_ip

router = APIRouter(prefix="/email-in")

Editor = Annotated[AuthContext, Depends(require_role(Role.OWNER, Role.MEMBER))]
Viewer = Annotated[AuthContext, Depends(require_role_read(Role.OWNER, Role.MEMBER))]
Owner = Annotated[AuthContext, Depends(require_role(Role.OWNER))]


class ShelfIn(BaseModel):
    shelf_id: int | None


class FilterIn(BaseModel):
    mode: Literal["allowlist", "blocklist"]
    entries: list[str] = Field(max_length=email_in.MAX_FILTER_ENTRIES)
    acknowledge_open: bool = False


def _shelf_name(db, shelf: Shelf) -> str:
    from lectern.access import lineage

    return " / ".join(s.name for s in reversed(lineage(shelf)))


def _state(db, settings, auth: AuthContext) -> dict:
    owner = auth.user.role == Role.OWNER
    cfg, problems = email_in.load(settings)
    inbox = db.get(EmailInbox, auth.user.id)
    shelf = email_in.destination_shelf(db, inbox, auth.user) if inbox is not None else None
    filt = email_in.sender_filter(db, settings)
    poll = email_in.last_poll()
    if not owner:
        poll = {"at": poll["at"], "ok": poll["ok"]}  # why a poll failed is the owner's to see
    out = {
        "enabled": email_in.unavailable(settings) is None,
        "reason": email_in.unavailable(settings),
        "address": email_in.address_for(cfg, inbox.token) if inbox and cfg else None,
        "has_address": inbox is not None,
        "shelf": {"id": shelf.id, "name": _shelf_name(db, shelf)} if shelf else None,
        "shelf_missing": inbox is not None and inbox.shelf_id is not None and shelf is None,
        "formats": [t.lstrip(".").upper() for t in email_in.ALLOWED_TYPES],
        "max_mb": cfg.max_bytes // (1024 * 1024) if cfg else None,
        "poll_minutes": cfg.poll_s / 60 if cfg else None,
        "filter": {"mode": filt.mode, "source": filt.source, "count": len(filt.entries)},
        "last_poll": poll,
    }
    if owner:
        out["missing"] = problems
        # Entries set in .env stay there (count only); the owner's own list is theirs to see.
        if filt.source == "app":
            out["filter"]["entries"] = list(filt.entries)
    return out


@router.get("")
def get_state(auth: Viewer, db: DbDep, settings: SettingsDep) -> dict:
    return _state(db, settings, auth)


@router.post("/address")
def new_address(request: Request, auth: Editor, db: DbDep, settings: SettingsDep) -> dict:
    if not email_in.configured(settings):
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": "not_configured"})
    inbox = db.get(EmailInbox, auth.user.id)
    if inbox is None:
        db.add(EmailInbox(user_id=auth.user.id, token=email_in.new_token()))
    else:
        inbox.token = email_in.new_token()
    audit.record(db, "email_in.address", actor_user_id=auth.user.id,
                 ip=client_ip(request), detail="new" if inbox is None else "replaced")  # fmt: skip
    db.commit()
    return _state(db, settings, auth)


@router.delete("/address", status_code=status.HTTP_204_NO_CONTENT)
def remove_address(request: Request, auth: Editor, db: DbDep) -> Response:
    inbox = db.get(EmailInbox, auth.user.id)
    if inbox is not None:
        db.delete(inbox)
        audit.record(db, "email_in.off", actor_user_id=auth.user.id, ip=client_ip(request))
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/shelf")
def set_shelf(
    request: Request, auth: Editor, db: DbDep, settings: SettingsDep, body: ShelfIn
) -> dict:
    inbox = db.get(EmailInbox, auth.user.id)
    if inbox is None:
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": "no_address"})
    if body.shelf_id is not None:
        shelf = db.get(Shelf, body.shelf_id)
        if shelf is None or not can_edit_shelf(db, auth.user, shelf):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"detail": "bad_shelf"})
        if shelf.rule is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"detail": "smart_shelf"})
    inbox.shelf_id = body.shelf_id
    audit.record(db, "email_in.shelf", actor_user_id=auth.user.id, ip=client_ip(request),
                 target=f"shelf:{body.shelf_id}" if body.shelf_id else None)  # fmt: skip
    db.commit()
    return _state(db, settings, auth)


@router.put("/senders")
def set_filter(
    request: Request, auth: Owner, db: DbDep, settings: SettingsDep, body: FilterIn
) -> dict:
    current = email_in.sender_filter(db, settings)
    if current.source == "env":
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": "set_in_env"})
    if (
        body.mode == email_in.BLOCKLIST
        and current.mode != email_in.BLOCKLIST
        and not body.acknowledge_open
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"detail": "confirm_blocklist"})
    entries: list[str] = []
    for raw in body.entries:
        try:
            e = email_in.normalize_entry(raw)
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, {"detail": "bad_sender", "entry": raw[:254]}
            ) from None
        if e not in entries:
            entries.append(e)
    email_in.save_filter(db, body.mode, entries, auth.user.id)
    # Mode and count only: the addresses themselves stay out of the audit log (and the log).
    audit.record(db, "email_in.senders", actor_user_id=auth.user.id, ip=client_ip(request),
                 detail=f"{body.mode}, {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}")  # fmt: skip
    db.commit()
    return _state(db, settings, auth)
