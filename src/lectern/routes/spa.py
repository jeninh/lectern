"""Serve the built single-page app (frontend/ -> static/app) behind the session cookie."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from lectern.security.deps import Auth

router = APIRouter()
APP_DIR = Path(__file__).parent.parent / "static" / "app"
INDEX = APP_DIR / "index.html"

_MISSING = """<!doctype html><meta charset="utf-8"><title>Lectern</title>
<body style="font-family:system-ui;padding:2rem;max-width:40rem">
<h1>Lectern UI not built</h1>
<p>The single-page app has not been built into <code>static/app</code>.
Run <code>npm --prefix frontend install &amp;&amp; npm --prefix frontend run build</code>
(the Docker image does this for you).</p></body>"""


@router.get("/", include_in_schema=False)
def root(auth: Auth):
    return RedirectResponse("/app", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/app", include_in_schema=False)
@router.get("/app/{path:path}", include_in_schema=False)
def spa(request: Request, auth: Auth, path: str = ""):
    if path.startswith("assets/"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if not INDEX.is_file():
        return HTMLResponse(_MISSING, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return FileResponse(INDEX, media_type="text/html")
