"""Place files into /library/<Author>/<Title>/ and register them in the database."""

from __future__ import annotations

import logging
import re
import shutil
import unicodedata
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern.ingest.cover import EMBEDDED_FORMATS, CoverRejected, dominant_tint, save_cover_jpeg
from lectern.ingest.epub import EpubMetadata, extract_cover
from lectern.ingest.formats import EPUB, Probe, suffix_of
from lectern.ingest.matching import match_work, sort_author, sort_title
from lectern.models import BookFile, FileKind, MatchSuggestion, Work

log = logging.getLogger("lectern.ingest")

EPUB_MEDIA_TYPE = EPUB
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


def new_work(
    db: Session,
    library: Path,
    *,
    title: str,
    author: str,
    language: str | None = None,
    isbn: str | None = None,
    description: str | None = None,
    publisher: str | None = None,
    published: str | None = None,
) -> Work:
    """Create a work and reserve its ``<Author>/<Title>`` directory name."""
    rel_dir = _unique_dir(
        library,
        safe_component(author, "Unknown Author"),
        safe_component(title, "Untitled"),
    )
    (library / rel_dir).mkdir(parents=True, exist_ok=True)
    work = Work(
        title=title[:512],
        sort_title=sort_title(title)[:512],
        author=author[:512],
        sort_author=sort_author(author)[:512],
        language=language,
        isbn=isbn,
        description=description,
        publisher=publisher,
        published=published[:32] if published else None,
        dir_path=rel_dir,
    )
    db.add(work)
    db.flush()
    return work


def book_file_name(files, media_type: str, sha256: str) -> str:
    """``book.epub``, ``book.pdf``, ``book.cbz`` for a work's first file of that format (so one
    work holds an EPUB and a PDF of the same book side by side), ``book-<sha>.<ext>`` after."""
    ext = suffix_of(media_type)
    taken = any(f.kind == FileKind.EBOOK and f.rel_path.lower().endswith(ext) for f in files)
    return f"book-{sha256[:8]}{ext}" if taken else f"book{ext}"


def save_first_page_cover(data: bytes | None, dest: Path) -> bool:
    """A PDF's rendered first page or a comic's cover page, through the bounded decoder."""
    if not data:
        return False
    try:
        save_cover_jpeg(data, dest, formats=EMBEDDED_FORMATS, quality=85)
        return True
    except (CoverRejected, OSError):
        return False


def ingest_epub(
    db: Session,
    library: Path,
    source: Path,
    meta: EpubMetadata,
    sha256: str,
    pmd5: str,
) -> tuple[Work, BookFile, bool]:
    return ingest_book(db, library, source, Probe(meta), EPUB, sha256, pmd5)


def ingest_book(
    db: Session,
    library: Path,
    source: Path,
    probe: Probe,
    media_type: str,
    sha256: str,
    pmd5: str,
) -> tuple[Work, BookFile, bool]:
    """Move an EPUB, PDF, or CBZ into the library. Returns (work, file, created_new_work).

    An uncertain match creates a new work and a ``match_suggestions`` row for a person to
    confirm or dismiss in the web app.
    """
    meta = probe.meta
    if db.scalar(select(BookFile).where(BookFile.sha256 == sha256)) is not None:
        raise FileExistsError("identical file already in library")

    author = meta.authors[0] if meta.authors else "Unknown Author"
    match = match_work(db, meta.title, meta.authors, meta.isbn)
    created = False
    if match.certain:
        work = match.work
        work.version = (work.version or 1) + 1  # an open editor must not overwrite this
        if work.isbn is None and meta.isbn:
            work.isbn = meta.isbn
    else:
        work = new_work(
            db,
            library,
            title=meta.title,
            author=author,
            language=meta.language,
            isbn=meta.isbn,
            description=meta.description,
            publisher=meta.publisher,
            published=meta.published,
        )
        work.series = meta.series
        work.series_index = meta.series_index if meta.series else None
        work.tags = list(meta.tags)
        work.narrators = list(meta.narrators)
        created = True
        if match.work is not None:
            db.add(
                MatchSuggestion(
                    work_id=work.id,
                    candidate_work_id=match.work.id,
                    score=match.score,
                    reason=match.reason,
                )
            )

    work_dir = library / work.dir_path
    work_dir.mkdir(parents=True, exist_ok=True)
    rel_name = book_file_name(work.files, media_type, sha256)
    dest = work_dir / rel_name
    shutil.move(str(source), str(dest))

    if not work.has_cover:
        cover = work_dir / "cover.jpg"
        if (meta.cover_href and extract_cover(dest, meta.cover_href, cover)) or (
            save_first_page_cover(probe.cover, cover)
        ):
            work.has_cover = True
            work.cover_source = "embedded"
            work.cover_tint = dominant_tint(cover)

    book = BookFile(
        work_id=work.id,
        kind=FileKind.EBOOK,
        rel_path=rel_name,
        media_type=media_type,
        size=dest.stat().st_size,
        sha256=sha256,
        partial_md5=pmd5,
    )
    db.add(book)
    db.flush()
    return work, book, created
