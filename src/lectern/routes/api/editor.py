"""Editing works, trash, bulk edits, metadata suggestions, and library settings.

Owners and members only; readers get ``403`` on every write here and never see the editor.
Session + CSRF like the rest of ``/api``. ``PATCH /api/works/{id}``, bulk, and trash writes
accept an ``Idempotency-Key`` header. The logic lives in ``lectern.editing``,
``lectern.trash``, and ``lectern.lookup``; these handlers translate, lock, audit, and commit.
"""

from __future__ import annotations

import logging
import secrets
from contextlib import contextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from lectern import covergen, editing, idempotency, instance, lookup, trash
from lectern.access import can_see_work
from lectern.align import jobs as align_jobs
from lectern.align.transcribe import DEFAULT_MODEL, MODELS
from lectern.editing import EditError, Journal, WorkPatch
from lectern.fsops import LIBRARY_LOCK
from lectern.ingest import formats
from lectern.models import (
    Alignment,
    Role,
    ShelfItem,
    TrashedFile,
    User,
    Work,
    WorkRevision,
    utcnow,
)
from lectern.routes.api.selection import Selection
from lectern.routes.api.selection import resolve as resolve_selection
from lectern.routes.api.serializers import iso, work_json
from lectern.security import audit
from lectern.security.deps import (
    AuthContext,
    DbDep,
    OwnerOnly,
    SettingsDep,
    require_role,
    require_role_read,
)
from lectern.security.sessions import client_ip
from lectern.workmeta import AuthorIn, NoControls, metadata_of

router = APIRouter()
log = logging.getLogger("lectern.editor")

Editor = Annotated[AuthContext, Depends(require_role(Role.OWNER, Role.MEMBER))]
EditorRead = Annotated[AuthContext, Depends(require_role_read(Role.OWNER, Role.MEMBER))]
IdemKey = Annotated[str | None, Header(alias="idempotency-key", pattern=idempotency.KEY_PATTERN)]


def _edit_error(exc: EditError) -> HTTPException:
    return HTTPException(exc.status, {"detail": exc.code, **exc.extra})


@contextmanager
def _rollback_on_error(db, journal: Journal):
    """One transaction with file steps: if anything (the commit included) fails, the database
    rolls back and the journal puts the files back where the database says they are."""
    try:
        yield
    except BaseException:
        db.rollback()
        journal.rollback()
        raise


def _busy(request: Request):
    builder = request.app.state.readaloud
    return builder.is_generating


def _work(db, user: User, work_id: int) -> Work:
    work = db.scalar(select(Work).options(selectinload(Work.files)).where(Work.id == work_id))
    if work is None or not can_see_work(db, user, work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return work


def _audit(db, request, auth, action: str, target: str, detail: str | None = None) -> None:
    audit.record(
        db,
        action,
        actor_user_id=auth.user.id,
        target=target[:256],
        ip=client_ip(request),
        detail=detail[:500] if detail else None,
    )


# --- editor state ---------------------------------------------------------------------------------


def editor_json(db, settings, request: Request, user: User, work: Work) -> dict:
    from lectern.ingest.sentences import primary_epub
    from lectern.routes.api.readalong import read_aloud_status

    db.expire(work, ["files"])
    primary = primary_epub(db, work.id)
    tracks = editing.ordered_tracks(db, work.id)
    on = set(db.scalars(select(ShelfItem.shelf_id).where(ShelfItem.work_id == work.id)))
    editable = editing.editable_shelves(db, user)
    last = db.scalar(
        select(WorkRevision)
        .where(WorkRevision.work_id == work.id)
        .order_by(WorkRevision.id.desc())
        .limit(1)
    )
    users = {u.id: u.username for u in db.scalars(select(User))}
    alignment = db.scalar(select(Alignment).where(Alignment.work_id == work.id))
    return {
        "work": work_json(
            work, audio=(len(tracks), sum(t.duration_s for t in tracks)) if tracks else None
        ),
        "version": work.version,
        "metadata": metadata_of(work),
        "playback_default": work.playback_default,
        "readalong_enabled": work.readalong_enabled,
        "primary_file_id": primary.id if primary else None,
        "cover": {
            "has_cover": work.has_cover,
            "url": f"/library/works/{work.id}/cover.jpg?v={work.version}"
            if work.has_cover
            else None,
            "source": work.cover_source,
            "embedded_available": editing.embedded_cover_available(db, settings, work),
            "generated": {
                "url": covergen.cover_url(work) if not work.has_cover else None,
                "style": work.cover_style,
                "seed": work.cover_seed or 0,
                "effective_style": covergen.design_of(work).style,
                "styles": list(covergen.STYLES),
            },
        },
        "files": [
            {
                "id": f.id,
                "kind": f.kind.value,
                "rel_path": f.rel_path,
                "size": f.size,
                "sha256": f.sha256,
                "partial_md5": f.partial_md5,
                "media_type": f.media_type,
                "format": formats.label_of(f.media_type),
                "added_at": iso(f.added_at),
                "primary": primary is not None and f.id == primary.id,
            }
            for f in sorted(work.files, key=lambda f: (f.added_at, f.id))
            if f.kind.value == "ebook"
        ],
        "tracks": [
            {
                "id": t.id,
                "position": t.position,
                "title": editing.track_title(t),
                "rel_path": t.rel_path,
                "duration_s": t.duration_s,
                "size": t.size,
                "codec": t.codec,
                "sha256": t.sha256,
                "chapters": t.chapters or [],
            }
            for t in tracks
        ],
        "shelves": [{"id": s.id, "name": s.name, "member": s.id in on} for s in editable],
        "alignment": align_jobs.alignment_json(db, alignment),
        "align_models": list(MODELS),
        "align_default_model": settings.align_model or DEFAULT_MODEL,
        "read_aloud": read_aloud_status(db, settings, work, request.app.state.readaloud),
        "folder": {"dir_path": work.dir_path, "pending": work.id in editing.PENDING_FOLDERS},
        "last_revision": (
            {
                "id": last.id,
                "created_at": iso(last.created_at),
                "actor": users.get(last.actor_user_id),
                "changed": sorted(last.after),
                "undoable": last.undone_at is None
                and last.kind != "undo"
                and last.version_after == work.version,
            }
            if last
            else None
        ),
        "lookup_enabled": instance.get(db, "metadata_lookup_enabled"),
    }


@router.get("/works/{work_id}/edit")
def get_editor(
    request: Request, auth: EditorRead, db: DbDep, settings: SettingsDep, work_id: int
) -> dict:
    return editor_json(db, settings, request, auth.user, _work(db, auth.user, work_id))


def _save(
    request, auth, db, settings, work: Work, patch: WorkPatch, *, kind="edit", batch_id=None,
    journal: Journal | None = None,
):  # fmt: skip
    journal = journal or Journal()
    try:
        result = editing.save(
            db,
            settings,
            auth.user,
            work,
            patch,
            journal,
            busy=_busy(request),
            kind=kind,
            batch_id=batch_id,
        )
        if result.revision is not None:
            _audit(
                db,
                request,
                auth,
                "work.edit" if kind == "edit" else "work.bulk_edit",
                work.title,
                f"work:{work.id} v{patch.version}->v{result.revision.version_after}: "
                + ", ".join(result.changed)
                + (
                    f"; folder {result.folder['from']} -> {result.folder['dir_path']}"
                    if result.folder.get("moved")
                    else ""
                ),
            )
        return result, journal
    except Exception:
        db.rollback()
        journal.rollback()
        raise


@router.patch("/works/{work_id}")
def patch_work(
    request: Request,
    auth: Editor,
    db: DbDep,
    settings: SettingsDep,
    work_id: int,
    body: WorkPatch,
    key: IdemKey = None,
) -> dict:
    """One save from the editor. ``409 version_conflict`` carries the current editor state so
    the client can show what changed instead of overwriting it."""
    scope = f"work.patch:{work_id}"
    payload = body.model_dump(mode="json", exclude_unset=True)
    replay = idempotency.lookup(db, auth.user.id, scope, key, payload)
    if replay is not None:
        return replay
    with LIBRARY_LOCK:
        work = _work(db, auth.user, work_id)
        try:
            result, journal = _save(request, auth, db, settings, work, body)
        except EditError as exc:
            if exc.code == "version_conflict":
                fresh = _work(db, auth.user, work_id)
                exc.extra["current"] = editor_json(db, settings, request, auth.user, fresh)
            raise _edit_error(exc) from None
        out = {
            "revision_id": result.revision.id if result.revision else None,
            "changed": result.changed,
            "folder": result.folder,
        }
        try:
            idempotency.store(db, auth.user.id, scope, key, payload, out)
            db.commit()
        except Exception:
            db.rollback()
            journal.rollback()
            raise
    out["editor"] = editor_json(db, settings, request, auth.user, _work(db, auth.user, work_id))
    return out


class UndoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: int


@router.post("/works/{work_id}/undo")
def undo_work(
    request: Request, auth: Editor, db: DbDep, settings: SettingsDep, work_id: int, body: UndoIn
) -> dict:
    with LIBRARY_LOCK:
        rev = db.get(WorkRevision, body.revision_id)
        work = db.get(Work, work_id)
        if rev is None or work is None or rev.work_id != work_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        if rev.kind != "trash" and not can_see_work(db, auth.user, work_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        journal = Journal()
        try:
            editing.undo(db, settings, auth.user, work, rev, journal, busy=_busy(request))
            _audit(db, request, auth, "work.undo", work.title, f"work:{work.id} revision:{rev.id}")
            db.commit()
        except EditError as exc:
            db.rollback()
            journal.rollback()
            raise _edit_error(exc) from None
        except Exception:
            db.rollback()
            journal.rollback()
            raise
    return editor_json(db, settings, request, auth.user, _work(db, auth.user, work_id))


@router.post("/works/{work_id}/cover", status_code=status.HTTP_201_CREATED)
async def upload_cover(
    request: Request, auth: Editor, db: DbDep, settings: SettingsDep, work_id: int
) -> dict:
    """Stage a new cover image (JPEG, PNG, or WebP, at most 10 MiB) for the next save."""
    _work(db, auth.user, work_id)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > editing.COVER_MAX_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "cover_too_large")
    body = bytearray()
    async for block in request.stream():
        body += block
        if len(body) > editing.COVER_MAX_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "cover_too_large")
    from fastapi.concurrency import run_in_threadpool

    try:
        token = await run_in_threadpool(editing.stage_cover, settings, auth.user.id, bytes(body))
    except EditError as exc:
        raise _edit_error(exc) from None
    return {"token": token, "preview_url": f"/api/staged-covers/{token}.jpg"}


@router.get("/works/{work_id}/cover/generated.jpg", include_in_schema=False)
async def generated_cover_preview(
    auth: EditorRead,
    db: DbDep,
    work_id: int,
    title: Annotated[str | None, Query(max_length=covergen.MAX_TEXT)] = None,
    author: Annotated[str | None, Query(max_length=covergen.MAX_TEXT)] = None,
    style: Literal["classic", "modern", "minimal"] | None = None,
    seed: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
):
    """The editor's live preview: the generated cover for the draft title, author, style, and
    shuffle, drawn but not stored. Headers name the style and palette it resolved to."""
    work = db.get(Work, work_id)
    if work is None or work.trashed_at is not None or not can_see_work(db, auth.user, work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    design = covergen.design_for(
        work.title if title is None else title,
        covergen.design_of(work).author if author is None else author,
        style=style,
        seed=seed,
        lang=work.language,
    )
    from fastapi.concurrency import run_in_threadpool

    data = await run_in_threadpool(covergen.render_jpeg, design, width=480, quality=86)
    return Response(
        data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, max-age=600",
            "X-Cover-Style": design.style,
            "X-Cover-Palette": design.palette.name,
        },
    )


@router.get("/staged-covers/{token}.jpg", include_in_schema=False)
def staged_cover(auth: EditorRead, settings: SettingsDep, token: str):
    try:
        path = editing.staged_cover_path(settings, auth.user.id, token)
    except EditError as exc:
        raise _edit_error(exc) from None
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"}
    )


# --- trash -------------------------------------------------------------------------------------


class TrashIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int | None = Field(default=None, ge=1)


def _trash_one(
    request, auth, db, settings, work: Work, journal: Journal, batch_id: str | None = None
) -> WorkRevision:
    version = work.version
    try:
        trash.trash_work(db, settings, work, auth.user.id, busy=_busy(request), journal=journal)
    except trash.Busy:
        raise HTTPException(status.HTTP_409_CONFLICT, "work_busy") from None
    rev = WorkRevision(
        work_id=work.id,
        actor_user_id=auth.user.id,
        kind="trash",
        batch_id=batch_id,
        version_before=version,
        version_after=work.version,
        before={"trashed": False, "dir_path": work.trash_from},
        after={"trashed": True, "dir_path": work.dir_path},
    )
    db.add(rev)
    _audit(
        db,
        request,
        auth,
        "work.trash",
        work.title,
        f"work:{work.id} {work.trash_from} -> {work.dir_path}",
    )
    db.flush()
    return rev


@router.post("/works/{work_id}/trash")
def trash_work(
    request: Request,
    auth: Editor,
    db: DbDep,
    settings: SettingsDep,
    work_id: int,
    body: TrashIn | None = None,
) -> dict:
    with LIBRARY_LOCK:
        work = _work(db, auth.user, work_id)
        if body and body.version is not None and body.version != work.version:
            raise HTTPException(status.HTTP_409_CONFLICT, "version_conflict")
        journal = Journal()
        with _rollback_on_error(db, journal):
            rev = _trash_one(request, auth, db, settings, work, journal)
            db.commit()
    return {"work_id": work.id, "revision_id": rev.id, "trashed_at": iso(work.trashed_at)}


def _trash_json(db, w: Work, users: dict[int, str]) -> dict:
    purge = w.trashed_at + trash.TRASH_RETENTION
    return {
        "work": {"id": w.id, "title": w.title, "author": w.author, "has_cover": w.has_cover},
        "trashed_at": iso(w.trashed_at),
        "trashed_by": users.get(w.trashed_by),
        "purge_at": iso(purge),
        "days_left": max(0, (purge - utcnow()).days),
        "from": w.trash_from,
    }


@router.get("/trash")
def list_trash(auth: EditorRead, db: DbDep) -> dict:
    users = {u.id: u.username for u in db.scalars(select(User))}
    works = db.scalars(
        select(Work).where(Work.trashed_at.is_not(None)).order_by(Work.trashed_at.desc())
    ).all()
    files = db.scalars(
        select(TrashedFile)
        .where(TrashedFile.restored_at.is_(None))
        .order_by(TrashedFile.trashed_at.desc())
        .limit(500)
    ).all()
    titles = {
        w.id: w for w in db.scalars(select(Work).where(Work.id.in_({f.work_id for f in files})))
    }
    return {
        "retention_days": trash.TRASH_RETENTION.days,
        "can_purge": auth.user.role == Role.OWNER,
        "works": [_trash_json(db, w, users) for w in works],
        "files": [
            {
                "id": f.id,
                "kind": f.kind.value,
                "name": f.rel_path.rsplit("/", 1)[-1],
                "size": f.size,
                "work": {
                    "id": f.work_id,
                    "title": titles[f.work_id].title,
                    "in_trash": titles[f.work_id].trashed_at is not None,
                }
                if f.work_id in titles
                else None,
                "trashed_at": iso(f.trashed_at),
                "trashed_by": users.get(f.trashed_by),
                "purge_at": iso(f.trashed_at + trash.TRASH_RETENTION),
            }
            for f in files
        ],
    }


@router.post("/trash/works/{work_id}/restore")
def restore_work(
    request: Request, auth: Editor, db: DbDep, settings: SettingsDep, work_id: int
) -> dict:
    with LIBRARY_LOCK:
        work = db.get(Work, work_id)
        if work is None or work.trashed_at is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        journal = Journal()
        with _rollback_on_error(db, journal):
            trash.restore_work(db, settings, work, journal=journal)
            for rev in db.scalars(
                select(WorkRevision).where(
                    WorkRevision.work_id == work.id,
                    WorkRevision.kind == "trash",
                    WorkRevision.undone_at.is_(None),
                )
            ):
                rev.undone_at = utcnow()
            align_jobs.refresh_work(db, settings.library_dir, work, auto_enqueue=False)
            _audit(
                db, request, auth, "work.restore", work.title, f"work:{work.id} -> {work.dir_path}"
            )
            db.commit()
    return {"work_id": work.id, "dir_path": work.dir_path}


@router.delete("/trash/works/{work_id}", status_code=status.HTTP_204_NO_CONTENT)
def purge_work(
    request: Request, auth: OwnerOnly, db: DbDep, settings: SettingsDep, work_id: int
) -> Response:
    """Delete forever, before the 30 days are up. Owner only."""
    with LIBRARY_LOCK:
        work = db.get(Work, work_id)
        if work is None or work.trashed_at is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        title = work.title
        trash.purge_work(db, settings, work)
        _audit(db, request, auth, "work.purge", title, f"work:{work_id}")
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/trash/files/{file_id}/restore")
def restore_file(
    request: Request, auth: Editor, db: DbDep, settings: SettingsDep, file_id: int
) -> dict:
    from lectern.ingest.audio import audio_changed, relayout

    with LIBRARY_LOCK:
        tf = db.get(TrashedFile, file_id)
        work = db.get(Work, tf.work_id) if tf else None
        if tf is None or tf.restored_at is not None or work is None or work.trashed_at is not None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        journal = Journal()
        with _rollback_on_error(db, journal):
            try:
                obj = trash.restore_file(db, settings, tf, journal=journal)
            except FileExistsError:
                raise HTTPException(status.HTTP_409_CONFLICT, "duplicate") from None
            except LookupError:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None
            if tf.kind.value == "audio":
                relayout(db, work.id)
                audio_changed(db, work.id)
            work.version += 1
            align_jobs.refresh_work(db, settings.library_dir, work, auto_enqueue=False)
            from lectern.workmeta import write_book_json

            write_book_json(db, settings.library_dir, work)
            _audit(db, request, auth, "file.restore", work.title, f"work:{work.id} {obj.rel_path}")
            db.commit()
    return {"work_id": work.id, "rel_path": obj.rel_path}


@router.delete("/trash/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def purge_file(
    request: Request, auth: OwnerOnly, db: DbDep, settings: SettingsDep, file_id: int
) -> Response:
    with LIBRARY_LOCK:
        tf = db.get(TrashedFile, file_id)
        if tf is None or tf.restored_at is not None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        trash._remove_trashed_file(settings.library_dir, tf)
        _audit(db, request, auth, "file.purge", tf.rel_path, f"work:{tf.work_id}")
        db.delete(tf)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- bulk ----------------------------------------------------------------------------------------


class BulkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_ids: list[int] = Field(default_factory=list, max_length=500)
    selection: Selection | None = None
    action: Literal[
        "set_authors",
        "set_series",
        "add_tags",
        "remove_tags",
        "add_to_shelves",
        "remove_from_shelves",
        "trash",
    ]
    authors: list[AuthorIn] | None = Field(default=None, max_length=20)
    series: Annotated[str, NoControls] | None = Field(default=None, max_length=256)
    series_index: float | None = Field(default=None, ge=0, le=100000)
    tags: list[Annotated[str, Field(min_length=1, max_length=128), NoControls]] | None = Field(
        default=None, max_length=50
    )
    shelf_ids: list[int] | None = Field(default=None, max_length=100)


def _bulk_patch(db, user: User, work: Work, body: BulkIn) -> WorkPatch:
    meta = metadata_of(work)
    shelves = None
    if body.action == "set_authors":
        if not body.authors:
            raise EditError(422, "authors_required")
        meta["authors"] = [a.model_dump() for a in body.authors]
    elif body.action == "set_series":
        meta["series"] = (body.series or "").strip() or None
        if body.series_index is not None or not meta["series"]:
            meta["series_index"] = body.series_index
    elif body.action in ("add_tags", "remove_tags"):
        tags = [t.strip() for t in body.tags or [] if t.strip()]
        if not tags:
            raise EditError(422, "tags_required")
        folded = {t.casefold() for t in tags}
        meta["tags"] = (
            meta["tags"] + tags
            if body.action == "add_tags"
            else [t for t in meta["tags"] if t.casefold() not in folded]
        )
    else:
        if not body.shelf_ids:
            raise EditError(422, "shelves_required")
        editable = {s.id for s in editing.editable_shelves(db, user)}
        if set(body.shelf_ids) - editable:
            raise EditError(403, "shelf_not_editable")
        on = set(
            db.scalars(
                select(ShelfItem.shelf_id).where(
                    ShelfItem.work_id == work.id, ShelfItem.shelf_id.in_(list(editable))
                )
            )
        )
        shelves = sorted(
            on | set(body.shelf_ids)
            if body.action == "add_to_shelves"
            else on - set(body.shelf_ids)
        )
        return WorkPatch(version=work.version, shelves=shelves)
    return WorkPatch(version=work.version, metadata=meta)


@router.post("/works/bulk")
def bulk_edit(
    request: Request,
    auth: Editor,
    db: DbDep,
    settings: SettingsDep,
    body: BulkIn,
    key: IdemKey = None,
) -> dict:
    """Apply one change to many works. Each work is its own transaction (one failure does not
    undo the others) and its own revision, grouped by ``batch_id`` for ``bulk/undo``."""
    scope = "work.bulk"
    payload = body.model_dump(mode="json")
    replay = idempotency.lookup(db, auth.user.id, scope, key, payload)
    if replay is not None:
        return replay
    ids = resolve_selection(db, auth.user, body.work_ids, body.selection)
    batch_id = secrets.token_hex(8)
    results = []
    for wid in ids:
        with LIBRARY_LOCK:
            journal = Journal()
            try:
                with _rollback_on_error(db, journal):
                    work = _work(db, auth.user, wid)
                    if body.action == "trash":
                        _trash_one(request, auth, db, settings, work, journal, batch_id)
                        changed = ["trashed"]
                    else:
                        result, _ = _save(
                            request,
                            auth,
                            db,
                            settings,
                            work,
                            _bulk_patch(db, auth.user, work, body),
                            kind="bulk",
                            batch_id=batch_id,
                            journal=journal,
                        )
                        changed = result.changed
                    db.commit()
                results.append({"work_id": wid, "ok": True, "changed": changed})
            except Exception as exc:  # noqa: BLE001 - one work failing must not stop the rest
                results.append({"work_id": wid, "ok": False, "error": _item_error(exc)})
    out = {"batch_id": batch_id, "action": body.action, "results": results}
    _audit_batch(db, request, auth, "work.bulk", body.action, batch_id, results)
    idempotency.store(db, auth.user.id, scope, key, payload, out)
    db.commit()
    return out


def _item_error(exc: Exception) -> str:
    """Machine-readable reason one item of a bulk request failed (already rolled back)."""
    if isinstance(exc, EditError):
        return exc.code
    if isinstance(exc, HTTPException):
        if exc.status_code == 404:
            return "not_found"
        return exc.detail if isinstance(exc.detail, str) else exc.detail.get("detail")
    if isinstance(exc, OSError):  # MoveError included: a file could not be moved
        log.warning("bulk item failed: %s", exc)
        return "io_error"
    log.exception("bulk item failed", exc_info=exc)
    return "internal_error"


def _audit_batch(db, request, auth, action: str, target: str, batch_id: str, results) -> None:
    failed = [r for r in results if not r["ok"]]
    _audit(
        db,
        request,
        auth,
        action,
        target,
        f"batch:{batch_id} {len(results) - len(failed)}/{len(results)} works"
        + (
            "; failed " + ", ".join(f"{r['work_id']}:{r['error']}" for r in failed)
            if failed
            else ""
        ),
    )


@router.post("/works/bulk/{batch_id}/undo")
def bulk_undo(
    request: Request, auth: Editor, db: DbDep, settings: SettingsDep, batch_id: str
) -> dict:
    """Undo every revision of a batch, newest first. Each work is its own transaction: one
    that cannot be undone (changed since, or its removed file is back in the library) is
    reported and the rest still go back. Every undone work gets its own ``work.undo`` audit."""
    if len(batch_id) != 16 or any(c not in "0123456789abcdef" for c in batch_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    revs = db.scalars(
        select(WorkRevision)
        .where(WorkRevision.batch_id == batch_id)
        .order_by(WorkRevision.id.desc())
    ).all()
    if not revs:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    results = []
    for rev_id, wid in [(r.id, r.work_id) for r in revs]:
        with LIBRARY_LOCK:
            journal = Journal()
            try:
                with _rollback_on_error(db, journal):
                    rev, work = db.get(WorkRevision, rev_id), db.get(Work, wid)
                    if rev is None or work is None:
                        raise EditError(404, "not_found")
                    if rev.kind != "trash" and not can_see_work(db, auth.user, wid):
                        raise EditError(404, "not_found")
                    editing.undo(db, settings, auth.user, work, rev, journal, busy=_busy(request))
                    if rev.kind == "trash":
                        align_jobs.refresh_work(db, settings.library_dir, work, auto_enqueue=False)
                    _audit(
                        db,
                        request,
                        auth,
                        "work.undo",
                        work.title,
                        f"work:{work.id} revision:{rev.id} batch:{batch_id}",
                    )
                    db.commit()
                results.append({"work_id": wid, "ok": True})
            except Exception as exc:  # noqa: BLE001 - one work failing must not stop the rest
                results.append({"work_id": wid, "ok": False, "error": _item_error(exc)})
    _audit_batch(db, request, auth, "work.bulk_undo", batch_id, batch_id, results)
    db.commit()
    return {"batch_id": batch_id, "results": results}


# --- metadata suggestions -------------------------------------------------------------------------


@router.post("/metadata/lookup")
def metadata_lookup(request: Request, auth: Editor, db: DbDep, body: lookup.Query) -> dict:
    """Suggestions only; applying them is an ordinary, field-by-field edit."""
    if not instance.get(db, "metadata_lookup_enabled"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "lookup_disabled")
    if not request.app.state.limiters["lookup"].allow(f"user:{auth.user.id}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited")
    result = lookup.lookup(body, request.app.state.lookup_fetch)
    _audit(
        db,
        request,
        auth,
        "metadata.lookup",
        ",".join(body.providers),
        f"by {'isbn' if 'isbn' in result['sent'] else 'title'}; {len(result['suggestions'])} suggestion(s)"
        + (
            f"; errors: {','.join(e['provider'] + ':' + e['error'] for e in result['errors'])}"
            if result["errors"]
            else ""
        ),
    )
    db.commit()
    return result


# --- settings ----------------------------------------------------------------------------------------


@router.get("/library-settings")
def library_settings(auth: EditorRead, db: DbDep) -> dict:
    return {"settings": instance.all_settings(db), "can_change": auth.user.role == Role.OWNER}


class LibrarySettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata_lookup_enabled: bool | None = None


@router.put("/library-settings")
def update_library_settings(
    request: Request, auth: OwnerOnly, db: DbDep, body: LibrarySettingsIn
) -> dict:
    for k, v in body.model_dump(exclude_none=True).items():
        if instance.get(db, k) != v:
            instance.set_value(db, k, v, auth.user.id)
            _audit(db, request, auth, "settings.change", k, "on" if v else "off")
    db.commit()
    return {"settings": instance.all_settings(db), "can_change": True}


@router.get("/works/{work_id}/revisions")
def revisions(
    auth: EditorRead, db: DbDep, work_id: int, limit: int = Query(default=20, ge=1, le=100)
) -> dict:
    """History as opaque entries: what changed, by whom, and whether it can be undone.
    The stored before/after snapshots stay server-side (they name cover backups under the
    data directory); undo takes the revision ``id``."""
    current = _work(db, auth.user, work_id).version
    users = {u.id: u.username for u in db.scalars(select(User))}
    rows = db.scalars(
        select(WorkRevision)
        .where(WorkRevision.work_id == work_id)
        .order_by(WorkRevision.id.desc())
        .limit(limit)
    ).all()
    return {
        "revisions": [
            {
                "id": r.id,
                "kind": r.kind,
                "actor": users.get(r.actor_user_id),
                "created_at": iso(r.created_at),
                "version_before": r.version_before,
                "version_after": r.version_after,
                "changed": sorted(r.after),
                "undone_at": iso(r.undone_at),
                "batch_id": r.batch_id,
                "undoable": r.undone_at is None and r.kind != "undo" and r.version_after == current,
            }
            for r in rows
        ]
    }
