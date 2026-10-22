"""Application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Route

from lectern import __version__, backups, mcp_server
from lectern.config import Settings, get_settings
from lectern.db import init_engine, session_factory
from lectern.ingest.watcher import InboxWatcher
from lectern.jobs import JobRunner, housekeeping
from lectern.lookup import http_get as lookup_http_get
from lectern.migrate import upgrade_to_head
from lectern.readaloud.cache import ReadAloudBuilder
from lectern.routes import (
    api,
    auth,
    health,
    invites,
    koplugin,
    kosync,
    library,
    opds,
    setup,
    spa,
    worker_install,
)
from lectern.security.deps import LoginRequired
from lectern.security.ratelimit import SlidingWindowLimiter
from lectern.security.webauthn import ChallengeStore
from lectern.services import start_services
from lectern.setup_state import ensure_setup_token, print_setup_banner
from lectern.templating import TEMPLATES_DIR
from lectern.uploads import sweep as upload_sweep

log = logging.getLogger("lectern")

STATIC_DIR = TEMPLATES_DIR.parent / "static"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/app/assets/"):
            # Hashed, content-addressed build output; safe to cache hard.
            h["Cache-Control"] = "public, max-age=31536000, immutable"
        elif request.url.path.startswith("/app/pdfjs/"):
            h["Cache-Control"] = "public, max-age=86400"
        else:
            h.setdefault("Cache-Control", "no-store")
        if not request.url.path.startswith(("/opds", "/syncs", "/users")):
            # style-src allows inline styles (cover tints are per-card CSS variables; EPUB
            # stylesheets); scripts stay strictly same-origin, which is also what keeps
            # scripts inside EPUBs from running in the web reader. The reader renders book
            # sections in blob: iframes, so frames, images, fonts, and media may be blob:.
            # frame-ancestors is 'self', not 'none': Safari (iPhone included) applies it to those
            # blob: frames, which inherit this policy, and refused to show any book. Other
            # origins still cannot frame Lectern (and X-Frame-Options stays DENY).
            h.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob:; "
                "style-src 'self' 'unsafe-inline' blob:; font-src 'self' data: blob:; "
                "media-src 'self' blob:; frame-src 'self' blob:; worker-src 'self'; "
                "script-src 'self'; connect-src 'self'; frame-ancestors 'self'; "
                "form-action 'self'; base-uri 'none'",
            )
        settings: Settings = request.app.state.settings
        if settings.is_https:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def create_app(
    settings: Settings | None = None,
    *,
    start_watcher: bool | None = None,
    start_background: bool | None = None,
) -> FastAPI:
    """``start_watcher`` / ``start_background`` override ``LECTERN_INGEST_ENABLED`` /
    ``LECTERN_BACKGROUND_JOBS`` (tests run without threads and drive them by hand)."""
    settings = settings or get_settings()
    _configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # `lectern restore` refuses while any server holds this (shared) lock.
        with backups.server_lock(settings):
            if interrupted := backups.interrupted_restore(settings):
                log.error("Lectern will not start: %s", interrupted)
                raise RuntimeError(f"Lectern will not start: {interrupted}")
            async with _serve(app):
                yield

    @asynccontextmanager
    async def _serve(app: FastAPI):
        engine = init_engine(settings)
        upgrade_to_head(engine)
        settings.resolve_secret_key()
        with session_factory()() as db:
            token = ensure_setup_token(db, settings)
        if token:
            print_setup_banner(token, settings)
        if not settings.is_https and not settings.insecure_http:
            log.warning(
                "LECTERN_BASE_URL is not https; cookies are marked Secure and passkeys will not work. "
                "Set LECTERN_INSECURE_HTTP=1 for local development only."
            )
        with session_factory()() as db:
            upload_sweep(db, settings)
        if start_watcher is None or start_watcher:
            housekeeping(session_factory(), settings)
        watcher = runner = None
        run_watcher = settings.ingest_enabled if start_watcher is None else start_watcher
        if run_watcher:
            settings.library_dir.mkdir(parents=True, exist_ok=True)
            watcher = InboxWatcher(
                settings.inbox_path,
                settings.library_dir,
                session_factory(),
                settings.inbox_poll_seconds,
                settings,
            )
            watcher.start()
            log.info("watching inbox %s", settings.inbox_path)
        # Transcodes and the hourly upload sweep run even with ingest off. Tests that pass
        # start_watcher=False opt out of every background thread.
        if run_watcher or start_watcher is None:
            runner = JobRunner(session_factory(), settings, busy=app.state.readaloud.is_generating)
            runner.start()
        app.state.watcher = watcher
        app.state.jobs = runner
        services: list = []
        run_background = settings.background_jobs if start_background is None else start_background
        if run_background:
            services = start_services(session_factory(), settings)
        app.state.services = services
        app.state.tts = next((s for s in services if type(s).__name__ == "TTSRunner"), None)
        # A fresh MCP session manager per lifespan: each one can run only once.
        mcp_manager = mcp_server.build(settings)
        log.info("Lectern %s ready on %s", __version__, settings.base_url)
        try:
            async with mcp_manager.run():
                app.state.mcp = mcp_manager
                try:
                    yield
                finally:
                    app.state.mcp = None
        finally:
            # Workers first: a running alignment hands its job back before the engine goes.
            for service in reversed(services):
                try:
                    service.stop()
                except Exception:  # noqa: BLE001 - shutdown must reach engine.dispose()
                    log.exception("stopping %s failed", type(service).__name__)
            if watcher:
                watcher.stop()
            if runner:
                runner.stop()
            app.state.readaloud.shutdown()
            engine.dispose()

    app = FastAPI(
        title="Lectern",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.limiters = {
        "login": SlidingWindowLimiter(limit=10, window_seconds=60),
        "setup": SlidingWindowLimiter(limit=5, window_seconds=60),
        "basic": SlidingWindowLimiter(limit=settings.rate_limit_per_minute, window_seconds=60),
        "kosync": SlidingWindowLimiter(limit=settings.rate_limit_per_minute * 2, window_seconds=60),
        "webauthn": SlidingWindowLimiter(limit=20, window_seconds=60),
        # Resumable upload chunks (<= 50 MiB each): generous enough for a LAN, still bounded.
        "uploads": SlidingWindowLimiter(
            limit=settings.rate_limit_per_minute * 4, window_seconds=60
        ),
        # Sleep-timer rewind points that need ffmpeg (cache misses), per user.
        "rewind": SlidingWindowLimiter(limit=20, window_seconds=60),
        # failed worker-credential checks per address, and requests per valid worker key
        "worker_fail": SlidingWindowLimiter(
            limit=settings.rate_limit_per_minute, window_seconds=60
        ),
        "worker": SlidingWindowLimiter(
            limit=settings.worker_requests_per_minute, window_seconds=60
        ),
        # failed enrollment codes per address (POST /api/worker/enroll)
        "worker_enroll": SlidingWindowLimiter(limit=10, window_seconds=60),
        # KOReader plugin (lectern.koplugin): setup-secret exchanges and pairing-code tries per
        # address, tries per username (whatever the address), and codes shown per user.
        "koplugin": SlidingWindowLimiter(limit=10, window_seconds=60),
        "koplugin_pair_user": SlidingWindowLimiter(limit=10, window_seconds=15 * 60),
        "koplugin_code": SlidingWindowLimiter(limit=10, window_seconds=60),
        "koplugin_audit": SlidingWindowLimiter(limit=1, window_seconds=60),
        # Optional metadata suggestions (outbound to Open Library / Google Books), per user.
        "lookup": SlidingWindowLimiter(limit=10, window_seconds=60),
        # AI apps on /mcp: failed AI keys per address, requests per valid key, and tool calls
        # per key within the burst window (over it alerts; see lectern.ai_keys).
        "mcp_fail": SlidingWindowLimiter(limit=settings.rate_limit_per_minute, window_seconds=60),
        "mcp": SlidingWindowLimiter(limit=settings.mcp_requests_per_minute, window_seconds=60),
        "mcp_burst": SlidingWindowLimiter(
            limit=max(1, settings.mcp_burst_count),
            window_seconds=settings.mcp_burst_window_seconds,
        ),
    }
    # The only outbound HTTP Lectern makes; tests swap in a fake upstream.
    app.state.lookup_fetch = lookup_http_get
    app.state.challenges = ChallengeStore()
    app.state.readaloud = ReadAloudBuilder()

    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app_assets = STATIC_DIR / "app" / "assets"
    if app_assets.is_dir():
        app.mount("/app/assets", StaticFiles(directory=str(app_assets)), name="app-assets")
    # pdf.js character maps, standard fonts, and image decoders (vite.config.ts copies them).
    # Without this mount the SPA route answered them with index.html.
    pdfjs_assets = STATIC_DIR / "app" / "pdfjs"
    if pdfjs_assets.is_dir():
        app.mount("/app/pdfjs", StaticFiles(directory=str(pdfjs_assets)), name="app-pdfjs")

    app.state.mcp = None
    # AI access (docs/mcp.md): Streamable HTTP, AI-key bearer auth; nothing else accepts the key.
    app.router.routes.append(Route("/mcp", mcp_server.MCPEndpoint(), include_in_schema=False))
    app.include_router(health.router)
    app.include_router(setup.router)
    app.include_router(auth.router)
    app.include_router(api.router)
    app.include_router(invites.api)
    app.include_router(invites.pages)
    app.include_router(spa.router)
    app.include_router(library.router)
    app.include_router(opds.router)
    app.include_router(kosync.router)
    app.include_router(koplugin.router)
    app.include_router(worker_install.router)

    @app.get("/library", include_in_schema=False)
    @app.get("/account", include_in_schema=False)
    @app.get("/admin/users", include_in_schema=False)
    @app.get("/admin/audit", include_in_schema=False)
    async def _legacy_redirect(request: Request):
        target = {"/library": "/app/library", "/account": "/app/devices"}.get(
            request.url.path,
            "/app/admin" if request.url.path == "/admin/users" else "/app/activity",
        )
        return RedirectResponse(target, status_code=status.HTTP_308_PERMANENT_REDIRECT)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired):
        wants_html = "text/html" in request.headers.get("accept", "")
        if wants_html and request.method == "GET" and not request.url.path.startswith("/api/"):
            nxt = request.url.path
            return RedirectResponse(f"/login?next={nxt}", status_code=status.HTTP_303_SEE_OTHER)
        return JSONResponse(
            {"detail": "Authentication required"}, status_code=status.HTTP_401_UNAUTHORIZED
        )

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"detail": exc.detail}
        return JSONResponse(detail, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        if request.url.path.startswith(("/syncs", "/users")):
            return JSONResponse(
                {"message": "Invalid request"}, status_code=status.HTTP_400_BAD_REQUEST
            )
        # Field, message, and error type only (never the submitted input), so forms can say what
        # to fix and clients can branch on the type (e.g. ``control_characters``).
        errors = [
            {
                "loc": [str(p) for p in e.get("loc", ())][1:],
                "msg": str(e.get("msg", ""))[:200],
                "type": str(e.get("type", ""))[:64],
            }
            for e in exc.errors()[:20]
        ]
        return JSONResponse(
            {"detail": "Invalid request", "errors": errors},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    return app


def app_factory() -> FastAPI:
    return create_app()
