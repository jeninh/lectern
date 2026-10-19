"""KOReader progress sync, wire-compatible with koreader-sync-server.

Authentication is x-auth-user (Lectern username) + x-auth-key (md5 of a per-device sync key,
as KOReader computes it). Progress is stored per user, so every device of a user shares it.
"""

from __future__ import annotations

import re
import time

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from lectern.models import ReadingProgress
from lectern.security.deps import DbDep, KoSync

router = APIRouter()

_DOC_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class ProgressIn(BaseModel):
    document: str = Field(min_length=1, max_length=128)
    progress: str = Field(max_length=4096)
    percentage: float = Field(ge=0.0, le=1.0)
    device: str = Field(default="", max_length=128)
    device_id: str = Field(default="", max_length=128)

    @field_validator("document")
    @classmethod
    def _doc(cls, v: str) -> str:
        if not _DOC_RE.match(v):
            raise ValueError("bad document id")
        return v

    @field_validator("progress", mode="before")
    @classmethod
    def _progress(cls, v):
        # KOReader sends page numbers as integers for PDFs and xpointers as strings for EPUBs.
        return str(v)


@router.post("/users/create", include_in_schema=False)
async def users_create(_request: Request) -> JSONResponse:
    # Registration is closed by policy: credentials are minted in the Lectern web UI.
    # KOReader shows this message verbatim, so keep it actionable.
    return JSONResponse(
        {
            "message": "Registration is disabled. Create a sync key in Lectern (Account page) and use Login."
        },
        status_code=status.HTTP_403_FORBIDDEN,
    )


@router.get("/users/auth", include_in_schema=False)
def users_auth(auth: KoSync) -> JSONResponse:
    return JSONResponse({"authorized": "OK"})


@router.put("/syncs/progress", include_in_schema=False)
def put_progress(auth: KoSync, db: DbDep, body: ProgressIn) -> JSONResponse:
    now = int(time.time())
    row = db.scalar(
        select(ReadingProgress).where(
            ReadingProgress.user_id == auth.user.id, ReadingProgress.document == body.document
        )
    )
    if row is None:
        row = ReadingProgress(
            user_id=auth.user.id, document=body.document, progress="", percentage=0.0, timestamp=now
        )
        db.add(row)
    row.progress = body.progress
    row.percentage = body.percentage
    row.device = body.device
    row.device_id = body.device_id
    row.timestamp = now
    row.sync_key_id = auth.key.id
    db.commit()
    return JSONResponse({"document": body.document, "timestamp": now})


@router.get("/syncs/progress/{document}", include_in_schema=False)
def get_progress(auth: KoSync, db: DbDep, document: str) -> JSONResponse:
    if not _DOC_RE.match(document):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"message": "Invalid document"})
    row = db.scalar(
        select(ReadingProgress).where(
            ReadingProgress.user_id == auth.user.id, ReadingProgress.document == document
        )
    )
    if row is None:
        return JSONResponse({})
    return JSONResponse(
        {
            "document": row.document,
            "progress": row.progress,
            "percentage": row.percentage,
            "device": row.device,
            "device_id": row.device_id,
            "timestamp": row.timestamp,
        }
    )
