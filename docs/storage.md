# Storage

On a Raspberry Pi the SD card or USB disk fills up quietly: besides the books you added, Lectern
keeps data-saver copies of audiobooks, OCR copies of scans, converted EPUB editions, generated
covers, text documents of PDFs, read-aloud EPUBs, the Trash, and backups. **Admin → Storage**
(owner only) shows where the space goes and removes, safely, what Lectern can make again.

Members and readers never see it: the page, its API (`/api/admin/storage…`), and cleanup jobs
are the owner's alone, and nothing about storage is offered over OPDS or to AI apps (MCP).

## Free space

Each disk Lectern stores on is shown once, with its free and total space, read live on every
visit: the data folder, the library, and the backup target (when two share a disk, they share a
row). Below it, how free space has changed per day, from the samples the background
measurement keeps (at most one an hour, the last 120).

A disk with less free space than `LECTERN_STORAGE_WARN_FREE_PERCENT` (default 10) is marked
low. The health page (when it arrives) is meant to use the same setting.

## Where the space goes

Measuring walks every file once, so it never happens on a page load. It runs in the
background every six hours, when the page is first opened without a measurement, when you
press **Measure now**, and when a cleanup finishes while the page is open; the page shows when
it last ran. On a Pi with a few thousand books it takes seconds.

| Category | What it counts |
|---|---|
| Originals | The files you added (EPUB, PDF, audio), each book's `book.json` and cover art, and anything else in a book's folder that is not below. MOBI and AZW3 books are converted to EPUB when they are added and the Kindle file is not kept, so that EPUB is the original. Files in the library that belong to no book are counted here as "unfiled". |
| Generated audio | Narrated audiobooks (their tracks) and data-saver copies (`derived/audio/`). |
| OCR copies | `derived/ocr-<sha>.pdf` and `.json` of scanned PDFs. |
| Converted editions | The PDF→EPUB edition (`book-converted-from-pdf.epub`) and its report, rules, analysis, and previews, and the read-aloud EPUB (`derived/read-aloud.epub`). |
| Covers | Generated covers (`derived/cover-<key>.jpg`) and the cover backups Undo keeps. |
| Derived text | PDF text documents, sentence maps and alignments, the search index, the OCR scan cache. |
| Trash | Books and files in the Trash (kept 30 days). |
| Backups | The restic repository and any `.staging` left on the backup target, and `pre-restore-<time>/` folders kept by `lectern restore`. Restic always runs with `--no-cache`, so there is no restic cache on the data disk to count. |
| Other | The database and its WAL, upload parts, logs, the inbox, imports waiting for review, scratch space, and downloaded models and voices. |

What the measurement leaves out: **symbolic links** are never followed or counted (the page
says how many it met), as with backups; a file reachable through two hard links counts once;
sizes are file sizes, so a disk's "used" figure (which includes the file system's own blocks and
everything that is not Lectern's) is larger than the total here. The largest books are listed
with their size across categories.

## Cleaning up

Every cleanup shows a preview first: each item it would remove, its files, and the space it
frees. Untick anything to keep it, tick **I understand: these files are deleted for good**, and
confirm on the button, which names the action, the count, and the size.

| Cleanup | Removes | Afterwards |
|---|---|---|
| Empty the Trash (optionally only items older than N days) | Books and files in the Trash, as **Delete forever** does | Gone for good. |
| Data-saver copies (all, or one book from the largest books) | `derived/audio/` copies | Books play from the originals; make copies again from Admin → Data-saver copies or the book's page. |
| Generated covers (all, or one book) | `derived/cover-*.jpg` | Drawn again the next time the book is shown. |
| Read-aloud EPUBs (all, or one book) | `derived/read-aloud.epub` and its sidecar | Built again when someone downloads one. A book whose EPUB is being built right now is left alone. |
| Leftovers of interrupted runs | Upload parts no upload is using (older than 10 minutes), staged results and artifacts of finished jobs, scratch files older than a day, and the backup target's `.staging` from a killed backup | Nothing to make again. |
| Pre-restore folder (one at a time, by name and date) | `<data>/pre-restore-<time>/` | The files that restore replaced are gone; only do this once you are sure the restore was right. |

Never offered, and refused if asked for: originals, narrated audiobooks (their text-to-speech
would cost time or money to redo), OCR copies (a scan's text and read-along are built from
them; redo OCR on the book's page instead), PDF→EPUB editions (remove one on the book's page),
the database, and the restic repository (backups keep what retention says; change it in
Admin → Backups).

### How a cleanup runs

Confirming queues one cleanup job on the server's own job queue (never offered to workers),
holding the keys of the items you confirmed; one cleanup at a time. The job:

- removes each item only if it is still eligible: a book restored from the Trash since the
  preview, or a copy whose book was trashed, is skipped and counted;
- takes the library lock for anything in the library, so it never races an edit, an import, or
  a background build;
- never follows a symbolic link: a link is removed itself, never what it points to;
- waits while a backup runs (here or in another process holding the backup target) or a
  restore was interrupted: the job goes back to the queue and tries again five minutes later,
  and the page says why;
- records each item it finished, so a server restart resumes where it stopped and nothing is
  done twice;
- is in the audit log twice: `storage.cleanup` (who asked, what, how much) and
  `storage.cleanup.done` (what was removed, skipped, and freed).

Cancel a waiting or running cleanup like any job (`POST /api/jobs/{id}/cancel`); what it already
removed stays removed.
