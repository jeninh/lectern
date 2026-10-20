# Lectern contracts (Step 0)

The shared schema and interfaces that milestones 2 (audio + shelves + invites + resumable
uploads), 3 (alignment workers) and 4 (positions and read-aloud) build on. Step 0 ships the
database tables (migration `0005`), the sentence-map schema (`lectern.sentence_map`), the
position interface (`lectern.positions`), the visibility predicates (`lectern.access`), the
trusted-proxy client-address rule (`lectern.security.proxy`, **implemented**), and this
document. **The other HTTP routes below are not implemented yet**; they are the contract the
milestones implement.

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
- Visibility is checked through the track's work (`404` otherwise). Streaming is **not**
  written to the audit log per request (a player issues many); starting playback of a work is
  recorded once per session in milestone 2's player.

#### Optional low-bitrate variant (milestone 2 may implement)

Away from home, every byte still crosses the home uplink (over Tailscale or any other route), so
a 320 kb/s MP3 audiobook is slow to seek on a phone. Milestone 2 *may* add a
`variant=low` query parameter that streams a once-generated ~64 kb/s Opus (fallback AAC)
rendition. The contract leaves room for it without requiring it:

- Originals are never modified, re-encoded in place, or deleted; `GET /library/tracks/{id}/stream`
  without `variant` always serves the original bytes, and the plain download continues to
  work.
- The derivative lives in the work directory at
  `derived/audio/<original sha256>.<opus|m4a>`, keyed by the original's hash so a replaced
  track invalidates it. It is produced by a `transcode` job (same queue, `kind = "transcode"`,
  payload `{"track_id", "sha256", "codec", "bitrate_kbps"}`), on the server with `ffmpeg`
  when available or by an external worker. No schema is needed: existence of the file *is*
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

Sentence text is not returned; clients take it from the EPUB with the same normalisation (the
rules are deliberately simple enough to reimplement in the web reader). `count` is `0` and
`sentences` is `[]` for a work without a sentence map.

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

### Read-aloud EPUB — `GET /library/works/{work_id}/read-aloud.epub`

The work's EPUB with EPUB 3 Media Overlays added: every sentence wrapped in
`<span id="s{n}">`, one SMIL `<par>` per matched sentence pointing at the embedded audio
tracks, so Thorium, Apple Books, and KOReader can play it with highlighting. Decisions:

- The audio is embedded, so the file is roughly EPUB + audio size; it is generated on first
  request and cached **next to the work**, at
  `<LECTERN_LIBRARY_DIR>/<work.dir_path>/derived/read-aloud.epub` (i.e.
  `/library/<Author>/<Title>/derived/`), keyed by `(epub_sha256, audio_fingerprint)` in a
  sidecar `derived/read-aloud.json` and regenerated when either changes. Only sentence maps
  live under `<LECTERN_DATA_DIR>/align/`; everything derived from a work's own files lives in
  its `derived/` directory so it travels (and is deleted) with the work. Scanners never ingest
  from `derived/`.
- `409 no_alignment` unless `alignments.status` is `succeeded`. Generation is synchronous
  (seconds for a typical work); if it turns out to be slow, milestone 4 may move it to a
  `readaloud` job kind and answer `202` with a job id.
- Audited as a download, same as the plain EPUB. Auth and visibility as for downloads.
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

### Shelves — `/api/shelves`

Visibility rule, implemented by `lectern.access`:

> Owners and members see the whole library. Readers see only works on shelves they hold a
> grant for.

`visible_works(user)` is a SQL predicate that milestone 2 adds to `/api/library`,
`/api/works/{id}`, `/api/continue`, covers, downloads, OPDS, and all routes in this document.
A role change takes effect on the next request.

| Route | Who | Behaviour |
| --- | --- | --- |
| `GET /api/shelves` | any | Owner: all shelves. Member: shelves they own plus shelves granted to them. Reader: granted shelves only. Each with `work_count`, `permission` (`owner`, `edit`, `view`) |
| `POST /api/shelves {name}` | owner, member | `201`; `409` on a duplicate name for that owner |
| `PATCH /api/shelves/{id} {name}` / `DELETE` | shelf owner or the owner role | `DELETE` cascades items and grants, never works |
| `GET /api/shelves/{id}` | anyone who can see it | shelf plus its works in `position` order (readers never see a shelf they are not granted) |
| `PUT /api/shelves/{id}/items {work_ids: [...]}` | manager or `edit` grant | Replaces the whole ordered list; members may add any work, a reader with `edit` only works they can already see |
| `POST /api/shelves/{id}/items {work_id}` / `DELETE …/items/{work_id}` | manager or `edit` grant | Append / remove one |
| `GET /api/shelves/{id}/grants` | manager | `[{user, permission, granted_by, created_at}]` |
| `PUT /api/shelves/{id}/grants/{user_id} {permission}` / `DELETE` | manager | Upsert / revoke. Granting to an owner or member is allowed but has no effect on visibility (it still lists the shelf for them) |

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
  (not distinguished). The route shares the per-IP login rate limit. Passkeys/TOTP are set up
  afterwards from the normal security settings.

### Resumable uploads — `/api/uploads`

`POST /api/books/upload` (one request, whole file) stays for small EPUBs. A home uplink drops
long transfers, and some proxies cap request bodies (Cloudflare: 100 MB; Tailscale has no such
cap, but the 50 MiB chunk limit keeps one contract for every deployment), so milestone 2 adds a
resumable session upload for EPUBs and audiobooks. The schema
(`upload_sessions`, migration `0005`) is in place; the routes are M2's. **The inbox folder
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
| `PUT /api/uploads/{id}/chunk` | Headers `Upload-Offset: <int>`, `X-Chunk-SHA256: <hex>`, raw body ≤ `chunk_size` bytes (a larger body is `413` before anything is written). Every chunk except the last must be exactly `chunk_size` long (`400 short_chunk`). `Upload-Offset` must equal `received_bytes` (`409 offset_mismatch` with the current `Upload-Offset` header; the client reads it and resumes). A replay with `Upload-Offset + len == received_bytes` and the same `X-Chunk-SHA256` as the recorded last chunk answers `204` without writing (idempotent retry after a lost response); any other overlap is `409`. The body is hashed while streaming to the `.part` file; a mismatch truncates the file back to the previous offset and answers `422 chunk_checksum`. Success: `204` with `Upload-Offset: <new received_bytes>`. |
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
- **Expiry and cleanup.** A sweeper (on startup and hourly) marks `ACTIVE` sessions past
  `expires_at` as `EXPIRED` and deletes their `.part` files, deletes `.part` files with no
  `ACTIVE` row, and deletes rows in a terminal state after 7 days. A chunk for an `EXPIRED`,
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
xpointer_to_text(xpointer: str, spine_item_xhtml: bytes) -> TextPos   # M4: NotImplementedError
text_to_xpointer(pos: TextPos, spine_item_xhtml: bytes) -> str        # M4: NotImplementedError
```

The implemented parts are exercised by `tests/test_contracts.py` against the fixture map; the
two xpointer functions raise `NotImplementedError` until milestone 4 and that is tested too, so
the first real implementation has to update the test on purpose.

## Fixture

`tests/fixtures/magi/` pairs Project Gutenberg #7256 (*The Gift of the Magi*) with a 77-second
excerpt of the public-domain LibriVox narration and a hand-checked 15-sentence map. Provenance,
licences, URLs, hashes, and how to fetch the full recording are in
`tests/fixtures/magi/PROVENANCE.md`. It is a test fixture only; Lectern has no feature that
searches for or downloads books or audio.
