"""Pages of a text PDF ↔ the work's canonical sentence positions.

A position in a PDF is a page; the work's canonical position is a sentence
(``work_progress``, :mod:`lectern.progress`). For a PDF with a current sentence map
(:func:`pdf_map`) the two convert:

- **page → sentence**: the first work sentence that starts on the page (or, on a page that
  only continues a sentence, the sentence that runs over it);
- **sentence → page**: the page where that sentence starts in this PDF. For a work whose text is
  an EPUB, a sentence the PDF lacks is placed where the nearest earlier one is.

The map is current when the PDF's text is ready and either the PDF is the work's text (its
sentence numbers are the work's) or its projection onto the work's EPUB sentences was made
from the work's current sentence list. Otherwise there is no map, and pages stay pages: the
viewer and KOReader keep their page record exactly as before.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern.models import BookFile, PdfSentence, PdfText, Work
from lectern.pdftext.store import READY, is_self, pdf_text_of, sentences_key, work_text


@dataclass
class PdfMap:
    work: Work
    book: BookFile
    row: PdfText
    mode: str  # "self": the PDF is the work's text; "epub": projected onto the EPUB's sentences
    work_n: list[int | None]  # per PDF sentence
    page_start: list[int]  # per PDF sentence, 0-based
    page_end: list[int]

    def sentence_at_page(self, page: int) -> int | None:
        """The work sentence a reader on 0-based ``page`` is at."""
        best = None
        for k, (p0, p1) in enumerate(zip(self.page_start, self.page_end, strict=True)):
            if self.work_n[k] is None:
                continue
            if p0 == page:
                return self.work_n[k]
            if p0 < page <= p1:
                best = self.work_n[k]
            if p0 > page:
                break
        return best

    def page_of(self, work_n: int) -> int | None:
        """The 0-based page where work sentence ``work_n`` starts in this PDF."""
        pairs = sorted((n, k) for k, n in enumerate(self.work_n) if n is not None)
        if not pairs:
            return None
        i = bisect_right([n for n, _ in pairs], work_n) - 1
        if i < 0:
            return self.page_start[pairs[0][1]]
        # the first PDF sentence of that work sentence
        n = pairs[i][0]
        while i > 0 and pairs[i - 1][0] == n:
            i -= 1
        return self.page_start[pairs[i][1]]


def pdf_map(db: Session, library: Path, book: BookFile) -> PdfMap | None:
    row = pdf_text_of(db, book.id)
    if row is None or row.status != READY or row.pdf_sha256 != book.sha256:
        return None
    work = db.get(Work, book.work_id)
    if work is None:
        return None
    found = work_text(db, library, work)
    if found is None:
        return None
    source = found[0]
    if source.kind == "pdf":
        if source.file.id != book.id:
            return None  # another PDF of the work is its text
        if not is_self(db, work, row):
            return None  # the work's sentence list is not (yet) this PDF's
        mode = "self"
    else:
        if row.projected_key is None or row.projected_key != sentences_key(
            db, work.id, source.sha256
        ):
            return None
        mode = "epub"
    rows = db.execute(
        select(PdfSentence.n, PdfSentence.epub_n, PdfSentence.page_start, PdfSentence.page_end)
        .where(PdfSentence.file_id == book.id)
        .order_by(PdfSentence.n)
    ).all()
    return PdfMap(
        work=work,
        book=book,
        row=row,
        mode=mode,
        work_n=[n if mode == "self" else e for n, e, _, _ in rows],
        page_start=[p0 for _, _, p0, _ in rows],
        page_end=[p1 for _, _, _, p1 in rows],
    )
