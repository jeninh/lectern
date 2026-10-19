"""Place files into /library/<Author>/<Title>/ and register them in the database."""

from __future__ import annotations

import logging
import re
import shutil
import unicodedata
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern.ingest.cover import dominant_tint
from lectern.ingest.epub import EpubMetadata, extract_cover
from lectern.ingest.matching import find_matching_work, sort_author, sort_title
from lectern.models import BookFile, FileKind, Work

log = logging.getLogger("lectern.ingest")

EPUB_MEDIA_TYPE = "application/epub+zip"
_UNSAFE = re.compile(r"[^A-Za-z0-9 ._()\-',&]+")


def safe_component(name: str, fallback: str) -> str:
    """Filesystem-safe directory name: ASCII-folded, no separators, no dot-leading, bounded."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    cleaned = _UNSAFE.sub(" ", folded)
    cleaned = " ".join(cleaned.split()).strip(" .")
    cleaned = cleaned[:100].strip(" .")
    return cleaned or fallback


def _unique_dir(library: Path, author: str, title: str) -> str:
    base = Path(author) / title
    rel = base
    n = 2
    while (library / rel).exists():
        rel = base.parent / f"{base.name} ({n})"
        n += 1
    return rel.as_posix()


def ingest_epub(
    db: Session,
    library: Path,
    source: Path,
    meta: EpubMetadata,
    sha256: str,
    pmd5: str,
) -> tuple[Work, BookFile, bool]:
    """Move source into the library. Returns (work, file, created_new_work)."""
    if db.scalar(select(BookFile).where(BookFile.sha256 == sha256)) is not None:
        raise FileExistsError("identical file already in library")

    author = meta.authors[0] if meta.authors else "Unknown Author"
    work = find_matching_work(db, meta.title, meta.authors, meta.isbn)
    created = False
    if work is None:
        rel_dir = _unique_dir(
            library,
            safe_component(author, "Unknown Author"),
            safe_component(meta.title, "Untitled"),
        )
        work = Work(
            title=meta.title[:512],
            sort_title=sort_title(meta.title)[:512],
            author=author[:512],
            sort_author=sort_author(author)[:512],
            language=meta.language,
            isbn=meta.isbn,
            description=meta.description,
            publisher=meta.publisher,
            published=meta.published,
            dir_path=rel_dir,
        )
        db.add(work)
        db.flush()
        created = True
    elif work.isbn is None and meta.isbn:
        work.isbn = meta.isbn

    work_dir = library / work.dir_path
    work_dir.mkdir(parents=True, exist_ok=True)
    existing_ebook = next((f for f in work.files if f.kind == FileKind.EBOOK), None)
    rel_name = "book.epub" if existing_ebook is None else f"book-{sha256[:8]}.epub"
    dest = work_dir / rel_name
    shutil.move(str(source), str(dest))

    if not work.has_cover and meta.cover_href:
        if extract_cover(dest, meta.cover_href, work_dir / "cover.jpg"):
            work.has_cover = True
            work.cover_tint = dominant_tint(work_dir / "cover.jpg")
            work.cover_tint = dominant_tint(work_dir / "cover.jpg")

    book = BookFile(
        work_id=work.id,
        kind=FileKind.EBOOK,
        rel_path=rel_name,
        media_type=EPUB_MEDIA_TYPE,
        size=dest.stat().st_size,
        sha256=sha256,
        partial_md5=pmd5,
    )
    db.add(book)
    db.flush()
    return work, book, created
