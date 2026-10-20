# Lectern contracts (Step 0)

The shared schema and interfaces that milestones 2 (audio + shelves + invites + resumable
uploads), 3 (alignment workers) and 4 (positions and read-aloud) build on. Step 0 ships the
database tables (migration `0005`), the sentence-map schema (`lectern.sentence_map`), the
position interface (`lectern.positions`), the visibility predicates (`lectern.access`), the
trusted-proxy client-address rule (`lectern.security.proxy`, **implemented**), and this
document. Milestone 2 implements tracks, range streaming and the low-bitrate variant,
sentences, progress with cross-device hand-off, shelves, invites, resumable uploads, and the
notification and download-cap routes (`lectern.routes.api.works`, `…shelves`, `…uploads`,
`…notifications`, `lectern.routes.invites`, `lectern.routes.library`, migration `0006`).
Milestone 4 implements spine text and the read-aloud routes (see [`read-aloud.md`](read-aloud.md));
its interim `lectern.routes.media` was replaced by milestone 2's routes. Milestone 3 implements
the jobs routes, worker credentials, the whole worker protocol, the alignment diagnosis in
the track list, and the KOReader progress projection (see [`alignment.md`](alignment.md)).

Conventions used throughout:

- Every `/api/*` route needs a browser session; writes also need the CSRF token and a
  same-origin `Origin`, exactly like the existing API. `/library/*` routes also accept an OPDS
  app password (Basic auth), like downloads and covers today. `/api/worker/*` accepts only a
  worker credential (see [Worker protocol](#worker-protocol)).
- Errors are `{"detail": "<code or message>"}`. Machine-readable codes are lowercase snake
  case (`lease_lost`); everything else is prose.
- Timestamps are ISO 8601 UTC with a `Z` suffix. Durations and positions in audio are seconds
  as floats. Hashes are lowercase hex.
- A work the caller may not see is reported as `404`, never `403`, so readers cannot probe the
  library.

## Coordinate systems

A work has two timelines and the sentence list joins them.

```diagram
 text timeline                  sentences                   audio timeline
 (spine_index, char)            n = 0,1,2,...               seconds from start of track 0
┌───────────────────┐        ┌────────────────┐           ┌──────────┬──────────┬──────┐
│ item 0 │ item 1 │ …│ ◀────▶ │ n: spine, start│ ◀───────▶ │ track 0  │ track 1  │ …    │
└───────────────────┘        │    end, audio  │           └──────────┴──────────┴──────┘
                             └────────────────┘            offset_s = sum of earlier durations
```

- **Text position** `(spine_index, char)`: `spine_index` is the 0-based index into the OPF
  spine (`lectern.ingest.epub.read_spine`); `char` is a code-point offset into the normalised
  text of that spine item. Normalisation is defined by `lectern.positions.normalize_spine_text`
  and is frozen: body text only, `<script>`/`<style>` dropped, every whitespace run collapsed
  to one space, trimmed. Changing it would invalidate every stored sentence map.
- **Sentence position** `(sentence_n, offset)`: the canonical form stored in `work_progress`.
  `offset` is characters into the sentence, clamped to its length.
- **Audio time** `t`: seconds on the work timeline. Track `i` covers
  `[offset_s, offset_s + duration_s)`; `offset_s` is the sum of the previous tracks'
  durations.
- **KOReader xpointer**: `/body/DocFragment[k]/...`; `k` is `spine_index + 1`.

## Database

All tables are created by migration `0005_contracts`; models live in `lectern.models`. Enum
columns store the enum *name* (`'LEASED'`, `'MEMBER'`), which is why CHECK constraints quote
upper-case values. Timestamps are `UTCDateTime` (aware UTC in, aware UTC out).

| Table | Purpose | Key constraints |
| --- | --- | --- |
| `audio_tracks` | One row per audio file of a work, in play order | unique `(work_id, position)`, `(work_id, rel_path)`, `(work_id, sha256)`; `position, duration_s, offset_s >= 0`; `chapters` JSON `[{"title", "start_s"}]` relative to the track |
| `sentences` | Sentence list and, when aligned, audio span | unique `(work_id, n)`; `char_end > char_start >= 0`; `audio_start_s`/`audio_end_s` both null or both set with end ≥ start; `matched ⇒ audio set`; indexes `(work_id, spine_index, char_start)` and `(work_id, audio_start_s)` |
| `alignments` | Current alignment of a work (one row per work) | unique `work_id`; `coverage` null or in `[0, 1]`; `epub_sha256` and `audio_fingerprint` record the inputs; `job_id` → `jobs` (SET NULL) |
| `work_progress` | Canonical position per user and work | unique `(user_id, work_id)`; `sentence_n, sentence_offset >= 0`; `source ∈ {KOREADER, WEB, AUDIO}`; `raw` keeps what the device sent |
| `reading_progress` | **unchanged**: KOReader's own record keyed by document hash | as before |
| `invites` | Single-use sign-up links | unique `token_hash`; `role ∈ {MEMBER, READER}` (never OWNER); `used_at`/`used_by` both null or both set |
| `shelves` | Named collections owned by a user | unique `(owner_id, name)`; `rule` JSON reserved for smart shelves (`NULL` = manual) |
| `shelf_items` | Shelf membership | PK `(shelf_id, work_id)`; `position` for manual order |
| `shelf_grants` | Reader access to a shelf | PK `(shelf_id, user_id)`; `permission ∈ {VIEW, EDIT}` |
| `worker_credentials` | Hashed bearer keys for external workers | unique `key_hash`; `revoked_at` |
| `jobs` | Durable job queue with leases | `attempts >= 0`, `max_attempts >= 1`; `LEASED ⇒ lease_token_hash set`; index `(status, run_after, priority)`; **partial unique** `(work_id, kind) WHERE status IN ('QUEUED','LEASED')` |
| `upload_sessions` | Resumable browser uploads (chunks ≤ 50 MiB) | unique `public_id`; `size > 0`; `0 < chunk_size <= 52428800`; `0 <= received_bytes <= size`; `COMPLETED ⇒ received_bytes = size AND inbox_name set`; indexes `(user_id, status)`, `(status, expires_at)` |

Deletion behaviour (all `ON DELETE` at the SQL level; `PRAGMA foreign_keys=ON` is set by
`lectern.db`):

- Deleting a **work** removes its tracks, sentences, alignment, progress, shelf items, and
  jobs.
- Deleting a **user** removes their progress, grants, upload sessions, and *the shelves they
  own* (and so the grants on those shelves). The owner UI should warn before deleting a member
  who owns shelves. Their invites, credentials, and jobs survive with `created_by`/`used_by`
  set to NULL. The partial-upload file of a deleted user's session is removed by the sweeper
  (it finds `.part` files with no row).
- Deleting a **shelf** removes its items and grants; works are untouched.
- Deleting a **worker credential** is not offered; revoke it (`revoked_at`) so job history keeps
  `leased_by_id`.

The partial unique index on `jobs` is declared with `sqlite_where`; on another database it would
need the equivalent `postgresql_where`. Lectern is SQLite-only today.

## Sentence map

The artifact an alignment worker produces for a work. Stored at
`<LECTERN_DATA_DIR>/align/<work_id>.json` (`lectern.sentence_map.map_path`), written atomically
(`write_sentence_map`), and validated on every upload with `SentenceMap.model_validate_json`.
The `sentences` and `alignments` rows are rebuilt from it; the file is the source of truth.
Example: `tests/fixtures/magi/sentence_map.json`.

```json
{
  "version": 1,
  "work_id": 12,
  "epub_sha256": "<sha256 of the EPUB file>",
  "audio_fingerprint": "<sha256 of the track sha256s joined by \\n, in position order>",
  "model": "whisperx-large-v3",
  "language": "en",
  "generated_at": "2026-10-20T10:00:00Z",
  "tracks": [
    {"position": 0, "sha256": "<track sha256>", "duration_s": 802.5, "offset_s": 0.0}
  ],
  "sentences": [
    {"n": 0, "spine_index": 2, "char_start": 0, "char_end": 34,
     "audio_start_s": 0.25, "audio_end_s": 2.78, "matched": true,
     "text": "One dollar and eighty-seven cents."}
  ]
}
```

Rules enforced by the schema (each one is a test case in `tests/test_contracts.py`):

- `version` is `1`; unknown fields are rejected (`extra="forbid"`).
- `tracks` are numbered `0..n-1` in order; each `offset_s` equals the sum of the previous
  durations within 0.05 s; `audio_fingerprint` equals
  `lectern.sentence_map.audio_fingerprint([t.sha256 ...])`.
- `sentences` are numbered `0..n-1` in reading order: `(spine_index, char_start)` never
  decreases and sentences in the same spine item do not overlap. `char_end` is exclusive and
  greater than `char_start`.
- `audio_start_s`/`audio_end_s` are both absent or both present, `end >= start`, `end` within the
  total audio duration, and matched sentences' starts never go backwards. `matched` requires
  audio times. An unmatched sentence may still carry a guessed time span.
- `text`, if present, must have exactly `char_end - char_start` characters. It is for humans and
  debugging; the server never trusts it (M3 additionally checks `char_end` against the real
  spine item length).
- `coverage` is derived: matched sentences / all sentences.

## HTTP API

### Tracks — `GET /api/works/{work_id}/tracks`

```json
{
  "work_id": 12,
  "audio_fingerprint": "…",
  "total_duration_s": 802.5,
  "alignment": {"status": "succeeded", "model": "whisperx-large-v3", "coverage": 0.98},
  "tracks": [
    {"id": 7, "position": 0, "media_type": "audio/mpeg", "codec": "mp3",
     "duration_s": 802.5, "offset_s": 0.0, "size": 6420608, "sha256": "…",
     "chapters": [{"title": "Chapter 1", "start_s": 0.0}],
     "stream_url": "/library/tracks/7/stream"}
  ]
}
```

`alignment` is `null` when no alignment row exists. `tracks` is empty for a text-only work.
`404` if the work is not visible to the caller.

Implemented (milestone 2, `lectern.routes.api.works`). Milestone 3 adds to `alignment`:
`sentence_count`, `error`, `completed_at`, `span_coverage`, `diagnosis`, `warnings`
(`low_coverage`, `likely_abridged`, `stale`), and `suggested_model`; see
[`alignment.md`](alignment.md#coverage-diagnosis-abridgment). Tracks are the audio files under
the work directory (subfolders included, `derived/` and hidden entries excluded): imports from
the inbox and uploads place them under `audio/`, and the alignment rescan (at startup and every
`LECTERN_TRACK_RESCAN_SECONDS`) also picks up files copied there by hand.

### Streaming — `GET /library/tracks/{track_id}/stream`

Authenticated HTTP range streaming of one track; the `<audio>` element and KOReader-style
players seek with `Range`.

- Auth: session cookie or OPDS app password (Basic), the same options as file downloads.
- Without `Range`: `200`, full body, `Content-Length`, `Accept-Ranges: bytes`.
- With a single `Range: bytes=a-b` (or `a-`, `-n`): `206 Partial Content`,
  `Content-Range: bytes a-b/size`, `Content-Length: b-a+1`. Multiple ranges are not supported
  and are answered with the full `200` response. An unsatisfiable range is
  `416` with `Content-Range: bytes */size`.
- `ETag: "<sha256>"`, `Last-Modified`, and `If-Range` are honoured; `If-None-Match` yields
  `304`. `Cache-Control: private, max-age=3600`. `Content-Type` is the track's `media_type`.
- Byte positions longer than 19 digits are not a valid range: the header is ignored (`200`).
- Visibility is checked through the track's work (`404` otherwise). Streaming is **not**
  written to the audit log per request (a player issues many); starting playback of a work is
  recorded once per session in milestone 2's player.
- **Metering.** Metering is per **work**: an audiobook counts as one download per credential
  (session or app password) per hour, whichever tracks and ranges were played. The bytes
  actually sent (counted as the response body is written, not the declared `Content-Length`,
  so an open-ended `bytes=N-` the player abandons costs only what it received) are summed over
  all tracks of the work. Once they reach `stream_meter_bytes` (default 8 MiB) the work is
  admitted exactly once: caps, a `download` audit row (`target stream:work:<work_id>`,
  `detail stream`), and burst alerts apply. Later requests that hour are free. If admission
  is refused mid-response the body ends early; later requests for that work answer
  `429 download_cap` without writing more events, audit rows, or notifications while the
  credential stays over the cap.

#### Optional low-bitrate variant (implemented in milestone 2)

Away from home, every byte still crosses the home uplink (over Tailscale or any other route), so
a 320 kb/s MP3 audiobook is slow to seek on a phone. Milestone 2 *may* add a
`variant=low` query parameter that streams a once-generated ~64 kb/s Opus (fallback AAC)
rendition. The contract leaves room for it without requiring it:

- Originals are never modified, re-encoded in place, or deleted; `GET /library/tracks/{id}/stream`
  without `variant` always serves the original bytes, and the plain download continues to
  work.
- The derivative lives in the work directory at
  `derived/audio/<original sha256>.<opus|m4a>`, keyed by the original's hash so a replaced
  track invalidates it. It is produced by a `transcode` job (same queue, `kind = "transcode"`),
  on the server with `ffmpeg` when available or by an external worker. **As implemented**, one
  job covers the whole work, because the active-job index allows one active job per work and
  kind: payload `{"tracks": [{"track_id", "sha256"}], "codec", "bitrate_kbps"}`, `result.done`
  counts finished tracks. A `<file>.sha256` sidecar holds the rendition's own hash for its
  `ETag`. Default codec is AAC in `.m4a` (plays on iOS), `LECTERN_AUDIO_LOW_CODEC=opus` for Opus.
  `POST /api/works/{id}/transcode` (owner, member) queues it; `GET …/tracks` reports
  `transcode: {status, progress, total} | null`. No schema is needed: existence of the file *is*
  the state; the track object in `GET /api/works/{id}/tracks` gains
  `"variants": {"low": {"codec": "opus", "bitrate_kbps": 64, "size": …, "stream_url": "…?variant=low"} | null}`.
- Requesting `variant=low` before it exists answers `404 variant_unavailable`; clients fall
  back to the original. `ETag` for the variant is its own file hash. Sentence timings are on
  the work timeline and do not depend on which rendition plays; the encoder must preserve
  duration (no trimming of leading silence).

### Sentences — `GET /api/works/{work_id}/sentences`

Query, exactly one of:

- `from=<n>&to=<n>`: sentences `from <= n < to` (max 2000 per call, default `to = from + 500`).
- `t=<seconds>&around=<k>`: the sentence spoken at `t` on the work timeline plus `k` sentences
  on each side (`k` ≤ 50). If nothing is matched yet, `sentences` is empty.
- `spine=<i>&char=<c>`: the sentence containing that text position plus the next 100.

```json
{
  "work_id": 12,
  "count": 1830,
  "alignment": {"status": "succeeded", "model": "…", "coverage": 0.98},
  "sentences": [
    {"n": 0, "spine_index": 2, "char_start": 0, "char_end": 34,
     "audio_start_s": 0.25, "audio_end_s": 2.78, "matched": true}
  ]
}
```

Sentence text is not returned; clients take it from the spine text route below (or from the
EPUB with the same normalisation). `count` is `0` and `sentences` is `[]` for a work without a
sentence map. Implemented in milestone 4; a request with none or several of the three forms is
`400`.

### Spine text — `GET /api/works/{work_id}/text/{spine_index}` (milestone 4)

The normalised text of one spine item of the aligned EPUB (the first EPUB if there is no
alignment), split into display blocks, so the web read-along needs no EPUB parsing and no
markup reaches the browser:

```json
{
  "work_id": 12, "file_id": 31, "spine_index": 2, "length": 29283,
  "blocks": [{"kind": "p", "start": 0, "text": "One dollar and eighty-seven cents. …"}]
}
```

`start` is the code-point offset of the block's first character in the spine item's normalised
text (the sentence-map coordinates); `kind` is `p`, `h1`–`h6`, `li`, `quote`, or `pre`. Blocks
are the text grouped by nearest block-level element; the single spaces between blocks are not
in any block. `404` for an invisible work, a work without an EPUB, or an index past the spine.

Note on the frozen normalisation: the parser keeps unresolved named entity references
(`&nbsp;`, `&mdash;` in EPUB 2 files without a loaded DTD) as entity nodes, and
`normalize_spine_text` skips them, so `a&nbsp;b` normalises to `ab`. Aligners see the same text,
so maps stay consistent; the narrated EPUB writes those entities out as characters.

### Progress — `GET` / `PUT /api/works/{work_id}/progress`

The canonical position of the caller in a work. KOReader keeps writing `reading_progress`
through `/syncs/progress` exactly as today; milestone 4 projects those writes onto
`work_progress` (source `koreader`) when the document hash maps to a work with sentences.

`GET` →

```json
{
  "work_id": 12,
  "position": {"sentence_n": 41, "offset": 12},
  "source": "audio",
  "raw": "133.7",
  "device": "Living room",
  "received_at": "2026-10-20T10:00:00Z",
  "text": {"spine_index": 3, "char": 1024},
  "audio_s": 133.7,
  "percent": 2.3
}
```

`position`, `source`, `raw`, `device`, `received_at` are `null` when nothing is stored.
`text` and `audio_s` are the projections through `lectern.positions` (`audio_s` is `null` when
the sentence is unmatched and no neighbour is either). `percent` is `sentence_n / count`.

`PUT` body: `{"source": "web" | "audio", "device": "…", ...}` plus exactly one of

- `{"sentence_n": 41, "offset": 12}`
- `{"audio_s": 133.7}` (converted with `SentenceIndex.at_time`)
- `{"spine_index": 3, "char": 1024}` (converted with `SentenceIndex.at_text`)

Response is the same shape as `GET`. Rules:

- Last write wins; `received_at` is the server clock. There is no client timestamp and no
  merge: the device that wrote most recently defines the position (this matches what the
  KOReader sync protocol does).
- `raw` is stored as received: the xpointer, the seconds, or `"<n>:<offset>"`.
- `409 no_sentences` when the work has no sentence list yet. The schema allows a text-only
  sentence list (all `matched = false`) so milestone 4 can segment at ingest if progress before
  alignment turns out to matter.
- `404` if the work is not visible; `source: "koreader"` is rejected (`422`), that source is only
  written by the sync projection.

Status: implemented (milestone 2's `lectern.progress`, used by the web reader, the audio player,
and milestone 4's read-along). A `sentence_n` past the last sentence is `422`; `offset` is
clamped to the sentence. Additions beyond the shape above:

- Every EPUB gets a **text-only sentence list at ingest** (all `matched = false`), so progress
  works before alignment. Until a work is aligned, audio ↔ text conversion is proportional
  over the total duration and the response says `"estimated": true`.
- `PUT` with `source: "web"` may carry `raw` (≤ 1024 chars), the reader's own precise locator
  (an EPUB CFI), returned as `raw` so the same browser restores the exact page.
- `count` (sentences) and `total_duration_s` are included; `percent` is a fraction `0..1`.
- `koreader`: KOReader's latest `reading_progress` for the work's EPUB, as a read-only hand-off
  candidate `{source: "koreader", device, received_at, percent, raw, text, audio_s, estimated}`
  (text from the xpointer's `DocFragment` plus the percentage, so `estimated: true`). KOReader
  progress itself is unchanged and still keyed by document hash.
- **Hand-off rule (client):** a device offers "<device>, <time>: continue here?" only when a
  position newer than the last one it wrote or accepted exists, comes from another device, and
  is at least 0.5 % of the book (reading) or 20 s (listening) away. Clients save only positions
  the reader moved to, so opening a book never overwrites a newer position.
- **KOReader projection** (milestone 3, `lectern.routes.kosync`): a `PUT /syncs/progress`
  for a document hash whose EPUB is the exact edition the work was aligned against (and whose
  work the caller can see) also writes `work_progress` (source `koreader`), and
  `GET /syncs/progress/{document}` answers with a newer web/audio position converted back to
  an xpointer for that document (`device_id` `lectern-web` / `lectern-audio`). Other editions
  and hidden works sync exactly like koreader-sync-server. KOReader's own `reading_progress`
  row and its document-hash mapping are unchanged.

### Read-aloud EPUB — `GET /library/works/{work_id}/read-aloud.epub`

The work's EPUB with EPUB 3 Media Overlays added: every sentence wrapped in
`<span id="s{n}">`, one SMIL `<par>` per matched sentence pointing at the embedded audio
tracks. Readers with Media Overlay support (Thorium; BookFusion and Colibrio are reported to
work) play it with highlighting. **KOReader does not support Media Overlays and Apple Books
plays read-aloud only for fixed-layout books**; both open the file as an ordinary EPUB (see
[`read-aloud.md`](read-aloud.md#which-apps-play-it)). EPUB 2 sources are upgraded to EPUB 3.
Decisions:

- The audio is embedded, so the file is roughly EPUB + audio size; it is generated on first
  request and cached **next to the work**, at
  `<LECTERN_LIBRARY_DIR>/<work.dir_path>/derived/read-aloud.epub` (i.e.
  `/library/<Author>/<Title>/derived/`), keyed by `(epub_sha256, audio_fingerprint)` plus a
  generator version and a SHA-256 over the sentence list (so a re-alignment of the same files
  also rebuilds) in a sidecar `derived/read-aloud.json`. It is published by writing a temporary
  file and renaming it, never rewritten in place. Only sentence maps live under
  `<LECTERN_DATA_DIR>/align/`; everything derived from a work's own files lives in its
  `derived/` directory so it travels (and is deleted) with the work. Scanners never ingest
  from `derived/`.
- `409` with `no_alignment` unless `alignments.status` is `succeeded`, `alignment_stale` when
  the alignment is stale or its EPUB/audio no longer match the work's files, `no_audio`, or
  `missing_file`. A build that cannot produce a valid file (for example FLAC audio, which EPUB
  3 cannot embed) is `422` with the reason. Generation on download is synchronous; the web app
  instead starts it in the background with `POST /api/works/{work_id}/read-aloud` (`202`, CSRF)
  and polls `GET /api/works/{work_id}/read-aloud`:
  `{"work_id", "status": "unavailable" | "not_generated" | "generating" | "ready" | "failed",
  "reason", "message", "readalong", "alignment", "download_url", "size", "generated_at",
  "duration_s"}`. Both need visibility of the work, like the download.
- Audited as a download (`target = "read-aloud:<work_id>"`), same as the plain EPUB. Auth and
  visibility as for the web download (session; OPDS does not list it yet).
- The derived file has a different `partial_md5` and different DOM paths from the original, so
  KOReader treats it as a separate document. Milestone 4 maps both documents' xpointers onto the
  same sentence coordinates; until then progress from the read-aloud copy is not merged.

### Jobs — `GET /api/jobs`, `GET /api/jobs/{job_id}`, `POST /api/works/{work_id}/align`, `POST /api/jobs/{job_id}/cancel`

Owner and member only (`403` for readers). `GET /api/jobs?status=&work_id=&kind=` lists newest
first (page size 50). Shape of one job:

```json
{
  "id": 3, "kind": "align", "work_id": 12, "status": "leased",
  "attempts": 1, "max_attempts": 3, "priority": 0,
  "run_after": "…", "created_at": "…", "started_at": "…", "finished_at": null,
  "lease": {"worker": "gpu-box", "expires_at": "…", "heartbeat_at": "…", "progress": 0.4},
  "error": null,
  "result": null
}
```

`status` is the lowercase enum value. `lease` is `null` unless leased; `result` is
`{"sentence_count", "coverage"}` after success. `POST …/align` with
`{"model": "whisperx-large-v3", "force": false}` returns `202` and the new job, or `200` and
the existing active job for that work (the partial unique index makes a duplicate impossible).
With `force: true` an existing active job is cancelled first. `POST …/cancel` moves a
`queued`/`leased` job to `cancelled`; a worker still holding the lease gets `409 lease_lost` on
its next call.

Implemented (milestone 3). `force: true` is refused with `409 job_running` while a worker
holds the active job's lease (cancel it explicitly instead); a queued job is replaced. Each job
also reports `model`, `audio_s` (its audio duration), and
`eta`: `null` until an alignment has been measured on this server, then
`{"seconds", "speed", "basis"}` from the median measured speed (see
[`alignment.md`](alignment.md#jobs-leases-and-the-eta)). `result.stats` holds the matcher's
statistics and timings. `GET /api/jobs` adds `measured_speed`.

### Shelves — `/api/shelves`

Visibility rule, implemented by `lectern.access`:

> Owners and members see the whole library. Readers see only works on shelves they hold a
> grant for.

`visible_works(user)` is a SQL predicate that milestone 2 applies to `/api/library`,
`/api/works/{id}`, `/api/continue`, covers, downloads, OPDS, and all routes in this document.
A role change takes effect on the next request.

| Route | Who | Behaviour |
| --- | --- | --- |
| `GET /api/shelves` | any | Owner: all shelves. Member: shelves they own plus shelves granted to them. Reader: granted shelves only. Each with `work_count`, `permission` (`owner`, `edit`, `view`) |
| `POST /api/shelves {name}` | owner, member | `201`; `409` on a duplicate name for that owner |
| `PATCH /api/shelves/{id} {name}` / `DELETE` | shelf owner or the owner role | `DELETE` cascades items and grants, never works |
| `GET /api/shelves/{id}` | anyone who can see it | shelf plus its works in `position` order (readers never see a shelf they are not granted) |
| `PUT /api/shelves/{id}/items {work_ids: [...]}` | manager or `edit` grant | Replaces the whole ordered list; members may add any work, a reader with `edit` only works they can already see, and never to a shelf another reader is granted (`403 shelf_shared_with_readers`) |
| `POST /api/shelves/{id}/items {work_id}` / `DELETE …/items/{work_id}` | manager or `edit` grant | Append / remove one |
| `GET /api/shelves/{id}/grants` | manager | `[{user, permission, granted_by, created_at}]` |
| `PUT /api/shelves/{id}/grants/{user_id} {permission}` / `DELETE` | manager | Upsert / revoke. Only the owner role may grant to a reader (`403 only_owner_grants_readers`). Granting to an owner or member is allowed but has no effect on visibility (it still lists the shelf for them) |

Smart shelves: `rule` is reserved (JSON, `NULL` today). When implemented, a non-null rule is
evaluated instead of `shelf_items`, and grants on a smart shelf expose whatever the rule
matches at request time.

### Invites — `/api/invites`, `/invite/{token}`

Owner-only management; the sign-up page is the only unauthenticated route added.

- `POST /api/invites {role: "member"|"reader", note, expires_in_hours (default 72, max 720)}`
  → `201 {id, role, note, expires_at, url, token}`. The token (`secrets.token_urlsafe(32)`) is
  shown once; only `sha256(token)` is stored.
- `GET /api/invites` lists pending, used, expired, and revoked invites; `DELETE /api/invites/{id}`
  sets `revoked_at`.
- `GET /invite/{token}` renders the sign-up form; `POST /invite/{token} {username, password,
  password_confirm}` creates the user with the invite's role, marks the invite used, signs the
  user in, and redirects to `/app`. Used, expired, revoked, or unknown tokens all answer `404`
  (not distinguished). The invite is claimed with one conditional `UPDATE` (unused, unrevoked,
  unexpired) in the same transaction as the new account, so concurrent submissions create at
  most one user; a username taken in the meantime is a `400` form error and leaves the link
  usable. The route shares the per-IP login rate limit. Passkeys/TOTP are set up
  afterwards from the normal security settings.

### Resumable uploads — `/api/uploads`

`POST /api/books/upload` (one request, whole file) stays for small EPUBs. A home uplink drops
long transfers, and some proxies cap request bodies (Cloudflare: 100 MB; Tailscale has no such
cap, but the 50 MiB chunk limit keeps one contract for every deployment), so milestone 2 adds a
resumable session upload for EPUBs and audiobooks. The schema
(`upload_sessions`, migration `0005`, plus `idempotency_key` and `last_chunk_sha256` in `0006`)
backs the routes below, implemented in milestone 2. **The inbox folder
remains the path for very large local imports** (a 2 GB audiobook on the same LAN is a file
copy, not an upload).

```diagram
 browser                                   server
   │ POST /api/uploads {filename,size,sha256}  │  row: ACTIVE, received_bytes=0,
   │ ─────────────────────────────────────────▶│  file: <data>/uploads/<public_id>.part
   │ ◀── 201 {id, chunk_size, expires_at} ─────│
   │ PUT /api/uploads/{id}/chunk  Upload-Offset: 0        X-Chunk-SHA256: …
   │ ─────────── ≤ 50 MiB body ───────────────▶│  verify hash, append, received += len
   │ ◀── 204  Upload-Offset: <received> ───────│
   │ … (network drops) …                       │
   │ GET /api/uploads/{id}  ──────────────────▶│
   │ ◀── 200 {received_bytes: 52428800, …} ────│  client resumes from that offset
   │ PUT …/chunk  Upload-Offset: 52428800 …  ─▶│
   │ POST /api/uploads/{id}/finalize ─────────▶│  received == size, sha256 matches,
   │ ◀── 200 {status: completed, queued: …} ───│  rename into inbox → ordinary ingest
```

Routes (session + CSRF + same-origin `Origin` on every write, owner and member only; a reader
gets `403`; a session that belongs to another user is `404`, so ids cannot be probed):

| Route | Behaviour |
| --- | --- |
| `POST /api/uploads` `{filename, size, media_type, sha256?}` | `201 {id, chunk_size, received_bytes: 0, expires_at}`. `size` ≤ `LECTERN_UPLOAD_MAX_BYTES` (`413` otherwise); `media_type` ∈ `application/epub+zip`, `audio/mpeg`, `audio/mp4`, `audio/ogg`, `audio/flac` (`415`). `chunk_size` is chosen by the server: `min(50 MiB, size)`. `sha256`, if given, is checked at finalize; if omitted it must be sent to finalize. `id` is `public_id`, 22 URL-safe chars. `expires_at = now + 24 h`, extended by each accepted chunk. |
| `PUT /api/uploads/{id}/chunk` | Headers `Upload-Offset: <int>`, `X-Chunk-SHA256: <hex>`, raw body ≤ `chunk_size` bytes (a larger body is `413` before anything is written). Every chunk except the last must be exactly `chunk_size` long (`400 short_chunk`). `Upload-Offset` must equal `received_bytes` (`409 offset_mismatch` with the current `Upload-Offset` header; the client reads it and resumes). A replay with `Upload-Offset + len == received_bytes` and the same `X-Chunk-SHA256` as the recorded last chunk answers `204` without writing (idempotent retry after a lost response); any other overlap is `409`. The body is read (bounded by `chunk_size`), then hashed and written to the `.part` file in the threadpool, never on the event loop; a mismatch leaves the file at the previous offset and answers `422 chunk_checksum`. Success: `204` with `Upload-Offset: <new received_bytes>`. |
| `GET /api/uploads/{id}` | `{id, filename, size, chunk_size, received_bytes, status, error, expires_at, inbox_name}`. The resume point is `received_bytes`. |
| `GET /api/uploads` | The caller's sessions (owner: everyone's, with `user`), newest first, so the UI can offer to resume or discard. |
| `POST /api/uploads/{id}/finalize` `{sha256?}` | Requires `received_bytes == size` (`409 incomplete` with `received_bytes`) and a declared or supplied `sha256` (`400 sha256_required`). The server hashes the whole `.part` and compares (`422 checksum_mismatch` → status `FAILED`, file deleted). For EPUBs the zip magic is checked as `/api/books/upload` does. Then the file is renamed into `<inbox>/<stem>-<hex>.<ext>` and the row becomes `COMPLETED` with `inbox_name`; the ordinary watcher ingests it. Response `200` with the status shape plus `queued` (the inbox name) and `title` when readable. Finalize is idempotent: on a `COMPLETED` session it returns the same `200`. |
| `DELETE /api/uploads/{id}` | Cancel: status `CANCELLED`, `.part` deleted. `204`. Idempotent. |

Rules:

- **Ownership and isolation.** Only the creating user (and the owner role for listing and
  cancelling) can see a session. Partial files live under `<LECTERN_DATA_DIR>/uploads/`, not in
  the inbox, so the watcher never sees a half-uploaded file.
- **Idempotency.** `POST /api/uploads` accepts an optional `Idempotency-Key` header
  (≤ 64 chars); a repeat with the same key and user within 24 h returns the existing session
  (`200`) instead of creating another. Chunk replay and finalize replay are defined above.
- **Expiry and cleanup.** A sweeper (on startup and hourly, whether or not inbox ingest is
  enabled) marks `ACTIVE` sessions past `expires_at` as `EXPIRED` and deletes their `.part`
  files, deletes `.part` files with no `ACTIVE` row that are older than 10 minutes, and deletes
  rows in a terminal state after 7 days. The session row is committed before its `.part` file
  is created. A chunk for an `EXPIRED`,
  `CANCELLED`, `FAILED`, or `COMPLETED` session answers `410 gone`.
- **Limits.** `size` and the running total are checked against `LECTERN_UPLOAD_MAX_BYTES`
  (which M2 raises to a value suitable for audiobooks, e.g. 4 GiB; the old 200 MiB default is
  for EPUBs). At most 5 `ACTIVE` sessions per user (`429 too_many_uploads`); the sum of their
  `size` is the user's in-flight quota. Chunk uploads share the normal per-IP limiter for
  writes. Disk is checked at create (`507` if the data volume has less free space than `size`).
- **Audit.** `upload.start`, `upload.complete` (with the inbox name), `upload.fail`, and
  `upload.cancel`; not one row per chunk.
- **Why offsets, not indexed parts.** One append-only `.part` file needs no reassembly step and
  no per-part bookkeeping; `received_bytes` *is* the state, which keeps the SQL constraints
  checkable (`ck_upload_received`, `ck_upload_completed`). Parallel chunks would need indexed
  parts; a home uplink gains nothing from them.

### Imports (review before filing) — `/api/imports`

HTTP-free service in `lectern.imports`; the routes are thin wrappers, so a later automation
client (for example an MCP server) calls the same functions with the same schemas. Every route
is owner/member only, session + CSRF, and an import is visible only to its uploader (`404`
otherwise). Machine-readable errors are `{"detail": "<code>", ...}`; validation errors are
`422 {"detail": "Invalid request", "errors": [{"loc": [...], "msg": "..."}]}` (never echoing
the input).

| Call | Purpose |
| --- | --- |
| `POST /api/imports {label}` (+`Idempotency-Key`) | Create an import (`uploading`). Same key returns the same import. |
| `POST /api/uploads {..., import_id}` | Resumable upload into the import: finalize stages the verified file in `<library>/_staging/<id>/` instead of the inbox and reports `file.status` `ok`/`duplicate`. |
| `POST /api/imports/{id}/complete` | All files are in: detect metadata and propose a target. With `auto_file_imports`, a certain match is filed in the same call. |
| `GET /api/imports[?status=open\|all\|ready\|filed]`, `GET /api/imports/{id}` | `ImportInfo`: files, `detected`, draft `metadata`, `proposal {action, work_id, confidence high\|medium\|low, score, reason, candidates[≤5], siblings}`, `target`, `use_cover`, `version`. |
| `PATCH /api/imports/{id} {version, metadata?, target?, use_cover?}` | Edit the draft (optimistic `version`). |
| `POST /api/imports/{id}/file {version?, metadata?, target?, use_cover?, audio: refuse\|append}` (+`Idempotency-Key`) | File it. `target {action: new\|join, work_id}`. Joining fills only empty fields; adding tracks to a work with audio needs `audio: "append"` (`409 target_has_audio`). Duplicates are skipped. Replays return the first answer. |
| `POST /api/imports/{id}/combine {other_id}`, `DELETE /api/imports/{id}/files/{file_id}`, `DELETE /api/imports/{id}` | Merge two imports, drop a file, discard. |

Lifecycle: `uploading → ready → filed | discarded`, or `failed` (`all_duplicates`, unreadable).
Unfiled imports expire after 7 days; orphaned staging folders are swept hourly.

### Editing — `/api/works/{id}/edit`, `PATCH /api/works/{id}`

`GET …/edit` returns `EditorState` (metadata, files, tracks, shelves the caller may change,
cover, alignment, read-aloud, folder, last revision, `version`). `PATCH` takes a `WorkPatch`:
`{version, metadata?, shelves?, primary_file_id?, playback_default?, readalong_enabled?,
tracks?: [{id, title?, chapters?}] (full order), remove_file_ids?, remove_track_ids?, cover?:
{action: upload|embedded|remove, token?}}`. Only sent parts change. A stale `version` is
`409 version_conflict` with `current` (the fresh `EditorState`). An `Idempotency-Key` replays
the first answer; reusing a key with a different body is `422 idempotency_key_reused`. Keys are
scoped per user and route and kept 24 h.

Each save runs under the library lock with a journal of file steps (folder move, track renames,
formats to trash, cover backups) that is rolled back if any step or the commit fails. It writes
a `WorkRevision` (before/after of every changed aspect) and the per-work `book.json`.
`POST …/undo {revision_id}` reverts it while the work is still at that revision's
`version_after` (`409 undo_conflict` otherwise). Cover uploads are staged first:
`POST /api/works/{id}/cover` (raw JPEG/PNG/WebP ≤ 10 MiB) → `{token, preview_url}`.

Folder moves never overwrite: same filesystem uses rename; across filesystems (`EXDEV`) the file
is copied to a hidden temp name, fsynced, size- and SHA-256-verified, renamed, then the source is
removed. Paths are checked against traversal. A move is deferred while a read-aloud build is
running for the work. Changing the primary EPUB, removing a format, or reordering tracks rebuilds
sentences and cancels or stales the alignment, which is re-queued with the same model.

Bulk: `POST /api/works/bulk {work_ids[≤500], action: set_authors|set_series|add_tags|
remove_tags|add_to_shelves|remove_from_shelves|trash, ...}` (+`Idempotency-Key`) → `{batch_id,
results: [{work_id, ok, error?}]}`; each work is its own transaction and revision.
`POST /api/works/bulk/{batch_id}/undo` reverts the batch.

### Trash — `/api/trash`

`POST /api/works/{id}/trash {version?}` moves the folder to `_trash/works/` and hides the work
everywhere (library, OPDS, KOReader, search, matching). Removed formats go to `_trash/files/`
with their DB rows kept as JSON. `GET /api/trash`; `POST /api/trash/works|files/{id}/restore`
(owner/member; restores to the original folder or a free one); `DELETE …` purges now (owner
only). Housekeeping purges after 30 days.

### Metadata suggestions — `POST /api/metadata/lookup`

Off until the owner sets `PUT /api/library-settings {metadata_lookup_enabled: true}` (instance
setting, audited). Body `{isbn?, title?, author?, providers?}`; with an ISBN only the ISBN is
sent. Fixed HTTPS origins (`openlibrary.org`, `www.googleapis.com`), redirects refused, 6 s
per request and 10 s total, 1 MiB body cap, JSON only, 24 h cache, per-user and per-provider
rate limits. Returns `{sent, suggestions: [{provider, provider_name, source_url, fetched_at,
fields}], errors}`; fields are validated against the same `Metadata` schema. Nothing is ever
written by a lookup.

### Audit actions added

`import.file`, `import.auto_file`, `import.combine`, `import.discard`, `work.edit`,
`work.bulk_edit`, `work.bulk`, `work.bulk_undo`, `work.undo`, `work.trash`, `work.restore`,
`work.purge`, `file.restore`, `file.purge`, `metadata.lookup`, `settings.change`,
`readaloud.regenerate`.

## Worker protocol

External alignment workers (a GPU box, a laptop) pull jobs from the server; the server never
connects to them, so they work behind NAT and TLS terminates at the usual reverse proxy.
Milestone 3 implements this; Step 0 fixes the schema and the rules.

```diagram
┌────────┐  claim (Bearer key)   ┌────────┐
│ worker │ ────────────────────▶ │ server │  UPDATE jobs SET status='LEASED', lease…
│        │ ◀──── job + lease ─── │        │  WHERE id=… AND status='QUEUED'  (1 row or retry)
│        │  GET inputs (key+lease)         │
│        │  heartbeat every ≤ lease/3      │
│        │  PUT result (sentence map) ───▶ │  validate → epub/audio hashes match →
│        │ ◀──── 200 / 400 / 409 ───────── │  write map, rebuild sentences, SUCCEEDED
└────────┘                       └────────┘
```

### Credential

- `POST /api/worker-credentials {label}` (owner only) → `201 {id, label, secret}`; the secret
  is `lw_` + `secrets.token_urlsafe(32)` (256 bits) and is shown once. Stored as
  `worker_credentials.key_hash = sha256(secret)`, the same scheme as sync keys and app
  passwords.
- `GET /api/worker-credentials` lists `{id, label, created_at, last_used_at, revoked_at}`;
  `DELETE /api/worker-credentials/{id}` revokes (`revoked_at`), effective on the next request.
  Jobs leased by a revoked credential are reclaimed as if the lease had expired.
- Scope: the key is sent as `Authorization: Bearer lw_…` and is accepted **only** by
  `/api/worker/*`. It is not a user: it has no session, no role, no CSRF token, and cannot read
  the library, progress, or any other route. Sessions and app passwords are in turn rejected by
  `/api/worker/*`. A leaked worker key can claim jobs and download the inputs of jobs it holds,
  nothing else.

### Implementation notes (milestone 3)

- A lease is bound to the credential that claimed it: the token alone is not enough, every
  later call must come with the same worker key (`409 lease_lost` otherwise).
- Rate limits: failed credential checks are limited per client address (the ordinary
  per-minute limit; once over it, the address gets `429` before any lookup). A valid key has
  its own bucket of `LECTERN_WORKER_REQUESTS_PER_MINUTE` (default 1200) requests, enough for
  ranged downloads of every track; `/fail` is exempt so a lease is never left to expire.
- Claim, heartbeat, and fail bodies are capped at 64 KiB (`413`). Heartbeat `stats` is a typed
  model (`lectern.align.jobs.AlignStats`): finite numbers in range, `speed` > 0, `diagnosis` one of
  the documented values; unknown keys are dropped and anything else is `422`.
- Results: floats must be finite, and the map's track list must equal the job's
  (durations and offsets within 0.05 s, else `422 input_mismatch`). The result is validated
  and applied in a worker thread, not on the event loop.
- A spine item that is missing, dangling, empty, over 8 MiB, or unparseable contributes no
  text (and keeps its index), as does everything past 64 MiB of spine per book; items are
  parsed one at a time.

### Claim — `POST /api/worker/jobs/claim`

Body `{"kinds": ["align"], "worker": "gpu-box", "lease_s": 600}` (`lease_s` clamped to
60–3600). Response `204` when nothing is runnable, otherwise `200`:

```json
{
  "job": {"id": 3, "kind": "align", "work_id": 12, "attempt": 1, "max_attempts": 3,
          "payload": {"epub_sha256": "…", "audio_fingerprint": "…", "model": "…",
                      "language": "en",
                      "tracks": [{"position": 0, "sha256": "…", "duration_s": 802.5,
                                  "size": 6420608, "media_type": "audio/mpeg"}]}},
  "lease": {"token": "<opaque, 256-bit>", "expires_at": "…", "lease_s": 600},
  "inputs": {"epub": "/api/worker/jobs/3/inputs/epub",
             "tracks": ["/api/worker/jobs/3/inputs/tracks/0"]},
  "result_url": "/api/worker/jobs/3/result"
}
```

The claim is one statement: `UPDATE jobs SET status='LEASED', attempts=attempts+1,
leased_by_id=…, lease_token_hash=…, lease_expires_at=now+lease_s, heartbeat_at=now,
started_at=COALESCE(started_at, now) WHERE id=(SELECT id FROM jobs WHERE status='QUEUED' AND
kind IN (…) AND run_after <= now ORDER BY priority, created_at LIMIT 1) AND status='QUEUED'`.
If it updates 0 rows another worker won; retry a few times, then `204`. Before selecting, the
server reclaims expired leases (below). `payload` is the input snapshot taken when the job was
enqueued; it is what the result must match.

The lease token (`secrets.token_urlsafe(32)`) is stored hashed in `lease_token_hash` and must
accompany every later call as `X-Lease-Token`. Any call whose token does not match the job's
current lease hash, or whose lease has expired, or whose job is no longer `leased`, is answered
`409 lease_lost`.

### Inputs — `GET /api/worker/jobs/{id}/inputs/epub`, `GET …/inputs/tracks/{position}`

Bearer key + `X-Lease-Token`. Serve the work's current EPUB / track file with `Range` support,
`ETag: "<sha256>"`, and `X-Content-SHA256`. Before serving, the server compares the current
file's hash with the job's `payload`; on mismatch it answers `409 lease_lost` (the job has been
cancelled by the change, see below). There are no pre-signed URLs: both secrets are required on
every request, so revocation and cancellation are immediate.

### Heartbeat — `POST /api/worker/jobs/{id}/heartbeat`

Body `{"progress": 0.4, "message": "aligning chapter 3"}` (both optional). Extends
`lease_expires_at` by the lease's `lease_s`, sets `heartbeat_at`, and stores `progress`/`message`
in `result` for `/api/jobs` to show. Workers should heartbeat at least every `lease_s / 3`.
Response `200 {"expires_at": "…"}` or `409 lease_lost`.

### Result — `PUT /api/worker/jobs/{id}/result`

`Content-Type: application/json`, body = a sentence map, at most 32 MiB. Checks, in order:

1. credential active, lease valid → else `401` / `409 lease_lost`;
2. body parses and passes `SentenceMap` validation → else `400 invalid_map` with
   `errors: [...]`;
3. `work_id` equals the job's; `epub_sha256` and `audio_fingerprint` equal the job `payload`
   → else `422 input_mismatch` (the worker aligned the wrong files; the job stays leased so it
   can retry or fail it);
4. `payload` still equals the work's current files → else `409 lease_lost` (stale result: the
   work changed under the job);
5. every sentence's `char_end` is within the normalised length of its spine item → else
   `400 invalid_map`.

Then, in one transaction: write `<data>/align/<work_id>.json` atomically, replace the work's
`sentences` rows, upsert `alignments` (`succeeded`, `model`, `coverage`, `sentence_count`,
`epub_sha256`, `audio_fingerprint`, `job_id`, `completed_at`), set the job to `succeeded` with
`result = {"sentence_count", "coverage"}` and `finished_at`. Response
`200 {"job_id": 3, "status": "succeeded", "sentence_count": 1830, "coverage": 0.98}`.

Idempotency: finished jobs keep their last `lease_token_hash` (constraint `ck_job_lease` only
requires a token while leased). A repeated `PUT` for a job that is already `succeeded` with the
same `X-Lease-Token` returns the same `200` body without re-applying anything; with any other
token it is `409 lease_lost`.

### Failure — `POST /api/worker/jobs/{id}/fail`

Body `{"error": "CUDA out of memory", "retryable": true}`. If `retryable` and
`attempts < max_attempts`: status `queued`, lease cleared, `run_after = now + backoff(attempts)`
with `backoff(a) = min(30 s × 2^(a−1), 1 h)` ± 20 % jitter, `error` kept for display. Otherwise
terminal: status `failed`, `finished_at` set, and the work's `alignments` row becomes `failed`
with the error (a previous successful alignment's `sentences` stay in place until a new success
replaces them). Response `200 {"status": "queued", "run_after": "…"}` or
`{"status": "failed"}`.

### Lease expiry and reclaim

On every claim and from a 30-second background sweep: each job with `status = 'leased'` and
`lease_expires_at < now` is reclaimed with the same rule as a retryable failure (`error =
"lease expired"`), so it goes back to `queued` with backoff or becomes `failed` when attempts
are exhausted. Reclaiming clears `lease_token_hash`, so a late upload from the dead worker is
`409 lease_lost`. A worker whose heartbeat gets `409` must abandon the job.

### Staleness and cancellation

When a work's EPUB or audio changes (re-ingest, track added or removed), the server:

1. cancels any active `align` job for the work (`cancelled`, lease cleared) so the partial
   unique index frees up, then enqueues a fresh job with a new payload snapshot;
2. marks an existing `alignments` row `stale` (its `sentences` remain usable for reading until
   replaced);
3. lets a worker still holding the old lease discover this through `409 lease_lost` on its next
   heartbeat, input, or result call.

`failed` and `cancelled` jobs are never retried automatically; `POST /api/works/{id}/align`
creates a new one. History is kept: finished jobs are not deleted with their leases.

## Client addresses behind a proxy (implemented)

Lectern is meant to sit behind a TLS-terminating reverse proxy, in the documented deployment
`tailscale serve` (see [`deploy-tailscale.md`](deploy-tailscale.md)), so the socket peer is
often the proxy, not the reader. `lectern.security.proxy.resolve_client_ip` decides what
counts as the client address, and **every** consumer uses it through
`lectern.security.sessions.client_ip`: the per-IP rate limiters (login, setup, WebAuthn, OPDS
Basic auth, KOReader sync), the `ip` stored on sessions and audit rows, and anything
milestone 2+ adds (invites, uploads, streaming). Account lockout is per username and does not
use the address. The address never affects *authentication*; a wrong address means a wrong
rate-limit bucket or audit row, nothing more. Lectern's own sign-in stays mandatory however
private the network in front of it is (a tailnet is a trusted network, not an identity).

Rules:

1. `LECTERN_TRUSTED_PROXIES` is a comma-separated list of IPs or CIDRs (default empty). A
   peer is *trusted* only if `request.client.host` parses as an IP inside one of them. Unix
   sockets and non-IP peers are never trusted. For `tailscale serve` on the same host, or in a
   sidecar container whose network namespace Lectern shares, the value is `127.0.0.1`
   (serve dials its backend from loopback).
2. For an **untrusted** peer the client address is the peer. `X-Forwarded-For` and
   `CF-Connecting-IP` are ignored completely, so a client that reaches Lectern directly cannot
   choose which bucket it is throttled in or what the audit log records.
3. For a **trusted** peer, `X-Forwarded-For` is read **right to left**. The rightmost hop was
   appended by the proxy that connected to us and is the only one it vouches for; hops that are
   themselves inside `LECTERN_TRUSTED_PROXIES` are skipped, and the first untrusted address is
   the client. The leftmost entry is whatever the client sent and is never used on its own.
   `host:port` and `[v6]:port` forms are accepted. A malformed hop (`unknown`, junk, empty)
   ends the walk, because nothing to its left can be verified, and the peer is used; an
   all-trusted chain or an absent header also yields the peer. `tailscale serve` *replaces*
   the header with the caller's tailnet address, so in the documented deployment the header
   has exactly one hop.
4. `CF-Connecting-IP` is **ignored by default, even behind a trusted peer**. Every proxy
   other than cloudflared (`tailscale serve` included) passes it through from the client
   untouched, so believing it would hand the client its own address back. The boolean
   `LECTERN_TRUST_CF_CONNECTING_IP` (default `false`) enables it for the one-line Cloudflare
   Tunnel alternative in the deployment guide: a Cloudflare deployment must set it explicitly
   *and* limit `LECTERN_TRUSTED_PROXIES` to the `cloudflared` connector's address, nothing
   else. With the flag on, a valid `CF-Connecting-IP` from a trusted peer takes precedence
   over `X-Forwarded-For` (cloudflared sets it to the visitor and strips any copy the visitor
   sent); an invalid value falls through to rule 3, and an untrusted peer's header is still
   ignored.
5. uvicorn's own `--proxy-headers` handling is disabled (`lectern.cli`), so the rule above is
   the only place forwarding headers are interpreted.

Tests: `tests/test_proxy.py` (spoofed leftmost entries, all-trusted chains, malformed hops,
IPv4/IPv6 and port forms, trusted and untrusted peers, `CF-Connecting-IP` ignored by default
and honoured only with the flag on, and the rate-limit buckets and audit rows through the app).

## Notifications, alerts, and download caps (milestone 2)

Tables `notifications`, `known_devices`, `download_events` and the cap columns
`users.download_cap_per_hour` and `app_passwords.download_cap_per_hour` are in migration `0006`.

- **Alerts.** A sign-in from a browser without a recognised `lectern_device` cookie notifies
  the user and every owner (`device.new`, audit `device.new`); a known browser gets an info
  note. The first use of a sync key or app password notifies the user (`device.connected`,
  audit `device.first_use`). `download_burst_count` downloads within
  `download_burst_window_seconds` notify the user and owners once per hour (`download.burst`).
- **Caps.** Whole-file transfers (`/library/files/{id}/download`, OPDS downloads,
  `/library/works/{id}/book.epub`, `/library/tracks/{id}/download`,
  `/library/works/{id}/read-aloud.epub`) are counted per user and, for OPDS app passwords,
  per device over a rolling hour. Over a cap: `429 {"detail": "download_cap", "message",
  "scope": "user"|"device", "limit", "retry_after"}` with `Retry-After`, an audit
  `download.blocked`, and a `download.cap` notification. Streams count once per credential and
  work per hour after `stream_meter_bytes` were sent (see Streaming); read-along chapter text
  (`GET /api/works/{id}/text/{n}`) counts once per session and work per hour on the first
  chapter (`target text:<work_id>`). Counting and recording are atomic, so concurrent requests
  cannot overshoot a cap. Overrides: `POST /api/admin/users/{id}/download-cap {cap}` (owner) and
  `POST /api/devices/app-passwords/{id}/download-cap {cap}` (that user); `null` means the
  server default, `0` no cap. Members and readers may only lower their own device's effective
  cap (`403 only_owner_can_raise_cap` otherwise); the owner role may set any value. `GET /api/security` reports `downloads` usage and
  `known_devices`; `POST /api/security/known-devices/{id}/forget` forgets one.
- **Reading them.** `GET /api/notifications?limit=` → `{items: [{id, kind, severity, title,
  body, data, created_at, read_at}], unread}`; `POST /api/notifications/read {ids?, all?}`.
  `/api/me` includes `counts.unread_notifications`.
- **Extension point for e-mail or webhooks.** Lectern ships in-app delivery only, because
  external channels need configuration a household server does not have by default. Implement
  `lectern.notify.Sink` (`deliver(db, note, user)`, must return quickly and not commit) and call
  `notify.register_sink(...)` at startup, gated by your own setting; every notification reaches
  it after the row is written. Failures are logged, never raised.

## `lectern.positions` interface

```python
TextPos(spine_index: int, char: int)        # text timeline
SentencePos(n: int, offset: int = 0)        # canonical, what work_progress stores

normalize_spine_text(xhtml: bytes) -> str    # frozen normalisation rules (above)

class SentenceIndex:                         # built from Sentence rows or SentenceEntry items
    at_text(pos: TextPos) -> SentencePos | None     # between sentences → next one at 0
    to_text(pos: SentencePos) -> TextPos
    at_time(t: float) -> SentencePos | None         # linear interpolation inside a sentence
    to_time(pos: SentencePos) -> float | None       # unmatched → end of previous matched

xpointer_spine_index(xpointer: str) -> int | None   # DocFragment[k] → k - 1
xpointer_to_text(xpointer: str, spine_item_xhtml: bytes) -> TextPos   # M3, via lectern.textmap
text_to_xpointer(pos: TextPos, spine_item_xhtml: bytes) -> str        # M3, via lectern.textmap
```

All of it is exercised by `tests/test_contracts.py` against the fixture map. The xpointer
functions (milestone 3) follow crengine's path conventions and round-trip every character of
the fixture story and of generated XHTML (`tests/test_align.py`); unresolvable paths clamp to
the nearest position instead of failing.

## Fixture

`tests/fixtures/magi/` pairs Project Gutenberg #7256 (*The Gift of the Magi*) with a 77-second
excerpt of the public-domain LibriVox narration and a hand-checked 15-sentence map. Provenance,
licences, URLs, hashes, and how to fetch the full recording are in
`tests/fixtures/magi/PROVENANCE.md`. It is a test fixture only; Lectern has no feature that
searches for or downloads books or audio.
