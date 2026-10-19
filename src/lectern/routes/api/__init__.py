"""JSON API consumed by the single-page app. Cookie session auth; CSRF token on every write."""

from __future__ import annotations

from fastapi import APIRouter

from lectern.routes.api import admin, books, core, devices, security

router = APIRouter(prefix="/api")
router.include_router(core.router)
router.include_router(devices.router)
router.include_router(security.router)
router.include_router(admin.router)
router.include_router(books.router)
