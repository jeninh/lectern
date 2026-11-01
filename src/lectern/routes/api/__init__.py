"""JSON API consumed by the single-page app. Cookie session auth; CSRF token on every write."""

from __future__ import annotations

from fastapi import APIRouter

from lectern.routes.api import (
    admin,
    ai_keys,
    align,
    articles,
    audio_bookmarks,
    books,
    core,
    covers,
    devices,
    dictionary,
    editor,
    email_in,
    health,
    highlights,
    imports,
    jobqueue,
    listening_queue,
    notifications,
    ocr,
    offline,
    pages,
    pdf2epub,
    pdftext,
    queues,
    readalong,
    reader,
    reviews,
    search,
    security,
    selection,
    shelves,
    stats,
    tts,
    uploads,
    vocabulary,
    worker,
    works,
)

router = APIRouter(prefix="/api")
router.include_router(selection.router)
router.include_router(core.router)
router.include_router(stats.router)
router.include_router(devices.router)
router.include_router(ai_keys.router)
router.include_router(queues.router)
router.include_router(listening_queue.router)
router.include_router(security.router)
router.include_router(health.router)
router.include_router(admin.router)
router.include_router(admin.genres_router)
router.include_router(books.router)
router.include_router(uploads.router)
router.include_router(imports.router)
router.include_router(articles.router)
router.include_router(email_in.router)
router.include_router(editor.router)
router.include_router(shelves.router)
router.include_router(works.router)
router.include_router(highlights.router)
router.include_router(reviews.router)
router.include_router(offline.router)
router.include_router(notifications.router)
router.include_router(readalong.router)
router.include_router(reader.router)
router.include_router(search.router)
router.include_router(dictionary.router)
router.include_router(pages.router)
router.include_router(pdftext.router)
router.include_router(align.router)
router.include_router(worker.router)
router.include_router(tts.router)
router.include_router(pdf2epub.router)
router.include_router(ocr.router)
router.include_router(jobqueue.router)
router.include_router(vocabulary.router)
router.include_router(audio_bookmarks.router)
router.include_router(covers.router)
