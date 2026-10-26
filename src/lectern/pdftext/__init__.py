"""The text layer of PDFs: extraction, layout, and the canonical PDF text (docs/pdf-read-along.md).

- :mod:`lectern.pdftext.extract` reads words and their boxes from a PDF in the same sandboxed
  child process, with the same limits, as PDF ingest (:mod:`lectern.ingest.pdf`).
- :mod:`lectern.pdftext.layout` turns those words into reading-order text: columns, running
  headers and footers, page numbers, hyphenation, ligatures, paragraphs, and sections from the
  outline or page ranges. It is pure Python and deterministic.
- :mod:`lectern.pdftext.document` is the canonical text document (JSON), the thing alignment,
  narration, and the read-along overlay all read.
- :mod:`lectern.pdftext.store` keeps it current for every PDF in the library and persists the
  sentences with their page and box maps.
"""
