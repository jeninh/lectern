"""Replay-safe writes: the ``Idempotency-Key`` request header.

A client that loses the response to a write (a flaky uplink, a closed laptop) sends the same
request again with the same key and gets the stored answer instead of a second effect. Keys
are scoped to the user and the route, kept for 24 hours, and bound to a digest of the request
body: reusing a key for a different request is ``422 idempotency_key_reused``.

Only successful answers (2xx) are stored; a failed attempt can be retried with the same key.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lectern.models import IdempotencyRecord, utcnow

RETENTION = timedelta(hours=24)
KEY_PATTERN = r"^[A-Za-z0-9._:-]{8,64}$"


def digest(body: object) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def lookup(db: Session, user_id: int, scope: str, key: str | None, body: object) -> dict | None:
    """The stored response for this key, or ``None`` to go ahead."""
    if not key:
        return None
    row = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.user_id == user_id,
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.key == key,
            IdempotencyRecord.created_at >= utcnow() - RETENTION,
        )
    )
    if row is None:
        return None
    if row.request_sha256 != digest(body):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "idempotency_key_reused")
    return row.response


def store(
    db: Session, user_id: int, scope: str, key: str | None, body: object, response: dict
) -> None:
    """Remember ``response`` in the caller's transaction (committed with the effect itself, so
    an effect without a record, or a record without an effect, cannot happen)."""
    if not key:
        return
    db.execute(
        delete(IdempotencyRecord).where(
            IdempotencyRecord.user_id == user_id,
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.key == key,
        )
    )
    db.add(
        IdempotencyRecord(
            user_id=user_id,
            scope=scope,
            key=key,
            request_sha256=digest(body),
            status_code=200,
            response=json.loads(json.dumps(response, default=str)),
        )
    )
    try:
        db.flush()
    except IntegrityError:  # a concurrent request with the same key won the race
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "idempotency_in_progress") from None


def sweep(db: Session) -> int:
    res = db.execute(
        delete(IdempotencyRecord).where(IdempotencyRecord.created_at < utcnow() - RETENTION)
    )
    db.commit()
    return res.rowcount or 0
