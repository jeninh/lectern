"""Text-only sentence lists, built at EPUB ingest.

The canonical progress API (``work_progress``) stores positions as sentences. Alignment
(milestone 3) produces sentences with audio times; until then every EPUB gets a text-only list
(all ``matched = false``), which the contract allows, so the web reader, the audio player, and
the hand-off prompt have a shared coordinate system from day one.

Segmentation is deliberately simple and deterministic: split the normalised spine text
(:func:`lectern.positions.normalize_spine_text`) after ``.``, ``!``, ``?`` or ``…`` (plus closing
quotes and brackets) followed by whitespace, except after common abbreviations and initials or
before a lowercase word, and cap very long runs at a word boundary. When a real sentence map arrives,
:func:`replace_sentences` swaps the list and re-projects every stored position through the
text timeline, so nobody loses their place.
"""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from lectern.ingest.epub import EpubError, read_spine
from lectern.models import Alignment, BookFile, FileKind, Sentence, WorkProgress
from lectern.positions import SentenceIndex, SentencePos, normalize_spine_text

log = logging.getLogger("lectern.ingest")

MAX_SENTENCE_CHARS = 600
MAX_SPINE_ITEM_BYTES = 16 * 1024 * 1024
_END = re.compile(r"[.!?…]+[\"'”’»)\]]*(?=\s)")
_ABBREV = {
    "mr", "mrs", "ms", "dr", "st", "prof", "sr", "jr", "vs", "etc", "mt", "no", "vol",
    "ch", "fig", "e.g", "i.e", "cf", "rev", "gen", "capt", "lt", "col", "sgt",
}  # fmt: skip


def segment(text: str) -> list[tuple[int, int]]:
    """Half-open ``[start, end)`` spans of the sentences in already-normalised ``text``."""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _END.finditer(text):
        end = m.end()
        word = text[start : m.start()].rsplit(" ", 1)[-1].lower().strip("\"'“‘(")
        if m.group(0).startswith(".") and (word in _ABBREV or (len(word) == 1 and word.isalpha())):
            continue
        if text[end + 1 : end + 2].islower():  # “Really?” he asked. / etc. and so on
            continue
        _push(spans, text, start, end)
        start = end + 1  # the single space that follows (text is normalised)
    _push(spans, text, start, len(text))
    return spans


def _push(spans: list[tuple[int, int]], text: str, start: int, end: int) -> None:
    while end - start > MAX_SENTENCE_CHARS:
        cut = text.rfind(" ", start + 1, start + MAX_SENTENCE_CHARS)
        if cut <= start:
            cut = start + MAX_SENTENCE_CHARS
        spans.append((start, cut))
        start = cut + 1 if text[cut : cut + 1] == " " else cut
    if end > start:
        spans.append((start, end))


def epub_sentences(path: Path) -> list[dict]:
    """Text-only sentence entries for every spine item of the EPUB at ``path``."""
    out: list[dict] = []
    spine = read_spine(path)
    with zipfile.ZipFile(path) as zf:
        for spine_index, name in enumerate(spine):
            try:
                info = zf.getinfo(name)
                if info.file_size > MAX_SPINE_ITEM_BYTES:
                    continue
                text = normalize_spine_text(zf.read(name))
            except (KeyError, ValueError, zipfile.BadZipFile):
                continue
            for start, end in segment(text):
                out.append(
                    {
                        "n": len(out),
                        "spine_index": spine_index,
                        "char_start": start,
                        "char_end": end,
                    }
                )
    return out


def primary_epub(db: Session, work_id: int) -> BookFile | None:
    """The EPUB the reader opens and sentence lists are built from: the one chosen in the
    editor (``works.primary_file_id``), else the first one added."""
    from lectern.models import Work

    chosen = db.scalar(select(Work.primary_file_id).where(Work.id == work_id))
    if chosen is not None:
        book = db.get(BookFile, chosen)
        if book is not None and book.work_id == work_id and book.kind == FileKind.EBOOK:
            return book
    return db.scalar(
        select(BookFile)
        .where(BookFile.work_id == work_id, BookFile.kind == FileKind.EBOOK)
        .order_by(BookFile.added_at, BookFile.id)
        .limit(1)
    )


def replace_sentences(db: Session, work_id: int, entries: list[dict]) -> None:
    """Swap a work's sentence list and move every stored position along the text timeline.

    Each ``work_progress`` row is projected to a text position with the old list and back to a
    sentence with the new one. Caller commits.
    """
    old_rows = db.scalars(select(Sentence).where(Sentence.work_id == work_id)).all()
    old = SentenceIndex(old_rows) if old_rows else None
    texts = {}
    if old is not None:
        for p in db.scalars(select(WorkProgress).where(WorkProgress.work_id == work_id)):
            if p.sentence_n < len(old):
                texts[p.id] = old.to_text(SentencePos(p.sentence_n, p.sentence_offset))
    db.execute(delete(Sentence).where(Sentence.work_id == work_id))
    db.flush()
    db.add_all(
        Sentence(
            work_id=work_id,
            n=e["n"],
            spine_index=e["spine_index"],
            char_start=e["char_start"],
            char_end=e["char_end"],
            audio_start_s=e.get("audio_start_s"),
            audio_end_s=e.get("audio_end_s"),
            matched=bool(e.get("matched", False)),
        )
        for e in entries
    )
    db.flush()
    from lectern.progress import SentenceCache

    SentenceCache.invalidate(work_id)
    if not texts or not entries:
        return
    new = SentenceIndex(_Entry(e) for e in entries)
    for p in db.scalars(select(WorkProgress).where(WorkProgress.id.in_(list(texts)))):
        pos = new.at_text(texts[p.id])
        if pos is not None:
            p.sentence_n, p.sentence_offset = pos.n, pos.offset


class _Entry:
    __slots__ = ("n", "spine_index", "char_start", "char_end", "audio_start_s", "audio_end_s",
                 "matched")  # fmt: skip

    def __init__(self, e: dict) -> None:
        self.n, self.spine_index = e["n"], e["spine_index"]
        self.char_start, self.char_end = e["char_start"], e["char_end"]
        self.audio_start_s, self.audio_end_s = e.get("audio_start_s"), e.get("audio_end_s")
        self.matched = bool(e.get("matched", False))


def ensure_text_sentences(db: Session, library: Path, work_id: int) -> int:
    """Build the text-only list for a work that has an EPUB and no sentences yet.

    Never touches a work that has an alignment (its sentences come from the sentence map).
    Returns the number of sentences written. Caller commits.
    """
    if db.scalar(select(Alignment.id).where(Alignment.work_id == work_id)) is not None:
        return 0
    if db.scalar(select(func.count()).select_from(Sentence).where(Sentence.work_id == work_id)):
        return 0
    book = primary_epub(db, work_id)
    if book is None:
        return 0
    from lectern.models import Work

    work = db.get(Work, work_id)
    try:
        entries = epub_sentences(library / work.dir_path / book.rel_path)
    except (EpubError, OSError, zipfile.BadZipFile) as exc:
        log.warning("could not segment work %d: %s", work_id, exc)
        return 0
    replace_sentences(db, work_id, entries)
    return len(entries)
