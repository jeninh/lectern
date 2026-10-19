"""First-run owner setup token lifecycle."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy.orm import Session

from lectern.config import Settings
from lectern.models import SetupState, utcnow
from lectern.security.tokens import constant_time_equal, new_secret, sha256_hex

log = logging.getLogger("lectern.setup")


def get_state(db: Session) -> SetupState:
    state = db.get(SetupState, 1)
    if state is None:
        state = SetupState(id=1)
        db.add(state)
        db.flush()
    return state


def setup_completed(db: Session) -> bool:
    return get_state(db).completed_at is not None


def ensure_setup_token(db: Session, settings: Settings) -> str | None:
    """If setup is incomplete, mint a fresh one-time token, store its hash, and return it."""
    state = get_state(db)
    if state.completed_at is not None:
        return None
    token = new_secret(32)
    state.token_hash = sha256_hex(token)
    state.token_expires_at = utcnow() + timedelta(seconds=settings.setup_token_ttl_seconds)
    db.commit()
    return token


def check_setup_token(db: Session, token: str) -> bool:
    state = get_state(db)
    if state.completed_at is not None or not state.token_hash or not state.token_expires_at:
        return False
    if state.token_expires_at <= utcnow():
        return False
    return constant_time_equal(sha256_hex(token), state.token_hash)


def complete_setup(db: Session) -> None:
    state = get_state(db)
    state.completed_at = utcnow()
    state.token_hash = None
    state.token_expires_at = None


def print_setup_banner(token: str, settings: Settings) -> None:
    url = f"{settings.base_url}/setup?token={token}"
    banner = "\n".join(
        [
            "=" * 78,
            "  LECTERN FIRST-RUN SETUP",
            f"  Open this link within {settings.setup_token_ttl_seconds // 60} minutes to create the owner account:",
            f"  {url}",
            "  The token is single-use. Restarting the server issues a new one.",
            "=" * 78,
        ]
    )
    log.warning("\n%s", banner)
