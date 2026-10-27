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

## Many books at once

Select books in the Library and choose **Convert to EPUB** in the selection bar (owners and
members, at most 200 books per request, after a confirmation). Every selected book with a text
PDF gets the same conversion job as above; the rest are skipped and say why: already an EPUB
(an uploaded one, a Kindle book — MOBI and AZW3 become EPUB when they are added — or an
earlier conversion), no PDF at all (comics, audiobooks), or a PDF without text yet (a scan
needs OCR first). A book already converting is reported, not queued twice, and one book
failing never stops the rest.

The jobs go through the same queue and run one at a time on the server, so a big selection
waits its turn rather than loading a Raspberry Pi; the request itself is rate-limited (10 a
minute per person). A dialog follows each book (waiting, converting, converted, failed with
the reason) while it is open; cancel or retry single books from their details or the jobs
list.

API: `POST /api/works/pdf-epub` with `{"work_ids": [...]}` or a `selection` (as
`/api/works/bulk`) answers one result per book, in order — `ok` with `status` `queued` or
`already_queued` and the `job_id`, or not `ok` with `status` `skipped` / `failed` and an
`error` code. `GET /api/pdf-epub/jobs?ids=1,2,3` reports those jobs' states.
