# Kindle books (MOBI and AZW3)

Lectern takes **DRM-free** MOBI and AZW3 (KF8) books through every ingest path (the inbox, the
**Add books** menu and drag and drop, and the AI-tools upload) and converts each one to an
EPUB 3 when it arrives. The EPUB is what gets reviewed, matched, filed (`book.epub`), read,
aligned, and served; nothing downstream sees a Kindle format.

**DRM-protected books are refused** with this message:

> This book is DRM-protected; Lectern can only add DRM-free books.

Lectern does not remove DRM, and its docs, messages, and AI tools give no advice on removing it.

| Format | Media type | Extension | Recognised by |
| --- | --- | --- | --- |
| MOBI (Mobipocket 6/7, and joint MOBI7 + KF8 files) | `application/x-mobipocket-ebook` | `.mobi` | PalmDB header with `BOOKMOBI` at offset 60 |
| AZW3 (KF8) | `application/vnd.amazon.mobi8-ebook` | `.azw3` | PalmDB header with `BOOKMOBI` at offset 60 |

**Not supported:** KFX, Topaz (`.azw`/`.tpz`), Print Replica, PalmDoc (`TEXtREAd`) and other
PDB/PRC variants, dictionaries, and anything with DRM. They are refused as `not_mobi` or
`drm_protected`; a file the scan does not recognise (`.azw`, `.kfx`) is not picked up at all.

## Ingest

- **Magic bytes first**, as for PDFs and comics ([pdf-comics.md](pdf-comics.md)): the declared
  type (or the inbox file's extension) must agree with the first bytes, so a zip renamed `.mobi`
  is refused as `not_mobi`.
- **DRM check before any parser runs.** The server reads the PalmDB record list and each MOBI
  header itself, with bounded reads: record 0, and, in a joint file, the KF8 header after the
  `BOUNDARY` record. A non-zero encryption type in either is DRM (`drm_protected`). Only then is
  the book unpacked.
- **Uploads** (plain and into **Review before filing**) convert at finalize. A refused file
  fails its upload with the code and the plain message, and nothing is filed or left on disk.
  When both converter slots are busy, finalize answers `503 convert_busy` and keeps the upload,
  so it can be finalized again.
- **The inbox** converts in place under a dot-name the scan skips, files the EPUB, and only then
  deletes the original. A **DRM-protected file is left where it is**, as `.cbr` files are, and
  logged once; other unreadable files move to `_rejected` like any other bad file.
- **Metadata** comes from the MOBI EXTH header: title (updated title, else the full name),
  authors (`A & B` and `A; B` are two people), publisher, description, publication date,
  subjects, language, ISBN, and ASIN. The ASIN is kept in the EPUB as a
  `mobi-asin:` identifier; matching uses the ISBN, title, and authors as for any EPUB.
- **Cover**: the book's cover image, through the same bounded decoder as every EPUB cover.
- **Matching** is exactly the EPUB rules, so an AZW3 of a book you already have joins that work
  (as `book-<sha>.epub` next to the existing `book.epub`).
- The review sheet marks the file *Converted from MOBI to EPUB* (or AZW3).

### The original is not kept

Only the EPUB is stored. Keeping the MOBI would add a format nothing in Lectern can open
(the reader, KOReader sync, OPDS, and alignment all use the EPUB), double the storage per book,
and give two files with different KOReader document ids for one edition. Conversion is
deterministic (fixed zip timestamps and order, an identifier derived from the original's
SHA-256), so adding the same MOBI twice is caught as a duplicate like any file.

## The converter

KindleUnpack, from the maintained [`mobi`](https://pypi.org/project/mobi/) package (pure Python,
GPL-3.0, no native code), unpacks the book; Lectern builds the EPUB from what it unpacked.

| Limit | Value |
| --- | --- |
| Where it runs | a child process, `python -I -m lectern.ingest.kindle`, with a clean environment |
| Memory | 1.5 GiB address space (`RLIMIT_AS`) |
| CPU | 60 s (`RLIMIT_CPU`), `nice` +10 |
| Files written | at most 768 MiB each (`RLIMIT_FSIZE`), inside a temporary folder; no core dumps |
| Wall clock | 90 s, then the child is killed (`mobi_too_complex`) |
| Concurrency | shares two parser slots with PDF reading; a caller waits 10 s for one (`convert_busy`) |
| Documents | at most 5 000 |

Every XHTML document is rebuilt from an allowlist of elements and attributes in the XHTML
namespace: no scripts, forms, frames, objects, event handlers, meta refreshes, or remote
images. Inline styles that could load anything (`url()`, `image-set()`, CSS escapes) are
dropped; stylesheets have their escapes decoded and then lose `@import` and every remote
reference. SVG image files are not carried over, and inline SVG is sanitized like the rest; old Mobipocket markup (`<font>`, `<center>`, page breaks,
`height`/`width` spacing) becomes HTML5 with CSS. Links are kept only to documents and ids that
exist, or to `http`, `https`, and `mailto`. A MOBI7 book is split into chapters at its page
breaks, with its table of contents taken from the NCX; a KF8 book keeps its own documents,
stylesheets, fonts, and nav. The result passes epubcheck 5.4 with no errors or warnings on the
test fixtures (tests/fixtures/kindle).

Whatever the file does, the worst case is a refused upload: a malformed, truncated, or hostile
file fails as `not_mobi` or `mobi_too_complex` inside the limits above.

## Error codes

| Code | Meaning |
| --- | --- |
| `drm_protected` | DRM-protected; refused with the message above |
| `not_mobi` | not a MOBI/AZW3, or one that cannot be read (truncated, corrupt, Print Replica) |
| `mobi_too_complex` | the converter hit its time or memory limit |
| `convert_busy` | both converter slots were taken; retry (HTTP 503) |
