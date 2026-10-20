# Read-aloud (milestone 4)

Once a work has an EPUB, its audiobook tracks, and a finished alignment (a sentence map; see
[`contracts.md`](contracts.md)), Lectern offers two ways to read and listen at once:

- **Read-along in the browser** (`/app/read/<work id>`): the text with the sentence being
  spoken highlighted.
- **Narrated EPUB** (`/library/works/<work id>/read-aloud.epub`): a copy of the book with
  EPUB 3 Media Overlays and the audio embedded, for reading apps that support them.

Both appear in a work's **Formats** list (the expanded card on Home and the book sheet in the
Library) with their real state:

| State | Shown as | Meaning |
| --- | --- | --- |
| `unavailable` | dashed row with the reason | no audio, no finished alignment (or still aligning), alignment out of date, or a file missing from the library folder |
| `not_generated` | **Prepare** | inputs are ready; the narrated EPUB has not been built for them yet |
| `generating` | spinner, "Building…" | a build is running in the background; the row polls until it ends |
| `ready` | size and **Download** | the cached file matches the current inputs |
| `failed` | the reason and **Retry** | the last build for these inputs failed (kept in memory until the inputs change or a retry) |

Screenshots: [read-along playing](screenshots/readaloud-readalong-playing.png),
[scrolled away](screenshots/readaloud-readalong-detached.png),
[listen mode](screenshots/readaloud-listen-mode.png),
[mobile, dark, reduced motion](screenshots/readaloud-mobile-dark.png),
[library sheet](screenshots/readaloud-library-sheet.png), and the
[Formats states](screenshots/readaloud-formats-states.png) (ready, building, failed,
unavailable).

## Web read-along

- Click or tap a sentence to play from it. The player plays the work's tracks back to back on
  one timeline and highlights the sentence at the current audio time (every animation frame).
- **Follow the narration** (on by default) keeps the current sentence near the top third of
  the screen. Scrolling by hand (wheel, touch, scrollbar, or keyboard) while it plays stops
  the following; a **Back to narration** button (with an arrow toward it) brings it back, and
  scrolling back to the narration resumes following on its own. Pressing play also returns to
  it.
- **Read along** and **Listen** modes have separate speeds (0.75×–3×), remembered per browser:
  following text usually wants a gentler pace than listening alone. Listen mode hides the text
  and shows the cover.
- Keyboard: `Space` or `K` play/pause, `←`/`→` (or `J`/`L`) previous/next sentence, `<`/`>`
  slower/faster, `F` follow on/off. Lock-screen and headset controls work through the Media
  Session API (previous/next = sentence, seek ±10 s).
- Screen readers: the player is a labelled region with named buttons, the position slider has
  a spoken value ("1 minute 23 seconds of 12 minutes 50 seconds"), the current sentence has
  `aria-current`, and moving by sentence while paused announces the sentence number and text in
  a polite live region. Nothing is announced during playback, so it does not talk over the
  narration.
- Reduced motion: scrolling jumps instead of animating and the highlight has no transition.
  Forced-colours mode uses the system `Mark` colours. Below 900 px the player sits above the
  bottom navigation.
- Position: the current sentence is saved as the work's progress (`source: web` in read-along,
  `audio` in listen mode) on pause, every 15 s while it changes, after stepping while paused,
  and when the tab is hidden or closed. Opening the view resumes there without autoplay.

The browser never receives EPUB markup: `GET /api/works/{id}/text/{spine}` returns the
normalised text of a spine item split into blocks, in the same coordinates as the sentence
map, and the client wraps sentences in spans itself.

## Narrated EPUB (EPUB 3 Media Overlays)

Generated once per set of inputs and cached next to the work:

```text
<library>/<Author>/<Title>/book.epub                   original, never modified
<library>/<Author>/<Title>/derived/read-aloud.epub     generated
<library>/<Author>/<Title>/derived/read-aloud.json     sidecar: cache key + output facts
```

- **Cache key** (in the sidecar): generator version, the EPUB's SHA-256, the audio fingerprint,
  and a SHA-256 over the sentence list (`n`, spine index, character span, audio span, matched).
  A file is served only when the sidecar key equals the current key and the file has the
  recorded size. A new alignment, a replaced EPUB or track, or a generator change rebuilds it.
- **Publishing**: a build writes `derived/.read-aloud.epub.<random>.tmp`, fsyncs it, renames it
  over `read-aloud.epub`, then replaces the sidecar the same way. Nothing in `derived/` is
  rewritten in place, so a download in progress keeps reading the complete old file and a
  crashed or failed build leaves the previous file untouched. One build per work runs at a
  time.
- **The original is only read.** `book.epub`, its SHA-256, and its KOReader partial MD5 stay the
  same, so KOReader keeps syncing it as the same document. Before building, Lectern re-hashes
  the EPUB on disk and refuses (`alignment_stale`) if it is not the file the alignment used.

What the generated file contains:

- Every sentence wrapped in `<span id="s{n}">` (`lectern-s{n}` if the document already uses
  ids of that shape). Only wrappers are added: each spine item's normalised text is compared
  before and after on every build. A sentence across inline markup splits that element into two
  shallow copies (the copy drops `id`); a sentence across two blocks gets one wrapper per block
  (`s{n}`, `s{n}-2`, …) and its audio clip is shared between them by length.
- One SMIL file per spine item that has audio (`lectern/smil/NNNN.smil`), one `<par>` per
  matched sentence (part), `clipBegin`/`clipEnd` relative to the embedded track file. A clip
  that would cross into the next track is cut at the end of its own track.
- The tracks under `lectern/audio/track-NNN.mp3|m4a` (stored, not recompressed), so the file is
  roughly EPUB size plus audio size.
- OPF: manifest items, `media-overlay` on the spine items, `media:duration` per overlay and in
  total, `media:active-class` with a small stylesheet, and a new `dcterms:modified`.
- **EPUB 2 sources are upgraded to EPUB 3**, because Media Overlays do not exist in EPUB 2:
  `version="3.0"`; `opf:file-as`/`opf:role` become `refines` metadata; `opf:scheme`,
  `opf:event`, and `xsi:type` are dropped; one `dc:date` is kept (the publication date); the
  cover gets `properties="cover-image"`; a navigation document is generated from the NCX (the
  NCX stays for older readers); XHTML documents get the HTML5 doctype, lose `http-equiv` values
  HTML5 rejects (`Content-Style-Type`), and have named entity references written out as
  characters.
- Audio must be MP3 or AAC (`audio/mpeg`, `audio/mp4`), the EPUB 3 core audio types. Other
  formats fail with a clear message; transcoding is not done.

Validation: every build checks the result structurally (zip with `mimetype` first and stored,
every manifest item present, every SMIL fragment present in its document, every audio file
present, clips ordered, every XHTML document well-formed). CI runs
[epubcheck](https://github.com/w3c/epubcheck) 5.4.0 (pinned, checksum-verified) on the
Gift of the Magi fixture (an EPUB 2 file, so the upgrade path is covered) and on a two-chapter
EPUB 3: both report no errors and no warnings. To run it locally:

```sh
curl -sSfLO https://github.com/w3c/epubcheck/releases/download/v5.4.0/epubcheck-5.4.0.zip
unzip -q epubcheck-5.4.0.zip
EPUBCHECK_JAR=$PWD/epubcheck-5.4.0/epubcheck.jar uv run pytest tests/test_readaloud.py
```

### Which apps play it

Media Overlay support is uncommon. Based on the vendors' and projects' documentation (not
tested by Lectern beyond epubcheck):

| App | Plays the narration with highlighting? |
| --- | --- |
| [Thorium Reader](https://www.edrlab.org/software/thorium-reader/) (Windows, macOS, Linux) | Yes ("Activate Readaloud") |
| BookFusion, Colibrio Reader | Yes (listed by the Storyteller project as working with sentence-level Media Overlay EPUBs) |
| Calibre viewer | Limited, recent support |
| Apple Books | No for this file: Apple documents read-aloud for **fixed-layout** books only; a reflowable book opens as a normal EPUB |
| KOReader | No. KOReader has no audio engine and declined Media Overlay support ([koreader#9548](https://github.com/koreader/koreader/issues/9548)); the file opens as an ordinary EPUB |
| Kobo and Kindle e-readers | No |

On a KOReader device, keep reading the plain EPUB from the OPDS catalogue and use the web
read-along or a supporting app for listening. The narrated EPUB has a different partial MD5
(and different DOM paths) from `book.epub`, so KOReader treats it as a separate document;
progress from it is not merged with the plain EPUB's.

## Limits and known gaps

- Sentence-level highlighting only (the sentence map has no word timings).
- Text that the frozen normalisation drops (unresolved named entities such as `&nbsp;` in EPUB
  2 files) is not part of any sentence; the narrated copy writes those entities out as
  characters, so its own normalised text can differ from the original's there.
- Build failures are remembered in memory only; after a restart the next request tries again.
- Builds run synchronously on the first download if nothing is cached, and in a single
  background thread when started from the web app. Very large audiobooks take as long as
  copying their audio once.
- Not in this milestone: merging progress across the plain and narrated EPUBs, and an OPDS
  link to the narrated EPUB. The track list, range streaming, sentences, and progress the read-along uses are milestone 2's
  (`lectern.routes.api.works`, `lectern.routes.library`), which replaced this milestone's
  interim `lectern.routes.media`. The narrated EPUB download accepts OPDS app passwords and
  counts against the hourly download caps. The plain web reader is at `/app/reader/<id>`;
  read-along keeps `/app/read/<id>`. The KOReader xpointer ↔ sentence projection came with
  milestone 3 ([`alignment.md`](alignment.md)).
