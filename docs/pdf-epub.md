# PDF → EPUB conversion

A work with a text PDF (not a scan — those need OCR first) can gain a reflowable EPUB
edition: **Convert to EPUB…** in its detail view queues a conversion job. The original PDF
stays; the EPUB is a separate edition of the same work (`book-converted-from-pdf.epub`), and
removing it takes nothing else with it.

## What the converted edition has

- **Chapters** from the PDF's outline (bookmarks); a PDF without an outline gets sections
  from its detected headings, and the report says so.
- **Paragraphs** rebuilt from the reading order, with line joins and de-hyphenation exactly
  as the PDF read-along uses (docs/pdf-read-along.md).
- **Page numbers as metadata, not text.** Printed page numbers, running headers, and
  footers are stripped from the text flow. Each page boundary gets an inline
  `epub:type="pagebreak"` marker (a paragraph that runs over a break stays one paragraph),
  the navigation document has a `page-list` built from the PDF's own page labels
  (`/PageLabels`: roman numerals for front matter, offsets; physical numbers as fallback),
  and the package says where the numbers come from (`dc:source`, `pageBreakSource`).
  KOReader shows these as reference pages, so "page 143" in a citation still matches.
- **The work's cover and metadata** (title, author, language, ISBN).
- **A conversion report** (`derived/pdf2epub-report.json`, also on the job's result):
  counts, warnings (pages with no text layer, inferred chapters, unlinked pages), and where
  the labels came from.

Progress, bookmarks, and read-along carry over: the work's text timeline moves to the EPUB,
and the existing PDF↔EPUB sentence projection keeps the PDF's positions meaningful (the
reader can switch editions without losing their place).

## What it does not have (yet)

The conversion is deliberately text-first. Not converted in this version: images and
figures, tables, footnotes as `noteref` asides, drop caps, poetry mode, inline styles, and
math (issue #28's later bullets). A scanned PDF is refused until OCR exists (issue #17).

## Running it

The job runs on the server (a Raspberry Pi included — it only needs the already-extracted
text, so it is quick and is never sent to a worker). It appears in the shared jobs list with
cancel and retry like any other job.
