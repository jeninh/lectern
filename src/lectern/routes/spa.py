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


# The installed app's public files (docs/offline.md). The browser fetches the manifest, icons,
# and service worker without the session (and keeps checking the worker after sign-out), and
# none of them holds anything about a user. The worker is never cached by HTTP, so an update
# is seen on the next check.
PUBLIC = {
    "sw.js": ("application/javascript", "no-cache"),
    "manifest.webmanifest": ("application/manifest+json", "public, max-age=3600"),
}


@router.get("/app/sw.js", include_in_schema=False)
@router.get("/app/manifest.webmanifest", include_in_schema=False)
def pwa_file(request: Request):
    name = request.url.path.rsplit("/", 1)[1]
    media_type, cache = PUBLIC[name]
    path = APP_DIR / name
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    headers = {"Cache-Control": cache}
    if name == "sw.js":
        # Scope "/app" (not "/app/") so the worker also controls the app's start page, /app.
        headers["Service-Worker-Allowed"] = "/app"
    return FileResponse(path, media_type=media_type, headers=headers)


@router.get("/app/shell.html", include_in_schema=False)
def shell():
    """The app's HTML without the session check, for the service worker to keep for offline
    use. It is the same file every signed-in page gets and holds no user data; everything
    personal comes from /api afterwards, which still needs the session."""
    if not INDEX.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return FileResponse(INDEX, media_type="text/html", headers={"Cache-Control": "no-cache"})


@router.get("/app/pwa/{name}", include_in_schema=False)
def pwa_image(name: str):
    path = APP_DIR / "pwa" / name
    if "/" in name or not name.endswith((".png", ".svg")) or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    media_type = "image/svg+xml" if name.endswith(".svg") else "image/png"
    return FileResponse(
        path, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"}
    )


@router.get("/app/doc-media/{name}", include_in_schema=False)
def doc_media(auth: Auth, name: str):
    """Screenshots in the Docs tab (docs/screenshots, copied by vite.config.ts), for signed-in
    users. Outside /app/assets so the service worker does not precache them."""
    path = APP_DIR / "doc-media" / name
    if "/" in name or not name.endswith(".png") or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return FileResponse(
        path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"}
    )


@router.get("/app", include_in_schema=False)
@router.get("/app/{path:path}", include_in_schema=False)
def spa(request: Request, auth: Auth, path: str = ""):
    if path.startswith("assets/"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if not INDEX.is_file():
        return HTMLResponse(_MISSING, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return FileResponse(INDEX, media_type="text/html")
