"""Optional PDF → EPUB conversion (issue #28): a reflowable EPUB 3 edition built from a
PDF's canonical text (:mod:`lectern.pdftext`), with the PDF's own pagination kept as
metadata.

:mod:`lectern.pdf2epub.build` turns the text document (text + word boxes, reading order,
de-hyphenation, page labels) into a deterministic EPUB 3: one XHTML file per section,
``epub:type="pagebreak"`` markers at every page boundary (inline, so a paragraph that crosses
a page stays one paragraph), a ``page-list`` nav from the PDF's page labels, and
``dc:source`` + ``pageBreakSource`` so a reader knows which edition the page numbers belong
to. The original PDF is never touched; the result is a separate edition of the same work
(``book-converted-from-pdf.epub``), and the existing EPUB↔PDF sentence projection
(:mod:`lectern.pdftext.project`) carries positions between the two.

:mod:`lectern.pdf2epub.jobs` queues and runs the conversion as a job (``kind =
"pdf2epub"``) with the shared progress, cancel, and retry (routes/api/pdf2epub.py); it also
writes a conversion report (``derived/pdf2epub-report.json``).
"""
