"""Your full personal-data export (issue #139, lectern.export): one streamed zip of your own
notes, highlights, ratings, reading stats, vocabulary, shelves, and queues. Session only."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from lectern import export
from lectern.db import session_factory
from lectern.security.deps import Auth

router = APIRouter()


@router.get("/export")
def download(auth: Auth) -> StreamingResponse:
    user = auth.user
    day = datetime.now(UTC).date().isoformat()
    return StreamingResponse(
        export.stream_archive(session_factory(), user.id),
        media_type="application/zip",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="lectern-export-{user.id}-{day}.zip"',
        },
    )
