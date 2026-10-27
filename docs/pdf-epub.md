# PDF → EPUB conversion

A work with a text PDF, or a scan once text recognition has run ([ocr.md](ocr.md)), can gain a
reflowable EPUB edition: **Convert to EPUB…** in its detail view analyses the PDF, shows the rules it will
convert with, and converts once you confirm them. The original PDF
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

## Analyse, review the rules, convert

1. **Analyse** (a quick job, no edition yet): the pages are read in the sandboxed PDF child for
   type styles, images, and drawn lines, and the structure is found: figures, tables, maths,
   footnotes, drop caps, verse, lists, block quotes, and a confidence score for every page.
2. **Review**: a sheet lists every rule in plain words, with a control for each, and a
   side-by-side preview of sample pages (the PDF page beside its converted text) that follows
   your edits:
   - page numbers: the numbering found (e.g. "i–xii, then 1–340"), keep or drop, an offset;
   - chapters: rename, or join a chapter to the one before; the heading sizes found;
   - footnotes: at the end of each chapter, after their paragraph (pop-ups), or left as text;
   - figures (keep or leave out), tables (simple grids as HTML tables and complex ones as
     images, all as images, or plain text), maths (images with the text as alt, or text);
   - verse: detected automatically and/or on given pages; inline styles on or off;
   - pages to keep as images (low-confidence pages start selected) and pages to leave out
     (a printed cover, copyright page, or index).

   **Convert with these rules** works straight away with the detected defaults.
3. **Convert**: the edition is built with the confirmed rules, which are saved with it
   (`derived/pdf2epub-rules.json`). **Edit rules…** on a converted book re-opens the sheet
   and re-converts.

The same flow is available to AI apps over MCP: `analyse_pdf_conversion` returns the
detected rules (and converted text of chosen pages), `convert_pdf_to_epub` takes the rules
the person approved.

## What the conversion keeps beyond the text

- **Inline styles**: italic, bold, small caps, superscripts and subscripts; a drop cap is
  rejoined to its word.
- **Figures**: embedded images and vector drawings, rendered from their region of the page,
  with their captions as `figcaption` and any labels as alt text.
- **Tables**: a ruled grid becomes an HTML table (header row as `th`, caption as `caption`);
  anything less regular is an image of its region with its text as alt.
- **Footnotes**: small notes at the foot of a page matched to the raised marker in the body
  become `noteref` links and `aside epub:type="footnote"`.
- **Maths** and anything else that does not reflow: an image of the region, its text as alt.
- **Verse, lists, block quotes**: line breaks kept for verse; bulleted and numbered lists;
  indented passages as `blockquote`.
- **Page images**: a page kept as an image replaces its text with the page rendered whole
  (its text as alt), so a plate, a map, or a page the extractor reads badly stays faithful.

The report (`derived/pdf2epub-report.json`) adds what was converted, each page's confidence
and reasons, and a warning for low-confidence pages that were reflowed anyway.

## Scans (OCR)

A scan's edition is built from its recognised text (issue #62) and says so: the package
description names the OCR engine and language, a `<meta name="lectern:source"
content="ocr"/>` marks it, the contents page carries a note, and the report has `source:
"ocr"`, `ocr: {engine, language}` and a warning to expect recognition errors. The analysis
and review work the same way. A scan has no type styles to read, so it gets no italics or
footnote markers, and each page's confidence also counts the OCR (the review sheet says
"text from OCR"). When the PDF's text changes after a conversion (the scan was recognised
again), `GET /api/works/{id}/pdf-epub` says `outdated: true`, the stored analysis no longer
matches, and converting again re-analyses the new text and replaces the edition with the
saved rules.

## Not yet

Columns, running headers, and paragraph joins are decided by the text extractor for every
reader of the PDF's text, so the review sheet reports them but does not change them.

## Running it

The jobs run on the server (a Raspberry Pi included — they need the already-extracted text
and a sandboxed look at the pages, and are never sent to a worker). It appears in the shared jobs list with
cancel and retry like any other job.

## Many books at once

Select books in the Library and choose **Convert to EPUB** in the selection bar (owners and
members, at most 200 books per request, after a confirmation). Every selected book with a text
PDF gets the same conversion job as above; the rest are skipped and say why: already an EPUB
(an uploaded one, a Kindle book — MOBI and AZW3 become EPUB when they are added — or an
earlier conversion), no PDF at all (comics, audiobooks), or a PDF without text yet (a scan
needs OCR first), or a PDF being analysed for review right now. A book already converting is
reported, not queued twice, and one book
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
