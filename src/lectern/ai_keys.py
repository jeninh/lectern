"""AI keys: per-user, scoped bearer credentials for AI apps on the MCP endpoint (docs/mcp.md).

A key is created by its user in Devices → AI tools, shown once, and stored as SHA-256 (it
carries 256 bits of entropy, see ``security/tokens.py``). It acts as its user and only within
its scopes:

- ``upload``   add books: upload, review, or cancel imports (owners and members only).
- ``file``     file reviewed imports into the library (``confirm_import``, and filing a
               certain match automatically with the ``auto_file_imports`` preference).
- ``read``     search the library, book details, alignment status, the user's progress.
- ``organize`` shelves, send to device and its queues, start alignment and its jobs, metadata.
- ``reviews``  the user's private ratings, notes, and read-through dates (``lectern.reviews``),
               and the ``rating:`` filter. Never a default: only when its user adds it.
- ``stats``    the user's private reading statistics and year review. Never a default.

Its owner can change a key's scopes later (``routes/api/ai_keys.py``) without seeing or
rotating the secret. Every tool call reads the scopes from the database again, so a change
applies to the key's next request. Adding a scope, or creating a key with more than
the defaults, needs a fresh password or passkey (``security/stepup.py``).

A key never reaches anything else. Only ``/mcp`` accepts it, and no tool manages users,
invites, keys, or server settings, or returns a book file or audio.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lectern import guests, notify
from lectern.access import can_edit_library
from lectern.models import AIKey, User, utcnow
from lectern.security import audit
from lectern.security.tokens import new_secret, sha256_hex

PREFIX = "lak_"
SCOPES = ("upload", "file", "read", "organize", "reviews", "stats")
# Scopes only people who can add books may hold.
EDITOR_SCOPES = ("upload", "file")
HOUR = timedelta(hours=1)


def allowed_scopes(user: User) -> list[str]:
    """Readers cannot add books, so they cannot hold ``upload`` or ``file``."""
    if can_edit_library(user):
        return list(SCOPES)
    return [s for s in SCOPES if s not in EDITOR_SCOPES]


def default_scopes(user: User) -> list[str]:
    """Adding and filing books for people who can add them; read for readers."""
    return ["upload", "file"] if can_edit_library(user) else ["read"]


def ordered(scopes) -> list[str]:
    return [s for s in SCOPES if s in scopes]


def new_key(user: User, label: str, scopes: list[str]) -> tuple[AIKey, str]:
    secret = PREFIX + new_secret()
    return (
        AIKey(
            user_id=user.id,
            label=label,
            key_hash=sha256_hex(secret),
            prefix=secret[:12],
            scopes=ordered(scopes),
        ),
        secret,
    )


def bearer(header: str | None) -> str:
    header = header or ""
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def authenticate(db: Session, secret: str) -> tuple[AIKey, User] | None:
    """The active key and its active user, or None (unknown, revoked, or user disabled)."""
    if not secret.startswith(PREFIX) or len(secret) > 128:
        return None
    key = db.scalar(select(AIKey).where(AIKey.key_hash == sha256_hex(secret)))
    if key is None or key.revoked_at is not None:
        return None
    user = db.get(User, key.user_id)
    # AI keys and MCP are never open to guests.
    if not guests.account_usable(user) or user.guest is not None:
        return None
    return key, user


def record_use(db: Session, key: AIKey, user: User, ip: str | None, client: str | None) -> None:
    """Stamp last use; the first use ever alerts the key's user. Commits."""
    now = utcnow()
    first = db.execute(
        update(AIKey)
        .where(AIKey.id == key.id, AIKey.last_used_at.is_(None))
        .values(last_used_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    key.last_used_at, key.last_ip, key.last_client = now, ip, (client or None) and client[:128]
    if first:
        audit.record(
            db, "ai_key.first_use", actor_user_id=user.id, target=key.label, ip=ip, detail=client
        )
        notify.notify(
            db,
            user,
            "ai_key.connected",
            f"{key.label} connected",
            f"First request from this AI app{f' from {ip}' if ip else ''}. Revoke its key "
            "under Devices → AI tools if you did not set this up.",
            data={"key": f"ai_key:{key.id}", "ip": ip},
        )
    db.commit()


def burst_alert(db: Session, key: AIKey, user: User, ip: str | None, count: int, window_s: int):
    """Many tool calls in a short window: alert the user and the owners, once an hour. Caller
    commits."""
    minutes = max(1, window_s // 60)
    if notify.recently_sent(db, user.id, "ai_key.burst", f"ai_key:{key.id}", HOUR):
        return
    audit.record(
        db,
        "ai_key.burst",
        actor_user_id=user.id,
        target=key.label,
        ip=ip,
        detail=f"{count} calls in {minutes} min",
    )
    notify.notify_user_and_owners(
        db,
        user,
        "ai_key.burst",
        f"{key.label}: {count} AI tool calls in {minutes} minutes",
        f"Latest{f' from {ip}' if ip else ''}. If you did not ask your AI app for this much "
        "work, revoke its key under Devices → AI tools.",
        owner_title=f"{user.username}'s AI key {key.label}: {count} calls in {minutes} min",
        data={"key": f"ai_key:{key.id}", "count": count},
        dedupe=HOUR,
    )
