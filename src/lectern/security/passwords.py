"""argon2id hashing for human-chosen passwords."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

# OWASP guidance minimum is m=19 MiB, t=2, p=1; this is a little stronger.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2, type=Type.ID)

MIN_PASSWORD_LENGTH = 12
_DUMMY_HASH = _hasher.hash("dummy-password-for-constant-time")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Verify; when the account has no password, still burn a hash to keep timing flat."""
    target = password_hash or _DUMMY_HASH
    try:
        ok = _hasher.verify(target, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return ok and password_hash is not None


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def validate_password_strength(password: str) -> str | None:
    """Return an error message, or None if acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > 256:
        return "Password is too long."
    if password.strip() != password:
        return "Password must not start or end with whitespace."
    return None
