# Generated audiobooks (text-to-speech)

A book that has an EPUB or a text PDF ([pdf-read-along.md](pdf-read-along.md)) but no
audiobook gets **Make an audiobook…** in its Formats list
(owners and members). Choose an engine, a voice, a speed, and which chapters; play a sample;
then Lectern narrates the text into an ordinary audiobook edition of the work: one track per
chapter under `audio/`, titled after the chapter, playable, downloadable, and streamable like
any imported audiobook. **Read-along works immediately**: every sentence's start and end come
from the synthesis itself, so no alignment run is queued and none is needed.

For a PDF-only book the script is the PDF's extracted sentences (running heads, page numbers
and hyphenation already removed), and its chapters are the PDF's sections: outline entries,
or 10-page ranges when it has no outline; the sheet shows each one's pages. A scanned PDF is
refused with `pdf_needs_ocr` until it has text (issue #17), one still being read with
`pdf_text_pending`, and a comic with `needs_epub`.

```diagram
┌──────────┐ sentences  ┌──────────────┐ one sentence ┌──────────┐ PCM + sample ┌───────────┐
│ EPUB     │──────────▶ │ script       │ ───────────▶ │ engine   │ ───────────▶ │ chapter   │
│ (spine)  │ (same as   │ chapters of  │  at a time   │ Piper    │   counts     │ .m4a +    │
└──────────┘ alignment) │ numbered     │  (ElevenLabs:│ Kokoro   │              │ timings   │
                        │ sentences    │  paragraphs) │ 11Labs   │              └─────┬─────┘
                        └──────────────┘              └──────────┘                    │ result
                                                                ┌───────────────────▼──────┐
                                                                │ edition: audio_tracks,    │
                                                                │ sentence map, alignment   │
                                                                │ "succeeded" (synthesis)   │
                                                                └──────────────────────────┘
```

## Engines

| Engine | Where it runs | Good for | Text leaves your machines? |
| --- | --- | --- | --- |
| **Piper** | The server's CPU (ONNX), a Raspberry Pi included | Always available once installed; plain but clear voices | No |
| **Kokoro** (preferred) | A worker that advertises `tts-kokoro` (the Mac worker, [`mac-worker.md`](mac-worker.md)), or the server with `LECTERN_TTS_KOKORO_LOCAL=1` | Natural voices; wants Apple Silicon, a GPU, or a fast CPU | No |
| **ElevenLabs** (optional) | ElevenLabs' service, with **your** API key, called by the server only | Studio voices; costs credits | **Yes**: the chosen chapters' text is sent to `api.elevenlabs.io` |

The sheet lists all three, says where each would run, and recommends Kokoro when a capable
worker (or local install) is there, else Piper. A Kokoro narration can be queued with no
worker online; it waits, and starts when one connects.

### Installing Piper (Raspberry Pi and other small servers)

1. Build the image with the engine: `LECTERN_EXTRAS=tts` (`docker compose build --build-arg
   LECTERN_EXTRAS=tts`, or set it in `.env` for `deploy/tailscale/`); plain installs use
   `pip install 'lectern[tts]'`. `LECTERN_EXTRAS` is a space- or comma-separated list:
   `LECTERN_EXTRAS="align tts"` adds alignment too, so a Pi 4 or 5 with at least 4 GB can align
   and narrate by itself while the Mac worker is off
   ([alignment.md](alignment.md#where-to-run-it-raspberry-pi-nas-laptop)).
2. Put one or more voices (`<voice>.onnx` and `<voice>.onnx.json`) in `<LECTERN_DATA_DIR>/tts/piper/`
   (or `LECTERN_PIPER_VOICES_DIR`). With the image:

   ```sh
   docker compose exec lectern python -m piper.download_voices \
       --download-dir /data/tts/piper en_US-lessac-medium
   ```

   or `docker compose exec lectern lectern prepare-models --piper-voice en_US-lessac-medium`
   (also what Admin → Offline mode → Download models now fetches when no voice is installed;
   see [offline-mode.md](offline-mode.md)).
   Voices are listed at <https://huggingface.co/rhasspy/piper-voices>; check each voice's
   licence before you share what you make. `low` voices are fastest, `medium` sound better.

Measured in an Amp orb (2 × x86 vCPU, Python 3.12, piper-tts 1.8.0, `en_US-lessac-low`,
default threads): *The Gift of the Magi* EPUB, 2 chapters, 30,140 characters → **31 min 54 s of
audio in 2 min 34 s** (≈ 12× realtime, ≈ 195 characters/s), peak ≈ 170 MB for the voice. One
run; no Raspberry Pi was measured, and ARM will be slower.

### Kokoro

A Kokoro worker (the Mac worker) claims `tts-kokoro` jobs (it downloads the
script, uploads each chapter as it finishes, then the result; see
[contracts](#wire-contract-the-tts-kind)). To run Kokoro in the server instead, install
the `kokoro` extra into the server's environment (`pip install 'lectern[kokoro]'`, which
includes spaCy's pinned `en_core_web_sm`: Kokoro's English text analysis needs it and would
otherwise try to `pip install` it in the middle of a narration) and set
`LECTERN_TTS_KOKORO_LOCAL=1`. An engine that exits the process fails the narration with the
reason instead of stopping the runner; two such crashes fail it for good (see
[mac-worker.md](mac-worker.md#when-a-job-crashes-the-worker)). While a Kokoro
worker is online and not paused, the server's own runner leaves new Kokoro jobs to it for
`LECTERN_LOCAL_DEFER_SECONDS` (default 600) before running them itself.

Voices: the Kokoro v1.0 set (`af_heart`, `bm_george`, …); the first letter is the language
(`a` American English, `b` British English, `e` Spanish, `f` French, `h` Hindi, `i` Italian,
`j` Japanese, `p` Brazilian Portuguese, `z` Mandarin).

### Voice samples

Lectern ships one built-in clip per Kokoro voice in
`src/lectern/static/voice-samples/kokoro/`: the same sentence for every voice of a language
("Once upon a time, in a quiet town by the sea, there lived a curious reader." in English,
its translation otherwise), at speed 1.0, 4.5 to 7.5 seconds each, AAC in M4A at 40 kbps
(it plays on iOS; Opus does not on older Safari). The 26 clips total about 750 KB;
`manifest.json` lists each clip's sentence, length, and size. When Kokoro is not installed on
this server, **Play a sample** plays the chosen voice's clip straight from `/static`, whether or
not a worker is online, and says it is a standard sample, not the book's text or the chosen
speed. When Kokoro runs on this server, the sample is made here from the book's text, as for
Piper.

Regenerate the clips when `KOKORO_VOICES` changes (needs ffmpeg, and espeak-ng for Spanish,
French, Hindi, Italian, and Portuguese; downloads the weights from Hugging Face on first use):

```sh
uv run --with "kokoro>=0.9" --with "misaki[en,ja,zh]" python scripts/kokoro_samples.py
```

Licence: Kokoro-82M's weights and voices are Apache-2.0
([hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)), which allows
redistributing audio made with them; the attribution is in the clips' `NOTICE` file.

## ElevenLabs (bring your own key)

**Admin → ElevenLabs** (owner only):

- **Save and test.** The key is checked against `GET /v1/user/subscription` and, if
  ElevenLabs accepts it, encrypted and stored. It is **never shown again**: the API and the
  page report only `configured` and the last four characters. Test it again at any time.
- **Encryption at rest.** Fernet (AES-128-CBC + HMAC-SHA256) under a key derived with HKDF-SHA256
  from the server's secret key (`LECTERN_SECRET_KEY`, else `<data>/secret_key`). A copy of
  `lectern.db` alone does not reveal it. If the secret key changes, the stored key cannot be
  decrypted any more; the page says so and asks for it again.
- **Remove key** deletes the ciphertext at once. The key is read again before every request
  (retries included), so a running or queued ElevenLabs narration sends nothing more and fails.
  A request already in flight cannot be recalled.
- **Audit.** `integration.elevenlabs.set`, `.test`, `.revoke`, `.preview`, `.use` (credits per
  narration), and `tts.request` / `tts.retry` (engine, chapters, characters, estimate, cap).
  The page shows the last 30 days' characters and credits (`integration_usage`).
- **Owner only.** Members can use Piper and Kokoro, not the owner's paid account.

### What is sent, and when

Every request goes to the fixed origin **`https://api.elevenlabs.io`** (a constant, not a
setting; redirects are refused). Nothing is sent at startup or in the background. Requests
happen only when the owner: saves or tests the key; opens the voice list or model list (the
sheet does this when ElevenLabs is chosen); plays a sample (≤ 300 characters, billed);
estimates (models and quota, no text); or confirms a narration. A narration sends the text of
the chosen chapters, in paragraph-sized pieces with the neighbouring piece as context, plus
the voice settings.

### Estimate, quota, confirmation, cap

Choosing ElevenLabs shows the disclosure above, account voices, the model, stability and
similarity, and speed (0.7–1.2). **Estimate cost** returns the characters, the model's credits
per character (from `/v1/models`; Flash/Turbo bill half), the credits it will use, the plan's
remaining quota and reset date, and, if you set `LECTERN_ELEVENLABS_PRICE_PER_1000_CREDITS`,
a money estimate. Then you must:

1. set a **spending cap** for this job (defaults to the estimate + 10 %), and
2. tick **"Send the text of these N chapters to ElevenLabs and spend up to C credits"**.

The server refuses the request (`409 confirmation_required`) without the tick, with
`409 estimate_changed` if the characters or price differ from what you saw, `400
cap_below_estimate`, or `409 quota_insufficient`. While it runs, each request is checked
against the cap **before** it is sent and recorded as reserved (`pending`) in the usage ledger
in the same step; the job stops (failed, not retried) rather than pass it. The cap is shared by
the job and its retries, and counts reserved and uncertain requests as spent.

### Chunks, retries, resume

Sentences are grouped into requests of at most 2,000 characters, ending at a paragraph once
half full; a sentence is never split. `POST /v1/text-to-speech/{voice}/with-timestamps`
(`pcm_24000`) returns the audio and a start and end time for every character; a sentence's
time is its first character's start to its last character's end, shifted by the piece's
place in the chapter. A `429` (rate limited: nothing was generated) is retried up to 5 times
with exponential backoff (1 s doubling to 60 s, ± 20 %), honouring `Retry-After`; a rejected
key, exhausted quota, or other `4xx` is not, and its reservation is released. A request whose
outcome is unknown, because the response was lost (network error), ElevenLabs answered `5xx`,
the answer was unusable, or the server stopped mid-request, may have been billed: it is
marked `uncertain`, stays counted against the cap, and is **never re-sent automatically**,
not by the client and not by an automatic re-run of the same job. The job fails with a
message saying so; **Retry** (a new job) is the owner's decision to send it again. Each paid
piece is cached under `<data>/tts/cache/elevenlabs/` (keyed by model, voice, settings, text,
and context; kept 30 days) before its request is marked `billed`, so a retry, a restart, or a
resumed job never pays again for a piece whose audio arrived.

## Jobs, progress, cancel, retry

Narrations are `tts` jobs in the same durable queue as alignment (`docs/contracts.md`): one
active job per work, an atomic claim with a hashed lease token, heartbeats, 3 attempts with
`min(30 s × 2^(a−1), 1 h) ± 20 %` backoff, leases reclaimed when they expire or when the server
restarts mid-job. Each has a capability, `tts-<engine>`; `tts-elevenlabs` is never leased to an
external worker, so the key never leaves the server.

- **Progress** is sentences narrated / all sentences; the Formats row shows it with the ETA
  and, for ElevenLabs, credits used of the cap. `GET /api/jobs?kind=tts` lists them.
- **ETA** is measured, never guessed: characters left divided by the median characters per
  second of the last five finished narrations with the same engine; before any, extrapolated
  from this run once it is 5 % done.
- **Cancel**: `POST /api/jobs/{id}/cancel` (the row's Cancel). The runner stops at its next
  sentence.
- **Retry**: a failed or cancelled narration's row offers **Retry** (`POST
  /api/works/{id}/audiobook/retry`), which queues it again with the same settings and cap.
  Chapters the earlier job finished are carried over and not narrated (or paid for) again.
  Artifacts of finished jobs are kept 7 days for that.
- A narration is refused (`409 has_audio`) for a work that already has audio, and is
  abandoned if an audiobook or a new EPUB arrives while it runs.

## Settings

| Setting | Default | |
| --- | --- | --- |
| `LECTERN_PIPER_VOICES_DIR` | `<data>/tts/piper` | Piper voices |
| `LECTERN_TTS_KOKORO_LOCAL` | `0` | Run Kokoro in the server (needs `kokoro` installed) |
| `LECTERN_TTS_LOCAL_RUNNER` | `1` | The server's own narration runner (Piper, ElevenLabs, local Kokoro) |
| `LECTERN_LOCAL_DEFER_SECONDS` | `600` | How long the server leaves a job to an online capable worker |
| `LECTERN_TTS_AUDIO_CODEC` | `aac` | `aac` (`.m4a`, plays everywhere) or `flac` |
| `LECTERN_TTS_AUDIO_BITRATE_KBPS` | `64` | AAC bitrate |
| `LECTERN_ELEVENLABS_PRICE_PER_1000_CREDITS` | `0` | Your price, for a money estimate (0 = credits only) |

## API

| Route | Who | |
| --- | --- | --- |
| `GET /api/works/{id}/audiobook` | owner, member | `{eligible, reason, chapters[{spine_index, title, sentences, characters, preview}], engines[…], recommended, job, edition, disclosure}`. Kokoro voices carry `sample: {url, text, duration_s}`, the built-in clip ([voice samples](#voice-samples)). No outbound request. |
| `POST /api/works/{id}/audiobook/estimate` | owner, member (ElevenLabs: owner) | `{chapters, sentences, characters, audio_s, eta, elevenlabs: {model, credits_per_character, credits, quota, fits, suggested_cap, cost}}` |
| `POST /api/works/{id}/audiobook` | owner, member (ElevenLabs: owner) | Body `{engine, voice, speed, model?, voice_settings?, chapters?: [spine_index], codec?, confirm?: {characters, credits, cap_credits, send_text}}` → `202` job |
| `POST /api/works/{id}/audiobook/retry` | owner, member | `202` job |
| `POST /api/works/{id}/audiobook/preview` | owner, member | `{engine: piper\|kokoro, voice, speed}` → WAV, made on this server |
| `GET\|PUT\|DELETE /api/integrations/elevenlabs`, `POST …/test` | owner | Status (never the key), save + test, revoke |
| `GET /api/integrations/elevenlabs/voices`, `GET …/models`, `POST …/preview` | owner | Account voices, TTS models with credit rates, a billed sample (WAV) |

## Wire contract: the `tts` kind

Agreed with the Mac worker (it implements the Kokoro client; this server implements the rest);
the same text is in [`contracts.md`](contracts.md).

- kind `tts`, capability `tts-<engine>`.
- payload: `{"engine", "model", "voice", "speed", "lang", "audio": {"codec": "aac"|"flac",
  "bitrate_kbps", "sample_rate"}, "script_sha256", "chapters", "sentences"}`, plus server
  bookkeeping a worker ignores (`characters`, `epub_sha256`, `spine_indices`, and for ElevenLabs
  `voice_settings`, `credits`, `cap_credits`, `cap_group`).
- input `script` (JSON, SHA-256 = `script_sha256`): `{"version": 1, "chapters": [{"index",
  "title", "sentences": [{"n", "text", "pause_s"}]}]}`; `n` is the global sentence number of the
  work's sentence list (the same segmentation as alignment), `pause_s` the silence after it
  (0.3 s in a paragraph, 0.7 s after one, 1.0 s after a heading line).
- artifacts, as each chapter finishes: `chapter-NNNN.m4a` (or `.flac`) and `chapter-NNNN.json`
  (that chapter's result entry). A retried attempt finds them and skips finished chapters.
- result: `{"version": 1, "engine", "model", "voice", "speed", "sample_rate", "codec",
  "chapters": [{"index", "title", "artifact", "sha256", "duration_s", "sentences": [{"n",
  "start_s", "end_s"}]}]}`, times relative to the chapter file. The server checks: same engine,
  voice, and codec as the job; the chapters and sentence numbers exactly the script's, in order;
  starts non-decreasing and ends within the chapter's duration (+ 0.05 s). Each artifact is
  copied into `<library>/.lectern-staging/` and hashed from that same read (it must match the
  declared SHA-256), then probed: it must be audio whose measured length is the declared
  `duration_s` (within 0.5 s or 1 %); those staged bytes are what gets published. Then, under
  the library lock, holding the exact lease that submitted the result (`queue.hold_lease`; a
  lease released, reclaimed, revoked, or cancelled meanwhile publishes nothing), and in one
  transaction, it copies the files into `audio/`, makes them tracks, writes the sentence map
  (`model = "tts-<engine>-<voice>"`; sentences of chapters not narrated stay unmatched, with no
  time), and marks the alignment `succeeded`. `422 invalid_result` / `input_mismatch` /
  `missing_artifact` / `bad_audio` otherwise; `409 lease_lost` if the work or the lease changed.
  A job's artifacts are kept for 7 days after it finishes, so **Retry** resumes from them.
- heartbeat `progress` = sentences done / total; `stats`
  `{engine, voice, sentences_done, characters_done, audio_s, elapsed_s, chars_per_second,
  credits, chapters_resumed}`, unknown keys dropped.

## Honest limits

- Chapters are spine items. A book that puts several chapters in one file gets one track for
  them; a TOC entry names a track only when it points at the start of the file, otherwise the
  chapter's first short line (or "Chapter N") does.
- Everything in the chosen chapters is read, including front matter or a licence that shares
  a file with the story (Project Gutenberg does this).
- Pronunciation of names and numbers is whatever the engine does.
- Kokoro on the Raspberry Pi was not measured and is not recommended; use Piper there and Kokoro
  on a worker.
- The ElevenLabs credit estimate counts the characters sent; ElevenLabs' own rounding,
  plan discounts, or price changes can make the bill differ slightly. The cap is enforced on
  Lectern's count.
