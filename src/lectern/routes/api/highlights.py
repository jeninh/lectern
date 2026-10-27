"""Your KOReader highlights and notes (lectern.annotations): per book, all books, and as
Markdown. Session only, and only the caller's own highlights on works they can see."""

from __future__ import annotations

import re
import unicodedata

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse, Response

from lectern import annotations
from lectern.access import can_see_work
from lectern.models import Work
from lectern.security.deps import Auth, DbDep, SettingsDep

router = APIRouter()

PRIVATE = {"Cache-Control": "no-store"}


def _visible(db, user, work_id: int) -> None:
    if not can_see_work(db, user, work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


def _download(text: str, name: str) -> Response:
    slug = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", slug).strip("-")[:80] or "highlights"
    return Response(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={**PRIVATE, "Content-Disposition": f'attachment; filename="{slug}.md"'},
    )


@router.get("/works/{work_id}/annotations")
def work_annotations(auth: Auth, db: DbDep, settings: SettingsDep, work_id: int) -> JSONResponse:
    _visible(db, auth.user, work_id)
    groups = annotations.visible(db, settings.library_dir, auth.user, work_id)
    db.commit()  # placements redone against a changed EPUB
    pairs = groups[0][1] if groups else []
    body = {
        "work_id": work_id,
        "annotations": [annotations.as_json(a, f) for a, f in pairs],
        "export_url": f"/api/works/{work_id}/annotations.md",
    }
    return JSONResponse(body, headers=PRIVATE)


@router.get("/works/{work_id}/annotations.md")
def work_annotations_md(auth: Auth, db: DbDep, settings: SettingsDep, work_id: int) -> Response:
    _visible(db, auth.user, work_id)
    groups = annotations.visible(db, settings.library_dir, auth.user, work_id)
    db.commit()
    title = db.get(Work, work_id).title
    if not groups:
        return _download(f"# {title}\n\nNo highlights yet.\n", f"{title} highlights")
    return _download(annotations.markdown(groups), f"{title} highlights")


@router.get("/annotations")
def all_annotations(auth: Auth, db: DbDep, settings: SettingsDep) -> JSONResponse:
    """Which of your books have highlights, and how many."""
    groups = annotations.visible(db, settings.library_dir, auth.user)
    db.commit()
    works = [
        {"work_id": w.id, "title": w.title, "author": w.author, "count": len(pairs)}
        for w, pairs in groups
    ]
    body = {
        "works": works,
        "total": sum(w["count"] for w in works),
        "export_url": "/api/annotations.md",
    }
    return JSONResponse(body, headers=PRIVATE)


@router.get("/annotations.md")
def all_annotations_md(auth: Auth, db: DbDep, settings: SettingsDep) -> Response:
    groups = annotations.visible(db, settings.library_dir, auth.user)
    db.commit()
    text = annotations.markdown(groups, title="Highlights")
    if not groups:
        text = "# Highlights\n\nNo highlights yet.\n"
    return _download(text, "Lectern highlights")
