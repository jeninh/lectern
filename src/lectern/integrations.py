"""Third-party API keys the owner adds (today: ElevenLabs), encrypted at rest.

A key is encrypted with Fernet (AES-128-CBC + HMAC-SHA256) under a key derived with HKDF from
the server's secret key (``LECTERN_SECRET_KEY``, or ``<data>/secret_key`` when unset). The
database therefore never holds a usable key on its own: a copy of ``lectern.db`` without the
secret key reveals nothing. The plaintext is decrypted only in the server process, only when a
request to the provider is about to be made, and is never sent to the browser again: the API
reports ``configured`` and the last four characters.

Changing the secret key makes stored keys undecryptable; they then report ``undecryptable``
and must be entered again. Every change, test, and revocation is audited.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern.config import Settings
from lectern.models import Integration, utcnow

ELEVENLABS = "elevenlabs"
PROVIDERS = (ELEVENLABS,)
_INFO = b"lectern integrations v1"


class Undecryptable(Exception):
    """The stored key was encrypted under another secret key."""


def _fernet(settings: Settings) -> Fernet:
    secret = settings.resolve_secret_key().encode()
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(secret)
    return Fernet(base64.urlsafe_b64encode(key))


def row(db: Session, provider: str) -> Integration | None:
    return db.scalar(select(Integration).where(Integration.provider == provider))


def active(db: Session, provider: str) -> Integration | None:
    r = row(db, provider)
    return r if r is not None and r.revoked_at is None and r.ciphertext else None


def store(db: Session, settings: Settings, provider: str, secret: str, user_id: int) -> Integration:
    """Encrypt and save ``secret`` (replacing any previous key). Caller commits."""
    r = row(db, provider)
    if r is None:
        r = Integration(provider=provider)
        db.add(r)
    r.ciphertext = _fernet(settings).encrypt(secret.encode())
    r.key_hint = secret[-4:]
    r.created_by = user_id
    r.created_at = utcnow()
    r.revoked_at = None
    r.last_tested_at = r.last_test_ok = r.last_test_detail = r.last_used_at = None
    db.flush()
    return r


def secret(db: Session, settings: Settings, provider: str) -> str | None:
    """The decrypted key, or ``None`` when none is configured. Raises :class:`Undecryptable`."""
    r = active(db, provider)
    if r is None:
        return None
    try:
        return _fernet(settings).decrypt(r.ciphertext).decode()
    except InvalidToken as exc:
        raise Undecryptable(provider) from exc


def revoke(db: Session, provider: str) -> bool:
    """Forget the key (the ciphertext is deleted). Caller commits."""
    r = row(db, provider)
    if r is None or r.revoked_at is not None:
        return False
    r.ciphertext = None
    r.revoked_at = utcnow()
    return True


def status(db: Session, settings: Settings, provider: str) -> dict:
    from lectern.routes.api.serializers import iso

    r = row(db, provider)
    configured = r is not None and r.revoked_at is None and bool(r.ciphertext)
    decryptable = None
    if configured:
        try:
            _fernet(settings).decrypt(r.ciphertext)
            decryptable = True
        except InvalidToken:
            decryptable = False
    return {
        "provider": provider,
        "configured": configured,
        "undecryptable": decryptable is False,
        "key_hint": f"…{r.key_hint}" if configured and r.key_hint else None,
        "created_at": iso(r.created_at) if r else None,
        "last_tested_at": iso(r.last_tested_at) if r else None,
        "last_test_ok": r.last_test_ok if r else None,
        "last_test_detail": r.last_test_detail if r else None,
        "last_used_at": iso(r.last_used_at) if r else None,
        "revoked_at": iso(r.revoked_at) if r else None,
    }
