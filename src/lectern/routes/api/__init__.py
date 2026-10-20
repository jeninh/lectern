"""JSON API consumed by the single-page app. Cookie session auth; CSRF token on every write."""

from __future__ import annotations

from fastapi import APIRouter

from lectern.routes.api import (
    admin,
    align,
    books,
    core,
    devices,
    notifications,
    readalong,
    security,
    shelves,
    uploads,
    worker,
    works,
)

router = APIRouter(prefix="/api")
router.include_router(core.router)
router.include_router(devices.router)
router.include_router(security.router)
router.include_router(admin.router)
router.include_router(books.router)
router.include_router(uploads.router)
router.include_router(shelves.router)
router.include_router(works.router)
router.include_router(notifications.router)
router.include_router(readalong.router)
router.include_router(align.router)
router.include_router(worker.router)
