"""A seeded Lectern for the mobile end-to-end tests (frontend/e2e, docs/offline.md#tests).

    uv run python frontend/e2e/serve.py [port]

Starts from an empty data directory every time, seeds it, and serves the built app:

- owner ``owner`` and two readers, ``iphone`` and ``android`` (one per Playwright project, so
  the projects never share positions or downloads), each with a shelf holding both books;
- *The Gift of the Magi*: EPUB, a 77 s narration with a data-saver copy, and its alignment
  (the repository's test fixture), so download, offline listening, and read-along work;
- *Field Notes*, a 6-page PDF, used for page positions and for revoking a book.

Passwords are ``E2E_PASSWORD``. Plain HTTP on localhost: browsers treat it as a secure
context, so service workers and passkeys (rp_id ``localhost``) work.
"""

from __future__ import annotations

import os
import secrets
import shutil
import sys
import tempfile
from io import BytesIO
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))  # tests.conftest helpers and fixtures

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("E2E_PORT", "8765"))
E2E_PASSWORD = "correct horse battery staple e2e"  # noqa: S105 - throwaway test accounts
MAGI = ROOT / "tests" / "fixtures" / "magi"


def seed(settings) -> None:
    from sqlalchemy import select

    from lectern.db import init_engine, session_factory
    from lectern.ingest.epub import read_epub_metadata
    from lectern.ingest.hashing import partial_md5, sha256_file
    from lectern.ingest.library import ingest_epub
    from lectern.ingest.watcher import InboxWatcher
    from lectern.jobs import transcode_track, variant_path
    from lectern.migrate import upgrade_to_head
    from lectern.models import (
        Alignment,
        AlignmentStatus,
        AudioTrack,
        Role,
        Sentence,
        Shelf,
        ShelfGrant,
        ShelfItem,
        ShelfPermission,
        User,
        Work,
    )
    from lectern.security.passwords import hash_password
    from lectern.sentence_map import load_sentence_map
    from lectern.setup_state import complete_setup

    upgrade_to_head(init_engine(settings))
    settings.resolve_secret_key()
    m = load_sentence_map(MAGI / "sentence_map.json")
    staged = settings.data_dir / "magi.epub"
    shutil.copy(MAGI / "the-gift-of-the-magi.epub", staged)
    # A PDF through the inbox, the way a person adds one.
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    from PIL import Image, ImageDraw
    from pypdf import PdfReader, PdfWriter

    pages = []
    for i in range(6):
        img = Image.new("RGB", (620, 877), (250, 246, 238))
        d = ImageDraw.Draw(img)
        d.rectangle((40, 40, 580, 837), outline=(31, 58, 147), width=4)
        d.text((70, 90), f"Field Notes - page {i + 1}", fill=(34, 29, 23))
        for y in range(160, 800, 28):
            d.line((70, y, 550, y), fill=(210, 198, 176), width=2)
        pages.append(img)
    buf = BytesIO()
    pages[0].save(buf, "PDF", save_all=True, append_images=pages[1:])
    w = PdfWriter(clone_from=PdfReader(BytesIO(buf.getvalue())))
    w.add_metadata({"/Title": "Field Notes", "/Author": "June Park"})
    with open(settings.inbox_path / "field-notes.pdf", "wb") as f:
        w.write(f)

    with session_factory()() as db:
        work, _book, _ = ingest_epub(
            db, settings.library_dir, staged, read_epub_metadata(staged),
            sha256_file(MAGI / "the-gift-of-the-magi.epub"),
            partial_md5(MAGI / "the-gift-of-the-magi.epub"),
        )  # fmt: skip
        audio_dir = settings.library_dir / work.dir_path / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(MAGI / "excerpt.mp3", audio_dir / "excerpt.mp3")
        t = m.tracks[0]
        db.add(
            AudioTrack(
                work_id=work.id, position=0, rel_path="audio/excerpt.mp3", media_type="audio/mpeg",
                codec="mp3", duration_s=t.duration_s, offset_s=0.0,
                size=(audio_dir / "excerpt.mp3").stat().st_size, sha256=t.sha256,
            )
        )  # fmt: skip
        for s in m.sentences:
            db.add(Sentence(work_id=work.id, **s.model_dump(exclude={"text"})))
        db.add(
            Alignment(
                work_id=work.id, model=m.model, status=AlignmentStatus.SUCCEEDED,
                coverage=m.coverage, sentence_count=len(m.sentences),
                epub_sha256=m.epub_sha256, audio_fingerprint=m.audio_fingerprint,
            )
        )  # fmt: skip
        transcode_track(
            settings,
            audio_dir / "excerpt.mp3",
            variant_path(settings.library_dir, work, t.sha256, settings.audio_low_codec),
            settings.audio_low_codec,
            settings.audio_low_bitrate_kbps,
        )
        users = {}
        for name, role in (
            ("owner", Role.OWNER),
            ("iphone", Role.READER),
            ("android", Role.READER),
        ):
            u = User(
                username=name, display_name=name.title(), role=role,
                password_hash=hash_password(E2E_PASSWORD),
                webauthn_user_handle=secrets.token_bytes(32),
            )  # fmt: skip
            db.add(u)
            users[name] = u
        db.flush()
        complete_setup(db)
        db.commit()
        magi_id = work.id

    watcher = InboxWatcher(
        settings.inbox_path, settings.library_dir, session_factory(), 0.01, settings
    )
    for _ in range(4):
        watcher.scan_once()
    with session_factory()() as db:
        pdf_id = db.scalar(select(Work.id).where(Work.title == "Field Notes"))
        assert pdf_id, "the PDF was not filed"
        owner = db.scalar(select(User).where(User.username == "owner"))
        for name in ("iphone", "android"):
            reader = db.scalar(select(User).where(User.username == name))
            shelf = Shelf(owner_id=owner.id, name=f"For {name}")
            db.add(shelf)
            db.flush()
            db.add_all(
                [ShelfItem(shelf_id=shelf.id, work_id=magi_id, position=0),
                 ShelfItem(shelf_id=shelf.id, work_id=pdf_id, position=1),
                 ShelfGrant(shelf_id=shelf.id, user_id=reader.id,
                            permission=ShelfPermission.VIEW, granted_by=owner.id)]
            )  # fmt: skip
        db.commit()


def main() -> None:
    from lectern.config import Settings
    from lectern.main import create_app

    root = Path(os.environ.get("E2E_DIR") or tempfile.mkdtemp(prefix="lectern-e2e-"))
    shutil.rmtree(root, ignore_errors=True)
    settings = Settings(
        data_dir=root / "data",
        library_dir=root / "library",
        base_url=f"http://localhost:{PORT}",
        insecure_http=True,
        log_level="warning",
        # Many downloads and re-downloads in one run; the cap itself is tested in pytest.
        download_cap_per_hour=1000,
        download_device_cap_per_hour=1000,
        download_burst_count=0,
        # The fixture narration is a 32 kb/s MP3; keep its data-saver copy smaller than that.
        audio_low_bitrate_kbps=16,
    )
    settings.data_dir.mkdir(parents=True)
    settings.library_dir.mkdir(parents=True)
    seed(settings)
    app = create_app(settings, start_watcher=False, start_background=False)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
