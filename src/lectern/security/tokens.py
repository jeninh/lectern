"""Secret generation and digests for high-entropy, machine-generated credentials.

Human-chosen passwords go through argon2id (see passwords.py). Machine-generated secrets
(session cookies, sync keys, app passwords, setup tokens) carry >= 256 bits of entropy, so a
single SHA-256 is the right store format: brute force is infeasible and lookups stay O(1),
which matters because KOReader presents its key on every request.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

TOKEN_BYTES = 32


def new_secret(nbytes: int = TOKEN_BYTES) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()


def md5_hex(value: str | bytes) -> str:
    """KOReader hashes the sync password with MD5 client-side before sending it."""
    if isinstance(value, str):
        value = value.encode()
    return hashlib.md5(value, usedforsecurity=False).hexdigest()


def sync_key_hash(secret: str) -> str:
    """Stored form of a KOReader sync key: SHA-256 of what the client will actually send."""
    return sha256_hex(md5_hex(secret))


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
