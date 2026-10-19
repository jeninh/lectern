from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
def healthz() -> JSONResponse:
    # Deliberately reveals nothing beyond liveness.
    return JSONResponse({"status": "ok"})
