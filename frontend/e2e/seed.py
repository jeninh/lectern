"""Seed a throwaway Lectern for the Playwright suite: an owner account and a large library.

    uv run python frontend/e2e/seed.py <dir>

Writes <dir>/data and <dir>/library. 360 works ("Book 001" … "Book 360", by "Author 00" …
"Author 11"), so the Library grid is far taller than the window and has to be windowed; six of
them are real EPUBs (for Send to device), the rest are bare folders. "Book 001" … "Book 006"
are the EPUBs. Run it against an empty directory only.

It also writes three EPUBs to upload (Review before filing) to frontend/e2e/.out/.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

from sqlalchemy import select

from lectern.config import Settings
from lectern.db import init_engine, session_factory
from lectern.ingest.watcher import InboxWatcher
from lectern.migrate import upgrade_to_head
from lectern.models import Role, User, Work
from lectern.security.passwords import hash_password
from lectern.setup_state import complete_setup

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.conftest import make_epub  # noqa: E402

PASSWORD = "e2e correct horse battery"  # noqa: S105 - throwaway test server
TOTAL = 360
EPUBS = 6


UPLOADS = ["Upload One", "Upload Two", "Upload Three"]


def main(base: Path) -> None:
    out = Path(__file__).parent / ".out"
    out.mkdir(exist_ok=True)
    for i, title in enumerate(UPLOADS):
        make_epub(out / f"upload-{i + 1}.epub", title=title, authors=["Uploader"], padding=500 + i)
    settings = Settings(data_dir=base / "data", library_dir=base / "library", insecure_http=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(init_engine(settings))
    for i in range(1, EPUBS + 1):
        make_epub(
            settings.inbox_path / f"b{i:03}.epub",
            title=f"Book {i:03}",
            authors=[f"Author {i % 12:02}"],
            padding=i * 7,
        )
    w = InboxWatcher(settings.inbox_path, settings.library_dir, session_factory(), 0.01)
    w.scan_once()
    w.scan_once()
    with session_factory()() as db:
        db.add(
            User(
                username="owner",
                display_name="Owner",
                role=Role.OWNER,
                password_hash=hash_password(PASSWORD),
                webauthn_user_handle=secrets.token_bytes(32),
            )
        )
        # The Docs tab's reader case (e2e/docs.spec.ts): docs for members and the owner never
        # reach a reader's browser.
        db.add(
            User(
                username="reader",
                display_name="Reader",
                role=Role.READER,
                password_hash=hash_password(PASSWORD),
                webauthn_user_handle=secrets.token_bytes(32),
            )
        )
        complete_setup(db)
        assert len(db.scalars(select(Work)).all()) == EPUBS
        for i in range(EPUBS + 1, TOTAL + 1):
            author = f"Author {i % 12:02}"
            title = f"Book {i:03}"
            rel = f"{author}/{title}"
            (settings.library_dir / rel).mkdir(parents=True)
            db.add(
                Work(
                    title=title,
                    sort_title=title.lower(),
                    author=author,
                    sort_author=author.lower(),
                    authors=[{"name": author, "sort": author}],
                    dir_path=rel,
                )
            )
        db.commit()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
