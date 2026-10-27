"""Save a web article as an EPUB (issue #67, docs/ingest-paths.md).

- ``GET  /api/articles``  whether saving works right now (``reason``: ``offline``) and the
  person's recent saves
- ``POST /api/articles``  ``{"url"}``: queue a save (``202``); the ``article`` job fetches the
  page, builds the EPUB, and files it as a new work (lectern.article.jobs)

Cancelling is the shared ``POST /api/jobs/{id}/cancel``.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from lectern.article import fetch
from lectern.article import jobs as aj
from lectern.models import Role
from lectern.security import audit
from lectern.security.deps import AuthContext, DbDep, require_role, require_role_read
from lectern.security.sessions import client_ip

router = APIRouter(prefix="/articles")

Saver = Annotated[AuthContext, Depends(require_role(Role.OWNER, Role.MEMBER))]
Viewer = Annotated[AuthContext, Depends(require_role_read(Role.OWNER, Role.MEMBER))]


class SaveIn(BaseModel):
    url: str = Field(min_length=1, max_length=fetch.MAX_URL)


@router.get("")
def state(auth: Viewer, db: DbDep) -> dict:
    reason = aj.unavailable()
    return {
        "enabled": reason is None,
        "reason": reason,
        "jobs": [aj.job_json(j) for j in aj.recent(db, auth.user.id)],
    }


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def save(request: Request, auth: Saver, db: DbDep, body: SaveIn) -> dict:
    reason = aj.unavailable()
    if reason is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, {"detail": reason})
    try:
        url = fetch.check_url(body.url)
    except fetch.FetchError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"detail": exc.code}) from None
    host = urlsplit(url).hostname or ""
    if aj.active_count(db, auth.user.id) >= aj.MAX_ACTIVE_PER_USER:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, {"detail": "too_many_articles"})
    job = aj.enqueue(db, auth.user, url)
    audit.record(db, "article.request", actor_user_id=auth.user.id, target=f"job:{job.id}",
                 ip=client_ip(request), detail=host[:200])  # fmt: skip
    db.commit()
    runner = getattr(request.app.state, "jobs", None)
    if runner is not None:
        runner.wake.set()
    return aj.job_json(job)
