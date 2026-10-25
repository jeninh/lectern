# Alignment and cross-format sync

Lectern lines up a work's EPUB with its audiobook, sentence by sentence, so one reading position
can move between KOReader, the web reader, and the audio player. This page explains how
alignment works, where to run it, what the numbers mean, and how positions are converted. The
wire formats are in [`contracts.md`](contracts.md); this page is the implementation.

```diagram
┌──────────┐   spine walk (lxml)   ┌────────────────────┐  segment   ┌───────────────┐
│ EPUB     │ ────────────────────▶ │ normalised text +  │ ─────────▶ │ sentences 0…n │
└──────────┘                       │ per-char source map│            └───────┬───────┘
                                   └────────────────────┘                    │ tokens
┌──────────┐  faster-whisper       ┌────────────────────┐   tokens   ┌───────▼───────┐
│ tracks   │ ────────────────────▶ │ timed words on one │ ─────────▶ │    matcher    │
│ 0, 1, …  │  word times + VAD     │ work timeline      │            └───────┬───────┘
└──────────┘                       └────────────────────┘                    │
                                                                    ┌───────▼───────┐
                                                                    │ sentence map  │
                                                                    │ + statistics  │
                                                                    └───────────────┘
```

## Turning it on

A generated audiobook ([`tts.md`](tts.md)) never needs this: its sentence times come from
the synthesis, so its read-along works as soon as the narration finishes and no `align` job is
queued for it.

Alignment starts by itself. Every 60 s (`LECTERN_TRACK_RESCAN_SECONDS`), and once at
startup, the server looks in every work directory for audio files (`.mp3 .m4a .m4b .mp4 .ogg
.oga .opus .flac`, in natural filename order, so `01 …`, `02 …`, `10 …` play in order),
updates `audio_tracks`, and queues one `align` job for each work that has an EPUB and at least
one track but no alignment yet. To add an audiobook, import it through the inbox or the web
upload (it lands in `library/<Author>/<Title>/audio/`), or copy its files into the work
directory by hand.

Who runs the job depends on the installation:

| Installation | What happens |
| --- | --- |
| `pip install 'lectern[align]'`, or the image built with `--build-arg LECTERN_EXTRAS=align` (or a list that includes it, such as `"align tts"`; 1.62 GB vs. 1.04 GB default, both with ffmpeg; plus ~0.46 GB per downloaded `small` model under `/data`) | The server runs jobs itself, one at a time, in a background thread (`LECTERN_ALIGN_LOCAL_WORKER=1`, the default). |
| Default image / plain `lectern` (e.g. a Raspberry Pi with an empty `LECTERN_EXTRAS`) | Jobs wait in the queue for an external worker (`lectern worker`, below). The log says so at startup, and while no worker has connected in the last 5 minutes the book's Formats card says alignment cannot run, with a link to this section, instead of "Aligning…". The jobs stay queued and start as soon as a worker connects. |
| Either, with `LECTERN_ALIGN_LOCAL_WORKER=0` | The server never transcribes, even with the extra installed. Use this on a small host. |

`LECTERN_BACKGROUND_JOBS=0` turns off the rescans, lease sweeps, and the local worker
altogether (the server then only serves; jobs can still be requested and run by external
workers, but expired leases are only reclaimed when a worker claims).

To turn alignment on, either rebuild the image with the engine
(`docker compose build --build-arg LECTERN_EXTRAS=align`, or set `LECTERN_EXTRAS=align` in
`.env` for `deploy/tailscale/`) or connect an external worker as described below.
`LECTERN_EXTRAS` takes a space- or comma-separated list, so `LECTERN_EXTRAS="align tts"`
builds one image that both aligns and narrates with Piper ([`tts.md`](tts.md)).

A job can also be requested by hand: `POST /api/works/{id}/align {"model": "medium",
"force": true}` (owner or member). `force` cancels the active job first. Jobs are listed at
`GET /api/jobs`.

## Where to run it (Raspberry Pi, NAS, laptop)

Transcription is the only expensive step; everything else takes well under a second per book.
With the default model (`small`, `int8`, CPU) Lectern measured **2.9× realtime on two x86
vCPUs with 1.4 GB peak memory** on a 13-minute recording (see [Benchmark](#benchmark)): a
10-hour audiobook would take about 3½ hours there. Memory, not CPU, is what rules out small machines.

- **Raspberry Pi 4 or 5 with at least 4 GB, as a fallback.** Run `lectern worker` (or the
  [Mac worker](mac-worker.md)) on a faster machine for most jobs, and build the Pi's image
  with `LECTERN_EXTRAS="align tts"` so it can still align (and narrate with Piper) by itself
  while that machine is off. While a worker is online the Pi leaves each new job to it for
  `LECTERN_LOCAL_DEFER_SECONDS` (10 minutes) before taking it itself
  ([Pi fallback](mac-worker.md#what-it-runs)). CTranslate2 ships ARM64 wheels and CI builds
  and imports this image for `linux/arm64`, but we have **not measured** a Pi, so we quote no
  speed for it; expect it to be much slower than the x86 figure. Use
  `LECTERN_ALIGN_MODEL=base` and `LECTERN_ALIGN_THREADS=` the core count to keep the working
  set (about 1.3 GB with `small`) from crowding the server and the page cache.
- **Smaller boards (under 4 GB).** Run the default (slim) image, or set
  `LECTERN_ALIGN_LOCAL_WORKER=0`, and align with `lectern worker` elsewhere.
- **NAS or mini PC with ≥ 8 GB.** The `align` image is fine. One job runs at a time and the
  thread yields to requests; reading and sync stay responsive, but it will use the CPU for
  hours per book.
- **A Mac with Apple Silicon.** Install the worker with one command and align on the GPU with
  MLX (falls back to the CPU): see [`mac-worker.md`](mac-worker.md).
- **Laptop or desktop with a GPU.** The fastest option: `lectern worker --device cuda
  --compute-type float16 --model medium` (needs the CUDA libraries CTranslate2 expects). It
  connects out to the server, so it works from behind NAT, on the tailnet, or anywhere the
  server's HTTPS address reaches.

### External workers

1. As the owner, create a credential: `POST /api/worker-credentials {"label": "desktop"}`.
   The response holds `secret` (`lw_…`) once; only its SHA-256 is stored.
2. On the worker machine (Python 3.12+):

   ```sh
   pip install 'lectern[align] @ git+https://github.com/jeninh/lectern'   # or uv tool install
   export LECTERN_WORKER_KEY=lw_…            # or --key-file /path/to/key
   lectern worker --server https://lectern.example --name desktop
   ```

   Options: `--model`, `--device`, `--compute-type`, `--threads`, `--models-dir`,
   `--lease` (seconds, 60–3600, default 600), `--poll` (default 15 s, at most 240 s: the
   server treats a worker silent for 5 minutes as gone, so a longer value is lowered with a
   warning), `--workdir` (scratch
   space for the downloaded EPUB and audio, deleted after each job), `--once`. Models are
   downloaded from Hugging Face on first use. `http://` URLs are refused unless you pass
   `--allow-http` (a trusted LAN only: the credential travels in every request).
3. `Ctrl-C` (or `SIGTERM`) stops after the current job is handed back as a retryable failure;
   a second signal stops at once, and the server reclaims the lease when it expires.

Revoke a credential with `DELETE /api/worker-credentials/{id}`: from the next request it no
longer authenticates and its leased jobs go back to the queue at once.

What a worker credential can do: claim jobs, download the inputs *of the jobs it currently
holds a lease on* (the lease token is required on every call, so there are no URLs to leak),
heartbeat, and upload a result or a failure. It is not a user: it cannot read the library,
progress, OPDS, or any other route, and sessions, app passwords, and sync keys are not
accepted by `/api/worker/*`.

## Text extraction

`lectern.align.text` walks the OPF spine with lxml. Each spine item becomes a
`lectern.textmap.SpineText`: exactly the frozen normalised text of
`lectern.positions.normalize_spine_text` (body only, `script`/`style` dropped, whitespace runs
collapsed), plus, for every character, the DOM text node it came from and its offset in that
node, and the block (paragraph, heading, list item, …) it belongs to. The source map is what
converts KOReader xpointers; block boundaries keep sentences from running across paragraphs.

Sentences are numbered over the whole spine in reading order. A sentence ends at `.` `!` `?`
`…` (plus closing quotes or brackets) followed by whitespace and a character that is not a
lowercase letter, except after common abbreviations (`Mr.`, `Dr.`, `St.`, …) and initials
(`O. Henry`). It never crosses a block. A run-on sentence longer than 500 characters is split
at `;`/`:`, which makes highlighting and interpolation finer. Segmentation is deterministic;
the fixture test checks it against the 15 hand-checked sentences.

## Transcription

[faster-whisper](https://github.com/SYSTRAN/faster-whisper) with `word_timestamps=True`,
`vad_filter=True` (Silero VAD skips silence and music), beam size 5, and
`condition_on_previous_text=False` (stops one misheard passage from derailing the next). Tracks
are transcribed in order and each word is placed on the work timeline by adding the track's
`offset_s` (sum of the earlier tracks' durations); word times are clamped to be
non-decreasing. Audio is decoded by PyAV's bundled FFmpeg; no system `ffmpeg` is needed.
PyAV is pinned below 19 because PyAV 19 breaks faster-whisper 1.2. Each track is decoded as a
stream and transcribed in overlapping windows, so memory stays flat however long the track is
(see [Long recordings](#long-recordings)).

| Setting | Default | Notes |
| --- | --- | --- |
| `LECTERN_ALIGN_MODEL` | `small` | `tiny`, `base`, `small`, `medium`, `large-v3`, `large-v3-turbo`, `distil-large-v3`. Also per job. |
| `LECTERN_ALIGN_DEVICE` | `cpu` | `cuda` on a GPU worker |
| `LECTERN_ALIGN_COMPUTE_TYPE` | `int8` | `float16` on GPU |
| `LECTERN_ALIGN_THREADS` | `0` | CTranslate2 threads; 0 = its default |
| `LECTERN_ALIGN_WINDOW_SECONDS` | `300` | Audio transcribed at a time (≥ 10). Memory grows with this, not with the book. Workers: `--window` |
| `LECTERN_ALIGN_OVERLAP_SECONDS` | `15` | Audio shared by neighbouring windows (at most a third of the window). Workers: `--overlap` |
| `LECTERN_ALIGN_MEMORY_LIMIT_MB` | 75 % of RAM / cgroup limit | Resident-memory cap of the local worker's alignment process; `0` = none of our own |
| `LECTERN_ALIGN_LOCAL_WORKER` | `1` | Run jobs in the server when faster-whisper is installed |
| `LECTERN_BACKGROUND_JOBS` | `1` | Rescans, lease sweeps, and the local worker |
| `LECTERN_WORKER_POLL_SECONDS` | `10` | Local worker's idle poll |
| `LECTERN_JOB_SWEEP_SECONDS` | `30` | Expired-lease reclaim interval |
| `LECTERN_ALIGN_STALL_SECONDS` | `3600` | A run whose progress has not moved this long is reclaimed (retried, then failed) |
| `LECTERN_TRACK_RESCAN_SECONDS` | `60` | Audio discovery interval |

Models are cached under `<LECTERN_DATA_DIR>/models` (the server) or the Hugging Face cache
(workers; `--models-dir` to change it). The image sets `HF_HOME=/data/huggingface` so the
read-only container can download into its data volume.

## Matching

Both streams go through the same token normalisation (`lectern.align.tokens`): NFKC,
casefold, apostrophes unified, and numbers read out as words, because Whisper writes digits
(`$1.87`, `1905`, `21st`) where books usually spell them out. Then (`lectern.align.matcher`):

1. **Anchors.** 5-grams that occur exactly once in the text *and* once in the transcript.
   Sorted by text position, the longest chain that is also increasing in the transcript
   (longest increasing subsequence) is kept. Everything off the chain is either noise or
   content read in a different order.
2. **Gaps** between consecutive anchors are matched recursively with 4- and then 3-grams that
   are unique *inside the gap* (3-grams only in gaps of ≤ 400 tokens), and finally by
   Levenshtein alignment of the remaining tokens when the gap is small enough
   (≤ 4 M cells), accepting equal and near-equal (similarity ≥ 0.75) words.
3. **Sentence times.** A sentence runs from its first matched word to its last. Whisper hangs
   leading silence on a phrase's first word, so the start is pulled to no earlier than that
   word's end minus a plausible duration for it. A sentence needs at least 4 matched tokens
   or 20 % of its tokens to count as matched; starts are forced to be non-decreasing.
4. **Interpolation.** A run of unmatched sentences between two matched ones gets the audio in
   between, split by character count, and stays `matched: false`. If that text would need
   more than 3× the available audio (+5 s) at the book's measured speaking rate, the audio
   skips it, and it gets no time at all rather than a wrong one.

What this tolerates, each covered by a test in `tests/test_align.py`:

- **Intros and outros** (LibriVox preambles, credits, music): they never anchor.
- **Missing or skipped chapters**: the gap has no audio, so its sentences stay unmatched with
  no interpolated time.
- **Reordered chapters**: the in-order chain keeps the longest consistent reading; the moved
  chapter is unmatched (sentence audio must be monotone to seek) and counted in
  `stats.reordered_text_tokens`.
- **Misheard passages**: interpolated between their neighbours, flagged unmatched.

### Coverage, diagnosis, abridgment

`coverage` is matched sentences / all sentences of the book, the number in the contract and
on the alignment. Below **85 %** the alignment carries the warning `low_coverage`. Low coverage
has different causes, and only one of them is fixed by a bigger model, so the matcher also
looks at the **narrated span**, the text from the first to the last matched sentence:

| `diagnosis` | Meaning | `suggested_model` |
| --- | --- | --- |
| `ok` | coverage ≥ 85 % | — |
| `partial_audio` | the span matched well (`span_coverage` ≥ 85 %) but the audio covers only part of the text: front matter, a licence, an excerpt, chapters missing at either end | none: a bigger model would not help |
| `poor_transcription` | the span itself matched poorly | the next larger model (`small` → `medium` → `large-v3`) |
| `abridged` | the span's audio is shorter than 80 % of an unabridged reading of it *and* more than 15 % of its words are unmatched (`likely_abridged`, also a warning) | none |
| `no_match` | nothing matched: wrong book, wrong language, or silent audio | the next larger model |

"An unabridged reading of it" is the span's word count divided by the speaking rate measured
on runs of consecutive matched sentences (pauses included). A poor transcription leaves gaps
too, but its audio is as long as the text needs, so it is not mistaken for an abridgment.
Rerun with a larger model with `POST /api/works/{id}/align {"model": "medium", "force": true}`.

Project Gutenberg EPUBs put their licence (about 3,000 words) at the end of the last text
file, which no narrator reads, so a perfectly aligned Gutenberg book shows `partial_audio` with
whole-book coverage well under 100 %. That is expected and correct.

## Jobs, leases, and the ETA

`lectern.align.jobs` implements the align queue for both the in-process worker and
`/api/worker/*` (the in-process transcode jobs from milestone 2 are `lectern.jobs` and share
only the `jobs` table), so the rules exist once: the single-statement atomic claim, 256-bit lease
tokens stored hashed, heartbeats every `min(lease_s / 3, 30 s)`, `min(30 s × 2^(a−1), 1 h) ± 20 %` backoff,
reclaim of expired leases (every 30 s and before each claim), of leases held by revoked
credentials, of the in-process worker's lease at startup (the server restarted or was
OOM-killed mid-run, so nobody holds it any more), and of runs whose progress has not moved
for `LECTERN_ALIGN_STALL_SECONDS` (default 3600; in the sweep and before each claim) even though their worker still heartbeats (each reclaim changes a job only if it is still the same lease it looked at, so a result, cancel, or new claim that lands in between is kept), three attempts, and the result checks in the contract (schema, `work_id`,
`epub_sha256` and `audio_fingerprint` against the job snapshot, the snapshot against the
work's current files, `char_end` against the real spine item lengths). A work whose EPUB or
audio changes gets its active job cancelled and a new one queued; a succeeded alignment becomes
`stale` and its sentences stay readable until the new one lands. When the server shuts down,
the local worker hands its job back without spending an attempt, even when transcription is
in the middle of a long segment.

The ETA in `GET /api/jobs` is **measured, never guessed**: it is `null` until at least one
alignment has succeeded on this server. After that, `eta.seconds` is the job's audio duration
divided by the median speed (audio seconds per wall-clock second) of the last five successful
runs of the same model (any model if none), scaled by the remaining progress for a running job
and including the jobs queued ahead of it. `eta.basis` says how many runs it rests on. It
assumes one worker draining the queue in order. Before any run has succeeded, a running job
that is at least 5 % done is extrapolated from its own progress so far (`eta.speed` is `null`,
`eta.basis` is "this run's progress so far").

The book's Formats card (`GET /api/works/{id}/read-aloud`, `alignment.job`) shows the running
job's heartbeat progress and ETA, a failed attempt waiting out its backoff, and, once attempts
are exhausted, the error with a **Retry** button for owners and members.

- Retry reruns the model that failed, except after running out of memory (the memory cap,
  a `MemoryError`, the OOM killer, or a worker reporting out of memory). Then it steps one
  size down (`medium` → `small` → `base` → `tiny`; turbo and distil count as large) and the
  button says so ("Retry with base"). It never repeats the model that ran out of memory.
- After running out of memory on `tiny` there is no Retry button, and
  `POST /api/works/{id}/align {}` answers `409 out_of_memory_choose_model`: rerun it
  deliberately from the editor (naming a model) after changing the memory limit or window,
  or with an external worker.
- Readers see that alignment failed, not why: the error text (which can quote file paths
  and the alignment process's log) is `null` for them in the read-aloud status, the
  tracks and sentences responses, and the AI tools.

## Cross-format sync

All positions pass through the canonical **sentence position** `(sentence_n, offset)` stored
in `work_progress` (`lectern.progress`):

```diagram
 KOReader xpointer                 canonical position               audio time
 /body/DocFragment[3]/      ┌─▶  (sentence 41, offset 12)  ◀─┐       133.7 s
   body/p[7]/text().12      │            ▲                   │          ▲
         │  SpineText       │            │ SentenceIndex     │          │
         ▼  source map      │            │ at_text / to_text │ at_time / to_time
 text position (3, 1024) ───┘            └───────────────────┴──────────┘
```

- **KOReader → Lectern.** `PUT /syncs/progress` stores KOReader's record exactly as before
  (`reading_progress`, keyed by the document's partial MD5; nothing about that mapping
  changes). When that hash belongs to a work with sentences, the xpointer is also resolved
  against the spine item it names and stored as the work's canonical position with source
  `koreader`. A projection failure never fails the sync (it runs in a savepoint).
- **Only the aligned edition, only visible works.** Projection happens only when the
  document's exact EPUB file is the one the sentences were aligned against and the KOReader
  user may see the work; otherwise sync behaves exactly like koreader-sync-server (no
  `work_progress` reads or writes), so another edition's xpointers are never misapplied and
  a reader cannot probe works outside their shelves.
- **Lectern → KOReader.** `GET /syncs/progress/{document}` returns whichever is newer:
  KOReader's own record, or a web/audio position written after it, converted back into an
  xpointer for that document (device `lectern-audio` / `lectern-web`, the writer's device
  name). So after listening on the phone, KOReader's "sync now" jumps to the sentence being
  spoken.
- **Audio resumes from KOReader.** `GET /api/works/{id}/progress` returns `audio_s`, the
  start of the KOReader position's sentence on the work timeline (or the end of the previous
  matched sentence when that one is unmatched). The player seeks there.

Last write wins, using the server clock only, as between two KOReader devices. xpointers
follow crengine's conventions (1-based indices, written only where siblings share a name;
whitespace-only nodes inside blocks do not count; `autoBoxing`/`floatBox` wrappers are
transparent; offsets count collapsed whitespace). Unresolvable paths clamp to the nearest
position that resolves instead of failing; offsets inside a text node can differ from
crengine's by the number of collapsed whitespace characters, i.e. within a word.

Not in scope: the read-aloud EPUB's xpointers are not merged yet (it is a different document
to KOReader), and there is no emulation of Kobo's native store sync API; Kobo devices sync
through KOReader.

## Benchmark

Measured with `lectern align-bench`, which runs exactly the pipeline the workers run and
prints the statistics. The fixture is Project Gutenberg #7256 (*The Gift of the Magi*) with the
public-domain LibriVox reading (provenance: `tests/fixtures/magi/PROVENANCE.md`).

```sh
uv sync --extra align
uv run lectern align-bench tests/fixtures/magi/the-gift-of-the-magi.epub \
    tests/fixtures/magi/excerpt.mp3 -o /tmp/excerpt_map.json
python tests/fixtures/magi/fetch_audio.py /tmp/magi      # the full 13:22 recording, verified
uv run lectern align-bench tests/fixtures/magi/the-gift-of-the-magi.epub \
    /tmp/magi/gift_of_the_magi_henry_blb_64kb.mp3 --words-out /tmp/magi/words.json
# re-match the saved words without a model (seconds):
uv run lectern align-bench tests/fixtures/magi/the-gift-of-the-magi.epub \
    /tmp/magi/gift_of_the_magi_henry_blb_64kb.mp3 --transcript /tmp/magi/words.json
```

Measured 2026-10-20 in an Amp orb: Intel Xeon @ 2.60 GHz, **2 vCPUs**, 3.8 GB RAM, Linux
x86-64, Python 3.14, faster-whisper 1.2.1 / CTranslate2 CPU, model `small`, `int8`, beam 5,
default threads. One run each; wall clock from `/usr/bin/time`.

| | Excerpt (`excerpt.mp3`) | Full recording (64 kb/s MP3) |
| --- | --- | --- |
| Audio | 77.2 s | 802.5 s (13:22) |
| Model load (cached) | 1.4 s | 1.4 s |
| Transcription | 39.6 s | 278.3 s |
| Speed (audio s per wall s, whole pipeline) | **1.95×** realtime | **2.88×** realtime |
| Text extraction + matching | 0.03 s | 0.04 s |
| Peak RSS | 1.33 GB | 1.38 GB |
| Words transcribed | 198 | 2,145 |
| Sentences matched / in book | 15 / 293 | 155 / 293 |
| `coverage` (whole book) | 0.051 | 0.529 |
| Narrated span | sentences 14–28 | sentences 12–166 (title → "\*\*\* END OF …") |
| `span_coverage` | 1.0 | 1.0 |
| `diagnosis` | `partial_audio` | `partial_audio` |
| Sentence start error vs. hand map (15 sentences) | median 0.18 s, max 0.94 s | median 0.16 s, max 0.36 s |
| Sentence end error vs. hand map | median 0.15 s, max 0.60 s | median 0.22 s, max 0.61 s |

The full file is faster per second of audio than the excerpt because fixed costs (VAD,
language detection, the first decoder passes) are spread over more audio. At ~2.9× realtime, a
10-hour audiobook would take about 3½ hours on the same two vCPUs, extrapolated, not measured.

Notes:

- **Why coverage is low in both runs.** The EPUB is 293 sentences: 14 of Gutenberg header,
  152 of story, the `*** END OF …` line, and 126 of Gutenberg licence. The excerpt narrates only the story's
  first 15 sentences, so 15 / 293 ≈ 5 % is low *by design*. The full recording narrates the
  title and the whole story but, of course, not the licence, so 53 % is the most it can reach.
  The number that says the alignment worked is `span_coverage` = 1.0 with diagnosis
  `partial_audio`: every sentence from the first to the last narrated one matched, so a larger
  model is not suggested. The LibriVox preamble and the Gutenberg header share common words
  but are not matched to each other, and the narrator's closing "End of *The Gift of the
  Magi*" matches the `*** END OF …` line, which is what was said. Against the hand-checked map, the
  recorded transcript (`tests/fixtures/magi/transcript_small.json`) places sentence starts
  with a median error under 0.3 s and every start within 1 s
  (`test_recorded_transcript_aligns_close_to_the_hand_checked_map`).
- Speed is wall-clock and includes decoding and VAD; "model load" is from the local cache
  (the first run also downloads ~460 MB).
- Peak RSS is the whole process. The table above predates windowing, when it grew with the
  length of the track (see "Long recordings" below); matching a full novel adds tens of MB.
- Whisper output varies between runs and builds: the checked-in transcript has 207 words for
  the excerpt, this run 198 (different splits of hyphenated and numeric words); matching is
  insensitive to that. Timing varies by a few percent run to run on a shared vCPU.
- No GPU, Raspberry Pi, or ARM numbers: none were measured.

Reproduce the recorded transcript with
`lectern align-transcribe tests/fixtures/magi/excerpt.mp3 -o transcript_small.json`.

## Long recordings

A single-file audiobook can be 10–40 hours. Before this section existed, faster-whisper was
handed the whole decoded track: about 70 bytes of transient memory per 16 kHz sample (the
float32 audio, VAD, and the STFT and mel features of all of it), roughly 1.1 MB per second of
audio on top of the model, so a 10-hour track would have needed ~40 GB.

**Windows.** Each track is now decoded as a stream (PyAV, 16 kHz mono int16, the same
resampling as `faster_whisper.decode_audio`) and transcribed in windows of
`LECTERN_ALIGN_WINDOW_SECONDS` (default 300 s) that overlap by `LECTERN_ALIGN_OVERLAP_SECONDS`
(default 15 s; at most a third of the window). Only the current window's samples are held. Each
window is transcribed with the same options as before (VAD, beam 5, no conditioning on previous
text), and its word times are shifted by the window's start and the track's `offset_s`, so the
work keeps one absolute timeline across windows and tracks.

**Seams.** Two neighbouring windows both hear the overlap. The words are joined at one cut
inside it: the middle of the longest pause between words of the earlier window in the central
half of the overlap (the edges of a window are where Whisper is least reliable), else the
overlap's midpoint. Earlier-window words before the cut and later-window words from the cut
on are kept; if the first kept later word repeats the last kept earlier word (same normalised
text, starts within 1 s) it is dropped. The cut depends only on the earlier window's words, so
the merge is deterministic. Language is detected once, on the first window that contains speech,
and then fixed for the rest of the work. Word start times stay non-decreasing.

**The rest is small.** The EPUB is read one spine item at a time and only its sentences and
tokens are kept; a long novel's text is a few MB, and matching adds tens of MB. Only the merged
word list (a few hundred bytes per word, ~20 MB for a 10-hour book) grows with the length of the audio. The child process writes
the sentence map to a file instead of piping it.

### Failure isolation

The in-server worker (`LECTERN_ALIGN_LOCAL_WORKER`) never transcribes in the web server's
process. For each job it starts `python -m lectern.align.child`, sends the job on stdin, and reads
progress lines from the child's stdout. The server keeps the lease and the heartbeats.

- The child sets its Linux `oom_score_adj` to 1000, so if the machine runs out of memory the
  kernel kills the alignment, not the server. It also runs at `nice` +10, never dumps core, and
  exits if its parent dies.
- The server watches the child's resident memory (`/proc/<pid>/status`) and kills it above
  `LECTERN_ALIGN_MEMORY_LIMIT_MB`. The default is 75 % of the memory available to the server
  (physical RAM or the container's cgroup limit, whichever is lower). `0` turns this cap off and
  leaves only the kernel's.
- The child's stderr is kept in `<LECTERN_DATA_DIR>/logs/align/job-<id>-attempt-<n>.log` (the 50
  most recent are kept), and its tail is quoted in the job's error.

| What happened | Job |
| --- | --- |
| Child finished | Result checked and stored exactly as a worker upload would be |
| Pipeline raised (bad file, no speech) | Failed or retried, as the error decides (unchanged) |
| Over the memory cap, `MemoryError` (exit 3), or a `SIGKILL` the server did not send (the OOM killer) | **Failed, not retried**: the same input, model, and limit would fail again. The error says to raise the limit, use a smaller model or window, or run an external worker |
| Any other signal (e.g. `SIGSEGV`) or exit code | Retried under the usual attempts and backoff (3 attempts) |
| Server shutting down | Child terminated, job **released** without spending an attempt |
| Lease lost (reclaimed, cancelled, credential revoked) | Child killed, result discarded |
| Server itself killed | Child exits; the restarted server requeues the job at once (an attempt is spent, so a server that keeps dying ends in a visible failure) |
| Child hangs but stays alive | Reclaimed after `LECTERN_ALIGN_STALL_SECONDS` without progress, then retried or failed |

External workers (`lectern worker --server …`) transcribe in their own process as before. They
are already isolated from the server and use the same windows (`--window`, `--overlap`).

### Measurements

Measured 2026-10-20 in an Amp orb: 2 vCPUs (Intel Xeon @ 2.60 GHz), 3.8 GB RAM, Linux x86-64,
Python 3.14, faster-whisper 1.2.1, CPU `int8`, `OMP_NUM_THREADS=2`, one run each. "Before" is
the code at commit `70acd67`; "after" uses the default 300 s / 15 s windows. Peak RSS is the
`align-bench` process's own `ru_maxrss` (the whole process: Python, model, audio). Runs were
killed at 2.8 GB to keep the orb alive.

The long files are the excerpt repeated with 2 s gaps
(`tests/fixtures/magi/make_long_audio.py --minutes N`). This is a memory and speed test only:
repeated text cannot be matched to one story, so coverage on those files means nothing. It is
not committed.

| Audio (model `tiny`) | Before: peak RSS | Before: time | After: peak RSS | After: time |
| --- | --- | --- | --- | --- |
| 15 min | 1,266 MB | 51 s | 668 MB | 55 s |
| 30 min | 1,993 MB | 100 s | 691 MB | 102 s |
| 45 min | > 2,800 MB (killed) | — | 690 MB | 155 s |
| 60 min | > 2,800 MB (killed) | — | 671 MB | 202 s |
| 120 min | not run | — | 676 MB | 361 s |
| 600 min (10 h) | not run | — | 708 MB | 1,974 s |

The window sets the memory (30-minute file, `tiny`, 30 s overlap): 120 s → 468 MB at 13.0×
realtime, 300 s → 674 MB at 16.5×, 600 s → 1,019 MB at 18.7×, 900 s → 1,361 MB at 18.7×. Short
windows cost speed because each one pays for VAD, the first decoder passes, and its overlap;
300 s keeps most of the speed.

Quality on the real 13:22 recording (model `small`), compared with the hand-checked map
(sentences 0–14, shifted by +24.50 s) and with whole-file transcription:

| | Before (whole file) | After, 300 s windows | After, 120 s windows |
| --- | --- | --- | --- |
| Peak RSS | 1,345 MB | 1,076 MB | 945 MB |
| Time / speed | 238 s / 3.37× | 245 s / 3.28× | 277 s / 2.89× |
| Words | 2,145 | 2,145 | 2,149 |
| Sentences matched | 155 | 154 | 155 |
| `span_coverage` / diagnosis | 1.0 / `partial_audio` | 0.994 / `partial_audio` | 1.0 / `partial_audio` |
| Start error vs. hand map | median 0.16 s, max 0.36 s | median 0.17 s, max 0.31 s | median 0.16 s, max 0.36 s |
| End error vs. hand map | median 0.22 s, max 0.61 s | median 0.17 s, max 0.59 s | median 0.21 s, max 0.61 s |
| Start vs. whole file (all matched) | — | median 0.04 s, max 0.66 s | median 0.03 s, max 0.73 s |

The one sentence the 300 s run missed starts at 154 s, inside the first window and far from a
seam: Whisper heard it slightly differently. The 120 s run crosses six seams and matches all 155.

Honest limits:

- `small` needs about 1.1 GB with the default window, `tiny` about 0.7 GB. Both are flat in
  the length of the book and fit a 4 GB Raspberry Pi with the server alongside. **No Pi was
  measured**; ARM speed will be lower than these x86 numbers.
- The memory cap is enforced by polling every 0.2 s, so a very fast spike can overshoot it
  before the kill. `oom_score_adj` still makes the kernel pick the child first.
- `oom_score_adj` and `/proc` are Linux-only. On other systems the child still isolates
  crashes, but there is no memory cap.
- PyAV stays pinned below 19 (`av>=11,<19`) because PyAV 19 breaks faster-whisper 1.2.
