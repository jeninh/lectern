"""AI access: an MCP server at ``/mcp`` (Streamable HTTP) for AI apps on the tailnet.

How a request travels (docs/mcp.md has the user-facing version)::

    AI app ──POST /mcp, Authorization: Bearer lak_…──▶ MCPEndpoint (this module)
        refuse an address with too many failed keys (429)
        look the key up: unknown, revoked, or user disabled → 401
        rate limit per key (LECTERN_MCP_REQUESTS_PER_MINUTE) → 429
        stamp last use; the first use ever alerts the key's user
      ──▶ the MCP SDK's session manager (stateless, JSON responses)
      ──▶ a tool: ``_call`` loads the key again (a revoke or a scope change takes effect
          on the next message; nothing caches scopes), checks its scope, runs the tool *as the key's user* through the same functions the
          web app's routes use (role, visibility, and validation included), writes one audit
          row per call, and counts the call toward the burst alert.

The tools reuse the API, they do not reimplement it: uploads go through
``routes.api.uploads`` and ``routes.api.imports`` (review before filing), sends through
``routes.api.queues`` / ``lectern.device_queue``, shelves through ``routes.api.shelves``,
alignment through ``routes.api.align``, and metadata edits through ``routes.api.editor``
(``WorkPatch`` / ``Metadata`` validation, including the control-character check).

Deliberately absent: any tool that returns a book file, audio, cover, or the read-aloud
EPUB, and any tool for users, invites, keys, or server settings. An AI key is accepted only
here, so it cannot reach ``/api``, ``/library``, or ``/opds`` either.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from lectern import __version__, ai_keys, device_queue, filters, imports, reviews
from lectern.access import can_edit_library, can_see_work, visible_works
from lectern.config import Settings
from lectern.db import session_factory
from lectern.ingest import formats as book_formats
from lectern.models import (
    AIKey,
    AudioTrack,
    Import,
    ImportStatus,
    Job,
    ShelfItem,
    User,
    Work,
    WorkProgress,
)
from lectern.routes.api.serializers import iso
from lectern.security import audit
from lectern.security.deps import AuthContext
from lectern.security.sessions import client_ip
from lectern.uploads import MEDIA_TYPES
from lectern.workmeta import Metadata, authors_of, metadata_of

log = logging.getLogger("lectern.mcp")

SCOPE_KEY = "lectern.ai_key"
# Raw bytes per upload_book chunk. Base64 makes it ~2.7 MiB, inside the SDK's 4 MiB body cap.
CHUNK_BYTES = 2 * 1024 * 1024
_EXTENSIONS = {ext: mt for mt, (exts, _) in MEDIA_TYPES.items() for ext in exts}

INSTRUCTIONS = """\
Lectern is a private household library of books (EPUB, PDF, and CBZ comics) and audiobooks.
You act as one person, with only their permissions and only the scopes their AI key was given.

- Add a book: upload_book (small files in one call; larger ones in chunks), then look at the
  detected metadata and the proposed match, and confirm_import (accept, edit fields, join an
  existing book, or file as new; needs the file scope, and only for imports you uploaded) or cancel_import. Ask the person when
  the match is not certain.
- With the reviews scope only: get_review (this person's private rating, notes, and read
  dates for a book), list_finished (what they finished in a year), and rating: in filters.
- With the separate stats scope only: get_reading_stats (this person's private year review).
- Find books with search_library / list_recent and inspect one with get_book. Book ids are
  numbers; use them for every other tool. search_library takes the Library's filters, e.g.
  format:epub -has:audio author:"Tolstoy" year:1850..1900 progress:<50% status:reading.
- Shelves are nested folders; name one by id or by path from the top level, e.g.
  "Kids/Picture books" (list_shelves). A book can be on several shelves.
- Organize: add_to_shelf / remove_from_shelf, create_shelf / move_shelf,
  send_to_device / list_device_queue,
  start_alignment / job_status, edit_metadata.
- A text PDF can gain an EPUB edition: analyse_pdf_conversion shows the detected rules,
  the person approves or edits them, then convert_pdf_to_epub.
- Only EPUB has text: alignment, read-along, and read-aloud need an EPUB. PDF and comics are
  page-based and cannot be aligned. CBR is not supported; ask the person to convert it to CBZ.
- DRM-free MOBI and AZW3 files are converted to EPUB when uploaded. DRM-protected ones are
  refused (drm_protected): tell the person Lectern can only add DRM-free books, and do not
  suggest ways to remove DRM.
- There is no way to download book files or audio through these tools.
"""

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


# --- the HTTP endpoint ----------------------------------------------------------------------------


def _reply(status: int, detail: str, headers: dict | None = None) -> Response:
    return JSONResponse({"detail": detail}, status_code=status, headers=headers)


_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="Lectern AI tools"'}


def _admit(request: Request, secret: str) -> tuple[int, int | None]:
    """(HTTP status, key id): 200 with the key, or 401/429. Runs in the threadpool."""
    limiters = request.app.state.limiters
    with session_factory()() as db:
        found = ai_keys.authenticate(db, secret) if secret else None
        if found is None:
            limiters["mcp_fail"].allow(f"mcp:{client_ip(request) or '?'}")
            return 401, None
        key, user = found
        if not limiters["mcp"].allow(f"key:{key.id}"):
            return 429, key.id
        ai_keys.record_use(
            db, key, user, client_ip(request), request.headers.get("user-agent", "")[:128]
        )
        return 200, key.id


class MCPEndpoint:
    """ASGI app at ``/mcp``: authenticate, rate limit, then hand over to the MCP SDK."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        request = Request(scope)
        manager = getattr(request.app.state, "mcp", None)
        if manager is None:  # app built without its lifespan (never in production)
            await _reply(503, "unavailable")(scope, receive, send)
            return
        ip_key = f"mcp:{client_ip(request) or '?'}"
        if request.app.state.limiters["mcp_fail"].exhausted(ip_key):
            await _reply(429, "Too many requests", {"Retry-After": "60"})(scope, receive, send)
            return
        secret = ai_keys.bearer(request.headers.get("authorization"))
        code, key_id = await run_in_threadpool(_admit, request, secret)
        if code == 401:
            await _reply(401, "AI key required", _CHALLENGE)(scope, receive, send)
            return
        if code == 429:
            await _reply(429, "Too many requests", {"Retry-After": "60"})(scope, receive, send)
            return
        # Server-side only: the SDK hands this same scope to the tool as its Request.
        scope[SCOPE_KEY] = key_id
        await manager.handle_request(scope, receive, send)


def build(settings: Settings):
    """A fresh MCP server and its Streamable HTTP session manager (one per app lifespan)."""
    server = MCPServer("Lectern", instructions=INSTRUCTIONS, version=__version__)
    for fn, hints in _TOOLS:
        server.add_tool(fn, annotations=hints)
    base = urlsplit(settings.base_url)
    origin = f"{base.scheme}://{base.netloc}"
    # DNS rebinding protection: the configured hostname (the tailnet name behind
    # `tailscale serve`) and loopback for clients on the server itself.
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[base.netloc, "127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=[origin, "http://127.0.0.1:*", "http://localhost:*"],
    )
    server.streamable_http_app(json_response=True, stateless_http=True, transport_security=security)
    return server.session_manager


# --- calling a tool -------------------------------------------------------------------------------


@dataclass
class Call:
    db: Session
    request: Request
    settings: Settings
    user: User
    key: AIKey

    @property
    def auth(self) -> AuthContext:
        # Route functions read only ``.user`` (no session: an AI key has no cookie or CSRF).
        return AuthContext(user=self.user, session=None)  # type: ignore[arg-type]

    @property
    def ip(self) -> str | None:
        return client_ip(self.request)

    def require_editor(self, what: str) -> None:
        if not can_edit_library(self.user):
            raise ToolError(f"forbidden: readers cannot {what}.")

    def has_scope(self, scope: str) -> bool:
        """Read the key's scopes from the database now (a long call, such as the last chunk
        of an upload, may outlive a change its owner made since the call started)."""
        row = self.db.execute(
            select(AIKey.scopes, AIKey.revoked_at).where(AIKey.id == self.key.id)
        ).one_or_none()
        return row is not None and row.revoked_at is None and scope in (row.scopes or [])

    def require_scope(self, scope: str) -> None:
        if not self.has_scope(scope):
            raise ToolError(_needs(scope))

    def may_file(self, imp: Import) -> bool:
        """Checked inside the filing transaction, right before it commits: the key still has
        ``file``, and this key uploaded the import (a key never files what the person, or
        another key, put into review)."""
        return imp.ai_key_id == self.key.id and self.has_scope("file")


def _needs(scope: str) -> str:
    return (
        f"forbidden: needs {scope} scope. This AI key does not have it; its owner can add it "
        "in Lectern under Devices → AI tools → Edit."
    )


def _http_error(exc: HTTPException) -> ToolError:
    detail = exc.detail
    if isinstance(detail, dict):
        code = str(detail.get("detail", "error"))
        extra = {k: v for k, v in detail.items() if k not in ("detail", "current")}
    else:
        code, extra = str(detail), {}
    hint = {
        404: "not found, or not visible to this person",
        403: "not allowed for this person",
        409: "conflict",
        429: "too many requests; wait a minute",
        503: "the server is busy; try the same call again shortly",
    }.get(exc.status_code)
    if code == "drm_protected":
        from lectern.ingest.kindle import DRM_MESSAGE

        hint = DRM_MESSAGE
    msg = f"{code}" + (f" ({hint})" if hint and hint != code else "")
    if extra:
        msg += f": {json.dumps(extra, default=str)[:500]}"
    return ToolError(msg)


def _validation_error(exc: ValidationError) -> ToolError:
    parts = [
        f"{'.'.join(str(p) for p in e.get('loc', ())) or 'value'}: {e.get('msg')} [{e.get('type')}]"
        for e in exc.errors()[:10]
    ]
    return ToolError("invalid: " + "; ".join(parts))


@contextmanager
def _call(ctx: Context, tool: str, scope: str) -> Iterator[Call]:
    request = ctx.request_context.request
    key_id = request.scope.get(SCOPE_KEY) if isinstance(request, Request) else None
    if key_id is None:
        raise ToolError("unauthenticated")
    settings: Settings = request.app.state.settings
    with session_factory()() as db:
        key = db.get(AIKey, key_id)
        user = db.get(User, key.user_id) if key else None
        if key is None or key.revoked_at is not None or user is None or not user.is_active:
            raise ToolError("unauthenticated: this AI key was revoked")
        call = Call(db, request, settings, user, key)
        who = f"key {key.label} #{key.id}"

        def record(outcome: str) -> None:
            audit.record(
                db,
                "mcp.call",
                actor_user_id=user.id,
                target=tool,
                ip=call.ip,
                detail=f"{who}: {outcome}"[:500],
            )
            limiter = request.app.state.limiters["mcp_burst"]
            if settings.mcp_burst_count and not limiter.allow(f"key:{key.id}"):
                ai_keys.burst_alert(
                    db, key, user, call.ip, settings.mcp_burst_count,
                    settings.mcp_burst_window_seconds,
                )  # fmt: skip
            db.commit()

        if scope not in (key.scopes or []):
            record(f"denied (needs {scope} scope)")
            raise ToolError(_needs(scope))
        try:
            yield call
        except ToolError as exc:
            db.rollback()
            record(f"error {str(exc)[:200]}")
            raise
        except (HTTPException, imports.ImportError_) as exc:
            db.rollback()
            if isinstance(exc, imports.ImportError_):
                # lectern.imports raises its own error (not_found, too_many_imports, ...);
                # map it the way routes/api/imports does.
                exc = HTTPException(exc.status, {"detail": exc.code, **exc.extra})
            err = _http_error(exc)
            record(f"error {err}")
            raise err from None
        except ValidationError as exc:
            db.rollback()
            err = _validation_error(exc)
            record(f"error {err}")
            raise err from None
        except Exception:
            db.rollback()
            record("error internal")
            raise
        record("ok")


# --- shapes ---------------------------------------------------------------------------------------


def _format_label(media_type: str) -> str:
    doc = book_formats.DOCUMENTS.get(media_type)
    return doc[1] if doc else "audiobook"


def _formats(work: Work) -> list[str]:
    """``EPUB``, ``PDF``, ``CBZ``, and/or ``audiobook``, in that order."""
    have = {_format_label(f.media_type) for f in work.files}
    order = [label for _ext, label, _code in book_formats.DOCUMENTS.values()] + ["audiobook"]
    return [label for label in order if label in have]


def _summary(work: Work) -> dict:
    return {
        "id": work.id,
        "title": work.title,
        "authors": [a["name"] for a in authors_of(work)],
        "series": work.series,
        "series_index": work.series_index,
        "language": work.language,
        "formats": _formats(work),
        "added_at": iso(work.created_at),
    }


def _import_summary(data: dict) -> dict:
    """An import as the review sheet sees it, minus URLs only a browser session can open."""
    return {
        "import_id": data["id"],
        "status": data["status"],
        "error": data.get("error"),
        "files": [
            {
                "filename": f["filename"],
                "kind": f["kind"],
                "size": f["size"],
                "status": f["status"],
                "duplicate_of": f["duplicate_of"],
            }
            for f in data["files"]
        ],
        "uploads_in_progress": data.get("uploads_active", 0),
        "detected": data.get("detected"),
        "metadata": data.get("metadata"),
        "proposal": data.get("proposal"),
        "target": data.get("target"),
        "filed_book": data.get("work"),
        "auto_filed": data.get("auto_filed"),
        "expires_at": data.get("expires_at"),
    }


def _visible(db: Session, user: User, book_id: int) -> Work:
    work = db.scalar(
        select(Work)
        .options(selectinload(Work.files))
        .where(Work.id == book_id, visible_works(user))
    )
    if work is None:
        raise ToolError(f"not_found: no book {book_id} that this person can see")
    return work


class MetadataChanges(BaseModel):
    """Fields to change; omitted fields keep their value, ``null`` clears one."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    authors: list[str] | None = Field(default=None, description="Display names, in order.")
    sort_title: str | None = None
    series: str | None = None
    series_index: float | None = None
    description: str | None = None
    language: str | None = Field(default=None, description="BCP 47, e.g. en or fr-CA.")
    isbn: str | None = None
    publisher: str | None = None
    published: str | None = Field(default=None, description="YYYY, YYYY-MM, or YYYY-MM-DD.")
    tags: list[str] | None = None
    narrators: list[str] | None = None


def _merged(current: dict | None, changes: MetadataChanges | None) -> Metadata:
    """``current`` with ``changes`` applied, through the editor's own validation."""
    data = dict(current or {})
    if changes is not None:
        for name, value in changes.model_dump(exclude_unset=True).items():
            if name == "authors":
                value = [{"name": n} for n in (value or [])]
            elif name in ("tags", "narrators") and value is None:
                value = []
            data[name] = value
    return Metadata.model_validate(data)


# --- tools: upload and review ---------------------------------------------------------------------


def upload_book(
    ctx: Context,
    filename: Annotated[str, Field(max_length=600, description="File name with extension.")],
    content_base64: Annotated[
        str,
        Field(
            description="This chunk of the file, base64. The whole file when it is at most "
            "2 MiB; otherwise exactly chunk_size bytes per call (the last chunk may be shorter)."
        ),
    ] = "",
    total_size: Annotated[
        int | None, Field(gt=0, description="Whole file size in bytes; needed for chunks.")
    ] = None,
    sha256: Annotated[
        str | None,
        Field(
            pattern="^[0-9a-f]{64}$", description="SHA-256 of the whole file; needed for chunks."
        ),
    ] = None,
    upload_id: Annotated[
        str | None, Field(max_length=32, description="Continue this upload (from the first call).")
    ] = None,
    offset: Annotated[int, Field(ge=0, description="Byte offset of this chunk.")] = 0,
    import_id: Annotated[
        str | None,
        Field(
            max_length=32,
            description="Add the file to this pending import (an EPUB, PDF, or CBZ "
            "and its audiobook, or several audio tracks, become one book).",
        ),
    ] = None,
    finish: Annotated[
        bool,
        Field(
            description="When the file is complete, analyse the import and propose a match. "
            "Set false while more files for the same book are still to come."
        ),
    ] = True,
) -> dict:
    """Upload a book file into "Review before filing": EPUB, PDF, or CBZ comic (.epub, .pdf,
    .cbz; CBR is not supported, convert it to CBZ), a DRM-free MOBI or AZW3 (.mobi, .azw3;
    converted to EPUB, DRM-protected files are refused), or an audiobook (MP3, M4B/M4A,
    OGG/Opus, FLAC). The file's first bytes must match its extension. Nothing is filed yet: when the upload is complete this returns the detected
    metadata and the proposed match; confirm it with confirm_import. Resumable: calling with
    upload_id and no content returns how many bytes the server has. Scope: upload (a certain
    match is filed straight away only if the person turned on auto-filing and the key also
    has the file scope)."""
    from lectern.routes.api import imports as imports_api
    from lectern.routes.api import uploads as uploads_api

    with _call(ctx, "upload_book", "upload") as c:
        c.require_editor("upload books")
        try:
            data = base64.b64decode(content_base64, validate=True) if content_base64 else b""
        except (binascii.Error, ValueError):
            raise ToolError("invalid: content_base64 is not valid base64") from None
        if len(data) > CHUNK_BYTES:
            raise ToolError(f"chunk_too_large: send at most {CHUNK_BYTES} bytes per call")
        if upload_id is None:
            media_type = _EXTENSIONS.get(Path(filename).suffix.lower())
            if media_type is None:
                raise ToolError(
                    "unsupported_media_type: use .epub, .pdf, .cbz, .mobi, .azw3, .mp3, .m4b, "
                    ".m4a, .ogg, .opus, or .flac (CBR is not supported: convert it to CBZ)"
                )
            size = total_size or len(data)
            if not size:
                raise ToolError("invalid: empty file")
            if size > len(data) and not sha256:
                raise ToolError("invalid: a chunked upload needs sha256 of the whole file")
            whole = sha256 or hashlib.sha256(data).hexdigest()
            if import_id is None:
                # Not committed here: start_session commits it together with the upload
                # session, so a refused upload (too_large, quota, ...) leaves no import.
                imp, created = imports.create(c.db, c.user, Path(filename).stem[:256], None)
                imp.ai_key_id = c.key.id
            else:
                imp, created = imports.get_owned(c.db, c.user, import_id), False
                _own_import(c, imp)
            row, _ = uploads_api.start_session(
                c.db,
                c.request,
                c.auth,
                c.settings,
                uploads_api.CreateIn(
                    filename=filename,
                    size=size,
                    media_type=media_type,
                    sha256=whole,
                    import_id=imp.public_id,
                ),  # fmt: skip
                chunk_max=CHUNK_BYTES,
            )
            upload_id = row.public_id
        else:
            created = False
            row = uploads_api._own(c.db, c.auth, upload_id)
            _own_import(c, c.db.get(Import, row.import_id) if row.import_id else None)
        try:
            uploads_api._open_session(c.db, c.auth, c.settings, upload_id)
            if data:
                uploads_api._store_chunk(
                    c.db,
                    c.auth,
                    c.settings,
                    upload_id,
                    offset,
                    hashlib.sha256(data).hexdigest(),
                    data,
                )
            row = uploads_api._own(c.db, c.auth, upload_id)
            imp = c.db.get(Import, row.import_id) if row.import_id else None
            if row.received_bytes < row.size:
                return {
                    "status": "uploading",
                    "upload_id": row.public_id,
                    "import_id": imp.public_id if imp else None,
                    "size": row.size,
                    "received_bytes": row.received_bytes,
                    "next_offset": row.received_bytes,
                    "chunk_size": row.chunk_size,
                }
            # The scopes may have changed since this upload started (or since this call did).
            c.require_scope("upload")
            done = uploads_api.finalize(row.public_id, c.request, c.auth, c.db, c.settings, None)
            assert imp is not None  # MCP uploads always stage into an import
            if not finish:
                out = _import_summary(done["import"])
                return {
                    **out,
                    "status": "uploaded",
                    "next": "upload more files with this import_id",
                }
            result = imports_api.complete(
                c.request, c.auth, c.db, c.settings, imp.public_id,
                allowed=lambda: c.may_file(imp),
            )  # fmt: skip
            return _import_summary(result)
        except Exception:
            if created:
                # This call opened the import and the upload failed in it (for example a
                # checksum or format error on a one-shot upload): nothing can resume it.
                c.db.rollback()
                imp = imports.get_owned(c.db, c.user, imp.public_id)
                if imp.status in imports.OPEN:
                    imports.discard(c.db, c.settings, imp)
                    c.db.commit()
                    imports.cleanup_staging(c.settings, imp)
            raise


def _own_import(c: Call, imp: Import | None) -> None:
    """An AI key adds files only to imports it opened: otherwise one key could slip content
    into another (filing) key's import, or into the person's own uploads."""
    if imp is None or imp.ai_key_id != c.key.id:
        raise ToolError(
            "forbidden: this import was not opened by this AI key. Start a new upload "
            "without import_id."
        )


def list_pending_imports(ctx: Context) -> dict:
    """Uploads waiting in "Review before filing" (this person's own), with what was detected
    and the proposed match. Scope: upload."""
    from lectern.routes.api import imports as imports_api

    with _call(ctx, "list_pending_imports", "upload") as c:
        c.require_editor("add books")
        found = imports_api.list_imports(c.auth, c.db, c.settings, status_="open")
        return {"imports": [_import_summary(i) for i in found["imports"]]}


def confirm_import(
    ctx: Context,
    import_id: Annotated[str, Field(max_length=32)],
    action: Annotated[
        Literal["accept", "new", "join"],
        Field(
            description="accept: file as proposed; new: file as a new book; join: add the "
            "files to the existing book work_id (only its empty fields are filled)."
        ),
    ] = "accept",
    work_id: Annotated[int | None, Field(description="The book to join (action=join).")] = None,
    metadata: Annotated[
        MetadataChanges | None, Field(description="Corrections to the detected metadata.")
    ] = None,
    use_cover: Annotated[bool | None, Field(description="Use the detected cover.")] = None,
    add_audio_tracks: Annotated[
        bool,
        Field(
            description="Joining audio to a book that already has an audiobook appends the "
            "tracks; without this it is refused so two audiobooks never mix by accident."
        ),
    ] = False,
) -> dict:
    """File a pending import into the library, as reviewed. Only imports this AI key uploaded
    with upload_book; others are filed by the person in Lectern. Scope: file."""
    from lectern.routes.api import imports as imports_api

    with _call(ctx, "confirm_import", "file") as c:
        c.require_editor("add books")
        imp = imports.get_owned(c.db, c.user, import_id)
        if imp.ai_key_id != c.key.id:
            raise ToolError(
                "forbidden: this import was not uploaded by this AI key. Only the person can "
                "file it, in Lectern's review sheet."
            )
        if imp.status == ImportStatus.UPLOADING:
            imports_api.complete(c.request, c.auth, c.db, c.settings, import_id, auto_file=False)
            c.db.refresh(imp)
        if action == "join" and (work_id is None or not can_see_work(c.db, c.user, work_id)):
            raise ToolError("invalid: action=join needs the work_id of a book you can see")
        target = (
            None
            if action == "accept"
            else imports.Target(action=action, work_id=work_id if action == "join" else None)
        )
        decision = imports.Decision(
            metadata=_merged(imp.metadata_, metadata) if metadata is not None else None,
            target=target,
            use_cover=use_cover,
            audio="append" if add_audio_tracks else "refuse",
        )
        try:
            out = imports_api.file_decision(
                c.request, c.auth, c.db, c.settings, import_id, decision,
                allowed=lambda: c.may_file(imp),
            )  # fmt: skip
        except HTTPException as exc:
            if isinstance(exc.detail, dict) and exc.detail.get("detail") == "needs_file_scope":
                raise ToolError(_needs("file")) from None
            raise
        work = _visible(c.db, c.user, out["work_id"])
        return {
            "book": _summary(work),
            "created": bool(out["report"].get("created")),
            "report": out["report"],
        }


def cancel_import(ctx: Context, import_id: Annotated[str, Field(max_length=32)]) -> dict:
    """Discard a pending import and its uploaded files (nothing in the library changes).
    Scope: upload."""
    from lectern.routes.api import imports as imports_api

    with _call(ctx, "cancel_import", "upload") as c:
        c.require_editor("add books")
        imports_api.discard_import(c.request, c.auth, c.db, c.settings, import_id)
        return {"import_id": import_id, "status": "discarded"}


# --- tools: read ----------------------------------------------------------------------------------


def search_library(
    ctx: Context,
    query: Annotated[
        str,
        Field(
            max_length=1000,
            description="Words from the title, author, series, or ISBN, and filters in the "
            "Library's syntax, e.g. 'format:epub,pdf -has:audio author:\"Tolstoy\" "
            'year:1850..1900 progress:<50% shelf:"Kids/Picture books"\'. Fields: '
            + ", ".join(filters.FIELDS)
            + ". Empty lists everything.",
        ),
    ] = "",
    sort: Annotated[
        str,
        Field(pattern=filters.SORT_PATTERN, description="One of: " + ", ".join(filters.SORTS)),
    ] = "title",
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict:
    """Find books this person can see, with the Library's filters (docs/filters.md): a
    leading - negates a filter, a,b matches either value, and numbers, sizes, lengths, and
    dates take <, >, or a..b. Scope: read; rating: and sorting by rating also need reviews,
    and without it started:/finished: use only the dates Lectern recorded, never hand-entered
    ones."""
    with _call(ctx, "search_library", "read") as c:
        private = c.has_scope("reviews")
        if sort == "rating" and not private:
            raise ToolError(_needs("reviews"))
        try:
            q = filters.parse(query)
            where = filters.predicate(filters.Context(c.db, c.user, private_reviews=private), q)
        except filters.FilterError as exc:
            raise ToolError(f"invalid_filter: {exc.message}") from None
        stmt = select(Work).options(selectinload(Work.files)).where(where)
        total = c.db.scalar(select(func.count()).select_from(stmt.subquery()))
        works = c.db.scalars(
            stmt.order_by(*filters.order_by(sort, c.user, private_reviews=private)).limit(limit)
        ).all()
        return {
            "total": total or 0,
            "filter": filters.format_query(q),
            "books": [_summary(w) for w in works],
        }


def get_book(ctx: Context, book_id: int) -> dict:
    """One book: metadata, formats, audiobook length, alignment status, shelves, and this
    person's reading or listening progress. Scope: read."""
    from lectern.align import jobs
    from lectern.progress import progress_json

    with _call(ctx, "get_book", "read") as c:
        work = _visible(c.db, c.user, book_id)
        tracks = c.db.execute(
            select(func.count(), func.sum(AudioTrack.duration_s)).where(
                AudioTrack.work_id == work.id
            )
        ).one()
        active = jobs.active_job(c.db, work.id)
        pj = progress_json(c.db, c.user.id, work.id)
        on = set(c.db.scalars(select(ShelfItem.shelf_id).where(ShelfItem.work_id == work.id)))
        shelves = [_shelf_ref(s) for s in _shelves(c) if s["id"] in on]
        return {
            "id": work.id,
            "version": work.version,
            "metadata": metadata_of(work),
            "formats": [
                {
                    "kind": f.kind.value,
                    "format": _format_label(f.media_type),
                    "media_type": f.media_type,
                    "size": f.size,
                }
                for f in work.files
            ],
            "audio": {"tracks": tracks[0], "duration_s": round(float(tracks[1] or 0), 1)}
            if tracks[0]
            else None,
            "alignment": jobs.alignment_json(
                c.db, jobs.work_alignment(c.db, work.id), detailed=can_edit_library(c.user)
            ),
            "alignment_job": {"id": active.id, "status": active.status.value} if active else None,
            "progress": {
                "percent": pj["percent"],
                "source": pj["source"],
                "device": pj["device"],
                "audio_s": pj["audio_s"],
                "updated_at": pj["received_at"],
            }
            if pj["source"]
            else None,
            "shelves": shelves,
            "added_at": iso(work.created_at),
            "updated_at": iso(work.updated_at),
        }


def list_recent(
    ctx: Context,
    kind: Annotated[
        Literal["added", "read"],
        Field(
            description="added: newest in the library; read: what this person read or "
            "listened to most recently."
        ),
    ] = "added",
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict:
    """Recently added books, or this person's recently read ones. Scope: read."""
    with _call(ctx, "list_recent", "read") as c:
        stmt = select(Work).options(selectinload(Work.files)).where(visible_works(c.user))
        if kind == "added":
            works = c.db.scalars(stmt.order_by(Work.created_at.desc(), Work.id.desc()).limit(limit))
            return {"books": [_summary(w) for w in works]}
        rows = c.db.execute(
            stmt.add_columns(WorkProgress.received_at)
            .join(WorkProgress, WorkProgress.work_id == Work.id)
            .where(WorkProgress.user_id == c.user.id)
            .order_by(WorkProgress.received_at.desc())
            .limit(limit)
        ).all()
        return {"books": [{**_summary(w), "last_read_at": iso(at)} for w, at in rows]}


# --- tools: reviews -------------------------------------------------------------------------------


def get_review(ctx: Context, book_id: int) -> dict:
    """This person's own private rating (stars, ½ to 5), Markdown notes, and read-throughs
    (the days they started and finished each read) of one book. Scope: reviews."""
    with _call(ctx, "get_review", "reviews") as c:
        _visible(c.db, c.user, book_id)
        out = reviews.review_json(c.db, c.user, book_id)
        return {k: out[k] for k in ("rating", "notes", "read_throughs", "finished")}


def list_finished(ctx: Context, year: Annotated[int, Field(ge=1900, le=2999)]) -> dict:
    """The books this person finished in a year, by date, with their own ratings and the dates
    as they corrected them. A re-read counts each time. Scope: reviews."""
    with _call(ctx, "list_finished", "reviews") as c:
        items = reviews.finished_in(c.db, c.user, year)
        return {"year": year, "count": len(items), "books": items}


def get_reading_stats(ctx: Context, year: Annotated[int, Field(ge=1900, le=2999)]) -> dict:
    """This person's private reading and listening totals, streaks, and year review. It excludes
    books they can no longer see. Scope: stats (separate opt-in; not covered by read or reviews)."""
    with _call(ctx, "get_reading_stats", "stats") as c:
        from lectern import reading_stats

        return reading_stats.summary(c.db, c.user, year)


# --- tools: organize ------------------------------------------------------------------------------


def _shelves(c: Call) -> list[dict]:
    from lectern.routes.api import shelves as shelves_api

    return shelves_api.Tree(c.db, c.user, private_reviews=c.has_scope("reviews")).list_json()[
        "shelves"
    ]


def _path(s: dict) -> str:
    return "/".join(s["path"])


def _segments(path: str) -> list[str]:
    return [p.strip() for p in path.strip().strip("/").split("/") if p.strip()]


def _shelf(c: Call, shelf: str) -> dict:
    """A shelf this person sees, by id, by path from their top level ("Kids/Picture
    books"), or by a name that only one of their shelves has."""
    found = _shelves(c)
    wanted = shelf.strip()
    hits = [s for s in found if str(s["id"]) == wanted]
    if not hits:
        parts = _segments(wanted)
        folded = [p.casefold() for p in parts]
        hits = [s for s in found if [n.casefold() for n in s["path"]] == folded]
        if not hits and len(parts) == 1:
            hits = [s for s in found if s["name"].casefold() == folded[0]]
    if len(hits) != 1:
        listed = hits or found
        names = ", ".join(f"{_path(s)} (#{s['id']})" for s in listed[:50]) or "none"
        reason = "ambiguous_shelf" if hits else "not_found"
        raise ToolError(f"{reason}: no single shelf '{shelf}'. Shelves: {names}")
    return hits[0]


def _shelf_ref(s: dict) -> dict:
    return {"id": s["id"], "name": s["name"], "path": _path(s)}


def list_shelves(ctx: Context) -> dict:
    """Every shelf (folder) this person sees, as paths from their top level ("Kids/Picture
    books"), with how many books are directly on each and what they may do there
    (owner: manage, edit: add and remove books and make sub-shelves, view). "auto" is null, or
    the kind ("group", "bucket", "value") of a library-generated shelf: those are read-only,
    so adding to, removing from, or changing one is refused (auto_shelf). Scope: read."""
    with _call(ctx, "list_shelves", "read") as c:
        return {
            "shelves": [
                {
                    **_shelf_ref(s),
                    "parent_id": s["parent_id"],
                    "books": s["work_count"],
                    "sub_shelves": s["shelf_count"],
                    "permission": s["permission"],
                    # A smart shelf's books are a saved filter's matches (search_library
                    # with shelf:"<path>" lists them); nothing is added to it by hand.
                    "smart": s["smart"],
                    **({"filter": s["filter"]} if "filter" in s else {}),
                    "auto": s["auto"]["kind"] if s.get("auto") else None,
                }
                for s in _shelves(c)
            ]
        }


ShelfArg = Annotated[
    str,
    Field(
        max_length=1200,
        description='Shelf id, path from the top level ("Kids/Picture books"), or a name '
        "only one shelf has.",
    ),
]


def add_to_shelf(ctx: Context, book_id: int, shelf: ShelfArg) -> dict:
    """Put a book on a shelf this person may edit (it stays on its other shelves).
    Scope: organize."""
    from lectern.routes.api import shelves as shelves_api

    with _call(ctx, "add_to_shelf", "organize") as c:
        s = _shelf(c, shelf)
        _visible(c.db, c.user, book_id)
        shelves_api.add_item(c.request, c.auth, c.db, s["id"], shelves_api.ItemIn(work_id=book_id))
        return {"shelf": _shelf_ref(s), "book_id": book_id, "on_shelf": True}


def remove_from_shelf(ctx: Context, book_id: int, shelf: ShelfArg) -> dict:
    """Take a book off a shelf this person may edit (the book stays in the library).
    Scope: organize."""
    from lectern.routes.api import shelves as shelves_api

    with _call(ctx, "remove_from_shelf", "organize") as c:
        s = _shelf(c, shelf)
        _visible(c.db, c.user, book_id)
        shelves_api.remove_item(c.request, c.auth, c.db, s["id"], book_id)
        return {"shelf": _shelf_ref(s), "book_id": book_id, "on_shelf": False}


def create_shelf(
    ctx: Context,
    path: Annotated[
        str,
        Field(
            max_length=1200,
            description='Where and what: "Kids" for a top-level shelf, "Kids/Picture books" '
            "for one inside Kids. Missing shelves along the path are created too.",
        ),
    ],
) -> dict:
    """Make a shelf (folder), nested at most 8 deep. Top-level shelves need an owner or
    member; inside a shelf, edit permission there. Names cannot contain "/".
    Scope: organize."""
    from lectern.routes.api import shelves as shelves_api

    with _call(ctx, "create_shelf", "organize") as c:
        parts = _segments(path)
        if not parts:
            raise ToolError("invalid: give a shelf name or path")
        parent: dict | None = None
        created: list[str] = []
        for i, name in enumerate(parts):
            here = [n.casefold() for n in parts[: i + 1]]
            existing = [s for s in _shelves(c) if [n.casefold() for n in s["path"]] == here]
            if len(existing) > 1:
                raise ToolError(
                    f"ambiguous_shelf: several shelves are '{'/'.join(parts[: i + 1])}'"
                )
            if existing:
                parent = existing[0]
                continue
            try:
                body = shelves_api.CreateIn(name=name, parent_id=parent["id"] if parent else None)
            except ValidationError as exc:
                raise ToolError(f"invalid: {exc.errors()[0]['msg']}") from None
            parent = shelves_api.create_shelf(c.request, c.auth, c.db, body)
            created.append(_path(parent))
        if not created:
            raise ToolError(f"duplicate_name: '{_path(parent)}' already exists (#{parent['id']})")
        return {"shelf": _shelf_ref(parent), "created": created}


def move_shelf(
    ctx: Context,
    shelf: ShelfArg,
    to: Annotated[
        str,
        Field(
            max_length=1200,
            description='The shelf to move it into (id or path); "" or "/" for the top level.',
        ),
    ],
) -> dict:
    """Move a shelf, with its sub-shelves and books, into another shelf or to the top level.
    Needs the right to manage the shelf and to make shelves where it goes; never into itself
    or below itself, and at most 8 deep. Scope: organize."""
    from lectern.routes.api import shelves as shelves_api

    with _call(ctx, "move_shelf", "organize") as c:
        s = _shelf(c, shelf)
        dest = None if not _segments(to) else _shelf(c, to)
        body = shelves_api.PatchIn(parent_id=dest["id"] if dest else None)
        moved = shelves_api.update_shelf(c.request, c.auth, c.db, s["id"], body)
        if not isinstance(moved, dict):  # moved out of everything this key may see
            return {"shelf": {"id": s["id"]}, "parent": None, "visible": False}
        return {"shelf": _shelf_ref(moved), "parent": _shelf_ref(dest) if dest else None}


def _device(c: Call, device: str) -> device_queue.Target:
    found = device_queue.targets(c.db, c.user)
    wanted = device.strip()
    hits = [t for t in found if str(t.pw.id) == wanted] or [
        t for t in found if t.pw.label.casefold() == wanted.casefold()
    ]
    if len(hits) != 1:
        names = ", ".join(f"{t.pw.label} (#{t.pw.id})" for t in found) or "none"
        reason = "ambiguous_device" if hits else "not_found"
        raise ToolError(f"{reason}: no single device '{device}'. Devices: {names}")
    return hits[0]


def _queue_device(t: device_queue.Target, viewer: User, pending: int) -> dict:
    return {
        "id": t.pw.id,
        "label": t.pw.label,
        "owner": t.owner.username,
        "own": t.owner.id == viewer.id,
        "last_used_at": iso(t.pw.last_used_at),
        "pending": pending,
    }


def send_to_device(
    ctx: Context,
    book_ids: Annotated[list[int], Field(min_length=1, max_length=device_queue.MAX_BATCH)],
    device: Annotated[str, Field(max_length=128, description="Device name or id.")],
) -> dict:
    """Queue books for an e-reader (its OPDS "Send to device" feed). The reader picks them up
    the next time it opens its queue. Scope: organize."""
    from lectern.routes.api import queues

    with _call(ctx, "send_to_device", "organize") as c:
        t = _device(c, device)
        out = queues.enqueue(c.request, c.auth, c.db, t.pw.id, queues.EnqueueIn(work_ids=book_ids))
        return {
            "device": _queue_device(t, c.user, out["device"]["pending"]),
            "results": out["results"],
        }


def list_device_queue(
    ctx: Context,
    device: Annotated[
        str | None,
        Field(max_length=128, description="Device name or id; omit to list the devices."),
    ] = None,
) -> dict:
    """The e-readers this person can send to, or one device's queue: pending books in order
    and recently delivered or removed ones. Scope: organize."""
    from lectern.routes.api import queues

    with _call(ctx, "list_device_queue", "organize") as c:
        if device is None:
            listed = queues.list_targets(c.auth, c.db)["devices"]
            return {
                "devices": [
                    {k: d[k] for k in ("id", "label", "own", "last_used_at", "pending")}
                    | {"owner": d["user"]["username"]}
                    for d in listed
                ]
            }
        t = _device(c, device)
        shown = queues.show(c.auth, c.db, t.pw.id)

        def item(i: dict) -> dict:
            return {
                "book": {k: i["work"][k] for k in ("id", "title", "author")},
                "status": i["status"],
                "queued_at": i["queued_at"],
                "queued_by": i["queued_by"],
                "delivered_at": i["delivered_at"],
            }

        return {
            "device": _queue_device(t, c.user, len(shown["pending"])),
            "pending": [item(i) for i in shown["pending"]],
            "history": [item(i) for i in shown["history"][:20]],
        }


def start_alignment(
    ctx: Context,
    book_id: int,
    model: Annotated[
        str | None, Field(max_length=64, description="Whisper model; default from the server.")
    ] = None,
    force: Annotated[bool, Field(description="Align again even if it is up to date.")] = False,
) -> dict:
    """Queue text–audio alignment for a book with both an EPUB and an audiobook (enables
    read-along). PDF and CBZ have no text and cannot be aligned. Returns the job; follow it with job_status. Scope: organize."""
    from lectern.routes.api import align

    with _call(ctx, "start_alignment", "organize") as c:
        c.require_editor("start alignment")
        _visible(c.db, c.user, book_id)
        job = align.request_align(
            c.request, c.auth, c.db, c.settings, Response(), book_id,
            align.AlignIn(model=model, force=force),
        )  # fmt: skip
        return {"job": job}


def job_status(
    ctx: Context,
    job_id: int | None = None,
    book_id: Annotated[int | None, Field(description="The book's latest alignment job.")] = None,
) -> dict:
    """An alignment job's state (queued, running with progress and ETA, done, failed) and the
    book's alignment. Give job_id or book_id. Scope: organize."""
    from lectern import jobqueue
    from lectern.align import jobs

    with _call(ctx, "job_status", "organize") as c:
        c.require_editor("see alignment jobs")
        if job_id is not None:
            job = c.db.get(Job, job_id)
        elif book_id is not None:
            _visible(c.db, c.user, book_id)
            job = c.db.scalar(select(Job).where(Job.work_id == book_id).order_by(Job.id.desc()))
        else:
            raise ToolError("invalid: give job_id or book_id")
        if (
            job is None
            or (job.work_id is None and not jobqueue.may_see(job, c.user))
            or (job.work_id is not None and not can_see_work(c.db, c.user, job.work_id))
        ):
            raise ToolError("not_found: no such job")
        alignment = jobs.work_alignment(c.db, job.work_id) if job.work_id else None
        # the Jobs page's rule (lectern.jobqueue.shows_error): a job with a book got here only
        # if the book is visible; one with no book shows its error to the owner alone
        show_error = jobqueue.shows_error(c.user, job.work_id is not None)
        return {
            "job": jobs.job_json(c.db, job, show_error=show_error),
            "alignment": jobs.alignment_json(c.db, alignment),
        }


def edit_metadata(
    ctx: Context,
    book_id: int,
    changes: MetadataChanges,
    expected_version: Annotated[
        int | None,
        Field(
            ge=1,
            description="The version from get_book; the edit is refused if the book "
            "changed since (omit to edit the current version).",
        ),
    ] = None,
) -> dict:
    """Change a book's metadata with the editor's validation (lengths, language and date
    formats, no control characters). Undo is in the web editor's history. Scope: organize."""
    from lectern.editing import WorkPatch
    from lectern.routes.api import editor

    with _call(ctx, "edit_metadata", "organize") as c:
        c.require_editor("edit books")
        work = _visible(c.db, c.user, book_id)
        patch = WorkPatch(
            version=expected_version or work.version,
            metadata=_merged(metadata_of(work), changes),
        )
        out = editor.patch_work(c.request, c.auth, c.db, c.settings, book_id, patch)
        fresh = _visible(c.db, c.user, book_id)
        return {
            "book_id": book_id,
            "changed": out["changed"],
            "version": fresh.version,
            "metadata": metadata_of(fresh),
        }


def analyse_pdf_conversion(
    ctx: Context,
    book_id: int,
    pages: Annotated[
        list[Annotated[int, Field(ge=1, le=10_000)]] | None,
        Field(max_length=6, description="Pages whose converted text to show (1-based)."),
    ] = None,
) -> dict:
    """Step 1 of PDF → EPUB conversion: analyse a book's text PDF. If no current analysis
    exists this queues one and returns the job (call again when job_status says it is done).
    Otherwise returns the detected rules (page numbers, chapters, headings, footnotes,
    figures, tables, maths, low-confidence pages to keep as images, front/back matter) with
    their evidence, and the converted text of `pages`. Show the rules to the person; then
    convert_pdf_to_epub with the rules they approve. Scope: organize."""
    from lectern.routes.api import pdf2epub as routes

    with _call(ctx, "analyse_pdf_conversion", "organize") as c:
        c.require_editor("convert books")
        work = _visible(c.db, c.user, book_id)
        if routes._analysis(c.db, c.settings, work) is None:
            return {
                "analysed": False,
                **routes.analyse(c.request, c.auth, c.db, c.settings, book_id),
            }
        out = routes.analysis(c.auth, c.db, c.settings, book_id)
        out.pop("zones", None)
        out["pages"] = [p for p in out["pages"] if p.get("flagged")][:50]
        if pages:
            out["preview"] = routes.preview(
                c.auth, c.db, c.settings, book_id, routes.PreviewIn(pages=pages)
            )["pages"]
        return {"analysed": True, **out}


def convert_pdf_to_epub(
    ctx: Context,
    book_id: int,
    rules: Annotated[
        dict | None,
        Field(
            description="The rules from analyse_pdf_conversion, edited as approved; omit "
            "for the detected defaults."
        ),
    ] = None,
    reconvert: Annotated[bool, Field(description="Replace an existing converted edition.")] = False,
) -> dict:
    """Step 2: convert a book's text PDF into a separate reflowable EPUB edition with the
    approved rules (the PDF stays). Returns the job; follow it with job_status. Scope:
    organize."""
    from lectern.pdf2epub.rules import Rules
    from lectern.routes.api import pdf2epub as routes

    with _call(ctx, "convert_pdf_to_epub", "organize") as c:
        c.require_editor("convert books")
        _visible(c.db, c.user, book_id)
        body = routes.ConvertIn(
            rules=Rules.model_validate(rules) if rules is not None else None, reconvert=reconvert
        )
        return routes.convert(c.request, c.auth, c.db, c.settings, book_id, body)


_TOOLS: list[tuple[Any, ToolAnnotations]] = [
    (upload_book, WRITE),
    (list_pending_imports, READ),
    (confirm_import, WRITE),
    (cancel_import, DESTRUCTIVE),
    (search_library, READ),
    (get_book, READ),
    (list_recent, READ),
    (get_review, READ),
    (list_finished, READ),
    (get_reading_stats, READ),
    (list_shelves, READ),
    (add_to_shelf, WRITE),
    (remove_from_shelf, DESTRUCTIVE),
    (create_shelf, WRITE),
    (move_shelf, WRITE),
    (send_to_device, WRITE),
    (list_device_queue, READ),
    (start_alignment, WRITE),
    (job_status, READ),
    (edit_metadata, WRITE),
    (analyse_pdf_conversion, WRITE),
    (convert_pdf_to_epub, WRITE),
]
