# Resource limits

Lectern runs on a Raspberry Pi next to the web reader and KOReader sync. The heavy work it does itself (narrating with Kokoro or Piper, text recognition, PDF and Kindle conversion, alignment, data-saver audio) is capped so that reading and syncing stay responsive while it runs.

The defaults suit a **Raspberry Pi 4B with 8 GB**: one heavy job at a time, each in its own process at a low CPU priority, with a memory cap. You only need to change anything if you have a faster machine and want more done at once.

Jobs that a [Mac worker](mac-worker.md) runs never count against these limits. They use the Mac's resources, not the Pi's.

## What is limited

Each feature has four limits:

- **Concurrency**: how many of its jobs may run at once on this server. `0` means never here: those jobs wait for a worker.
- **Threads**: how many CPU cores its engine may use.
- **Memory**: the most memory its process may use, in MiB. A job that needs more stops with a clear error ("needed more memory than … allows"), and the server keeps running. `0` turns off Lectern's own cap.
- **Nice**: its CPU priority, from 0 (normal) to 19 (lowest). The web server always runs at 0, so it wins.

On top of that, `LECTERN_HEAVY_JOBS_MAX` caps the heavy jobs of every feature together, so OCR, a narration, and a conversion never all start at once. A job that is waiting for a slot stays queued and starts by itself when one frees up.

## Settings

Set these in `.env` and restart Lectern (`docker compose … up -d`). An empty value means the default. An invalid value (not a number, or out of range) does not stop Lectern: it logs `LECTERN_… is invalid, using default …` and uses the default.

| Setting | Default | What it does |
| --- | --- | --- |
| `LECTERN_HEAVY_JOBS_MAX` | 1 | Heavy jobs of all features together (at least 1; to leave a feature to workers, set its `_CONCURRENCY` to 0) |
| `LECTERN_KOKORO_CONCURRENCY` | 1 | Kokoro narrations at once (only with `LECTERN_TTS_KOKORO_LOCAL=1`) |
| `LECTERN_KOKORO_THREADS` | 2 | CPU cores for Kokoro |
| `LECTERN_KOKORO_MEMORY_LIMIT_MB` | 3072 | Memory cap for Kokoro |
| `LECTERN_KOKORO_NICE` | 10 | Kokoro's CPU priority |
| `LECTERN_PIPER_CONCURRENCY` | 1 | Piper narrations at once |
| `LECTERN_PIPER_THREADS` | 2 | CPU cores for Piper |
| `LECTERN_PIPER_MEMORY_LIMIT_MB` | 1024 | Memory cap for Piper |
| `LECTERN_PIPER_NICE` | 10 | Piper's CPU priority |
| `LECTERN_OCR_CONCURRENCY` | 1 | OCR jobs at once |
| `LECTERN_OCR_THREADS` | 1 | CPU cores for Tesseract, per page |
| `LECTERN_OCR_MEMORY_LIMIT_MB` | 1536 | Memory cap for each page |
| `LECTERN_OCR_NICE` | 10 | OCR's CPU priority |
| `LECTERN_CONVERT_CONCURRENCY` | 1 | PDF → EPUB jobs at once |
| `LECTERN_CONVERT_MEMORY_LIMIT_MB` | 1536 | Memory cap for PDF → EPUB and MOBI/AZW3 conversion |
| `LECTERN_CONVERT_NICE` | 10 | Their CPU priority |
| `LECTERN_ALIGN_CONCURRENCY` | 1 | Alignments at once |
| `LECTERN_ALIGN_THREADS` | 0 | CPU cores for transcription (0 = all) |
| `LECTERN_ALIGN_MEMORY_LIMIT_MB` | 75 % of RAM | Memory cap for alignment |
| `LECTERN_ALIGN_NICE` | 10 | Alignment's CPU priority |
| `LECTERN_TRANSCODE_CONCURRENCY` | 1 | Data-saver copies made at once |
| `LECTERN_TRANSCODE_THREADS` | 1 | CPU cores for ffmpeg |
| `LECTERN_TRANSCODE_MEMORY_LIMIT_MB` | 1024 | Memory cap for ffmpeg |
| `LECTERN_TRANSCODE_NICE` | 19 | ffmpeg's CPU priority |

MOBI and AZW3 books are converted when they are imported, not as a job. They share the import's parser slots instead of counting as a heavy job, but they use the conversion memory cap and priority.

Saved web articles and ElevenLabs narration are light (the work happens elsewhere), so they never wait for a slot.

## Seeing the limits

**Admin → Resource limits** shows the limits in force, what is running now, and which features have jobs waiting because a limit was reached. It shows values only; the `.env` file itself is never shown or logged.

## Examples

A Pi 5 or a small PC with 16 GB that should OCR and narrate at the same time:

```
LECTERN_HEAVY_JOBS_MAX=2
```

Leave Kokoro to the Mac even though it is installed on the server:

```
LECTERN_KOKORO_CONCURRENCY=0
```
