"""Text recognition (OCR) for scanned PDFs (issue #17, docs/ocr.md).

A scanned PDF is pictures of pages with no text layer. Lectern finds them and recognises
their words page by page with Tesseract in sandboxed children (:mod:`.child`), as a
background job (:mod:`.jobs`, kind ``ocr``, capability ``ocr``: the server's own runner or a
worker), and keeps two derived files next to the work, never touching the original:

- ``<work>/derived/ocr-<sha256 prefix>.pdf``: the original pages with an invisible,
  searchable and selectable text layer (:mod:`.textlayer`);
- ``<work>/derived/ocr-<sha256 prefix>.json``: the recognised words with their boxes in PDF
  user space, per page, in exactly the shape the text-layer reader of text PDFs produces
  (``lectern.pdftext.extract``'s batch pages), so the same layout step turns them into the
  same canonical text document.
"""
