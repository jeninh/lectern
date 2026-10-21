# The Mac worker

A Mac with Apple Silicon is much faster than a Raspberry Pi at the heavy jobs: aligning an
audiobook with its EPUB and making generated audiobooks. The Lectern worker runs those jobs on
the Mac's GPU and hands the results back to your server. It connects out to the server, so it
works from behind NAT, on the tailnet, or through a school proxy, and the server never
connects to it.

```diagram
┌──────────────────────┐   claim (capabilities)   ┌───────────────────┐
│ Mac                  │ ───────────────────────▶ │ Lectern (the Pi)  │
│  menu bar ⇄ agent    │ ◀── job + lease ──────── │  queue: align,    │
│  align: mlx-whisper  │   inputs / artifacts     │  tts-kokoro, …    │
│  tts:   Kokoro       │ ───────────────────────▶ │                   │
│  transcode: ffmpeg   │   result                 │  runs jobs itself │
└──────────────────────┘                          │  when no Mac is on│
                                                  └───────────────────┘
```

## Installing (one command)

1. In Lectern, as the owner: **Admin → Workers → Add a worker**, give it a name. Lectern shows
   a command with a one-time code (valid for an hour, single use, stored hashed):

   ```sh
   curl -fsSL https://lectern.example/worker/install.sh | sh -s -- --code lwe_…
   ```

2. Paste it into Terminal on the Mac. It installs [uv](https://docs.astral.sh/uv/) if needed,
   then `lectern[mac]` as a uv tool (Python 3.12, MLX Whisper, Kokoro, the menu bar), then ffmpeg
   with Homebrew if Homebrew is present. It exchanges the code for a worker key and installs two
   launch agents. Nothing runs as root. Run the same command again with a new code to upgrade or
   to re-enroll.

Options: `--name`, `--proxy`, `--ca-bundle` (see [School mode](#school-and-proxy-mode)),
`--no-login` (no menu bar at login), `--source` (where to install Lectern from, default
`LECTERN_WORKER_INSTALL_SOURCE` on the server, `git+https://github.com/jeninh/lectern`).
**The repository is private**, so the Mac needs read access to it: sign in with
`gh auth login` (git uses its credential helper), or point `--source` at a wheel or a checkout
you can reach, for example `--source /Users/me/src/lectern`.

Without the installer:

```sh
uv tool install --python 3.12 'lectern[mac] @ git+https://github.com/jeninh/lectern'
lectern worker-enroll --server https://lectern.example --code lwe_…   # also installs launchd
```

Files: settings in `~/Library/Application Support/Lectern Worker/config.json`, the key in
`…/key` (mode 0600), logs in `~/Library/Logs/Lectern Worker/`. Remove everything with
`lectern worker-service uninstall`, `uv tool uninstall lectern`, and deleting that folder;
then revoke the worker in Admin.

## The menu bar

```
📗  Lectern Worker
    Making audiobook work 12: 43 %
    chapter 3: 210/480 sentences
    Runs: align, tts-kokoro, transcode
    ─────────
  ✓ Worker on
  ✓ Start at login
  ✓ Only when plugged in
    Pause on battery below 30 %
    Only on Wi-Fi
    ─────────
    Open Lectern…   Show logs   Quit menu bar
```

- **Worker on**: off stops taking jobs; a running job is handed back at once without
  counting as a failed attempt (the server re-queues it, and another worker or the Pi can take
  it). Artifacts already uploaded are kept, so a generated audiobook resumes from the last
  finished chapter.
- **Start at login**: the menu bar starts at login. The worker itself is a separate launch
  agent that always starts at login and pauses itself according to these settings; quitting the
  menu bar does not stop it, **Worker on** does.
- **Only when plugged in** pauses on battery. With it off, **Pause on battery below N %**
  pauses on battery under that charge (`battery_min_percent` in `config.json`, default 30; 0 =
  never).
- **Only on Wi-Fi** runs only while the active connection is Wi-Fi (not tethering through a
  phone's USB or Ethernet). To limit it to some networks, list them in `wifi_networks` in
  `config.json`. macOS shows network names only to apps allowed to use Location; without that
  permission the worker pauses with "Wi-Fi network name unavailable".
- The icon shows 📗 ready, 📖 and a percentage while busy, 📙 paused or not running, 📕 off.

The agent re-reads `config.json` every 2 seconds and checks power and network every 10, so a
toggle or unplugging takes effect within seconds; a job in progress is handed back at its next
progress report.

## What it runs

The worker advertises what it can run each time it asks for a job; the server leases a job
only to a worker that has its capability ([contracts](contracts.md#capabilities-and-routing)).

| Capability | Engine on the Mac | Without the Mac |
| --- | --- | --- |
| `align` | **mlx-whisper** on the GPU (`mlx-community/whisper-<model>-mlx`), same windows and matcher as the CPU engine. Falls back to faster-whisper on the CPU when MLX cannot load (no Metal device, blocked download) or fails mid-job; the window is redone and the rest of the run stays on the CPU | The server's local worker, if built with `LECTERN_EXTRAS=align` |
| `tts-kokoro` | **Kokoro-82M** (`kokoro` package, PyTorch on MPS), one sentence at a time so every sentence's timing is exact; chapters encoded with ffmpeg | Piper on the Pi (the generated-audiobooks feature decides when to fall back) |
| `transcode` | ffmpeg (data-saver renditions) | ffmpeg on the server |
| `tts-elevenlabs` | never: server only, the ElevenLabs key never leaves the server | the server |
| `ocr` | reserved | — |

**Pi fallback.** While a worker that can run a capability is online and not paused, the
server's own runners leave that capability's jobs to it for `LECTERN_LOCAL_DEFER_SECONDS`
(default 600) after they were queued, then run them anyway. A paused or offline Mac (on
battery, closed, switched off) does not count, so the Pi starts at once. Set it to `0` to have
the Pi never wait.

MLX differs from faster-whisper in two ways that matter little here: it has no Silero VAD
(the worker skips silent stretches with mlx-whisper's `hallucination_silence_threshold`, and
the matcher ignores words that match no text), and it decodes greedily. The sentence map's
`model` is `mlx-whisper-<model>` (or the CPU label after a fallback); retries and "larger
model" suggestions treat both the same. No Mac timings are quoted here: none were measured.

Choose with `lectern worker --engine auto|mlx|cpu` or `"engine"` in `config.json`.

## Credentials stay job-only

The worker key (`lw_…`) is accepted only by `/api/worker/*`. With it the Mac can claim jobs
it is allowed to run, download the inputs of jobs it holds a lease on (the lease token is
required on every call), upload artifacts and results for those jobs, and report its status.
It cannot read the library, progress, OPDS, or any user route. The owner can:

- **Revoke** it (Admin → Workers → Revoke): the next request fails, and its leased jobs return
  to the queue at once.
- **Limit** it to some capabilities ("Only align", or `PATCH /api/worker-credentials/{id}
  {"capabilities": ["align"]}`).
- See its status, platform, engines, and the job it is running.

The enrollment code is single use and expires; it is not a credential. The install script is
public and contains no secret.

## School and proxy mode

On a school, library, or office network the Mac often reaches the Internet only through an
HTTP proxy, sometimes one that inspects HTTPS with its own certificate. The worker only ever
makes outgoing HTTPS requests to your Lectern server (and to Hugging Face once, to download
models), so it works there:

- **Proxy.** `proxy` in `config.json` (or `--proxy` to the installer and `lectern worker`):
  `system` (default: `HTTPS_PROXY`, else the macOS proxy settings from System Settings →
  Network → Proxies; automatic proxy configuration (PAC) files are not evaluated), `none`, or a
  URL such as `http://proxy.school.example:3128`.
- **TLS inspection.** `ca_bundle` / `--ca-bundle /path/school-ca.pem` trusts the network's
  certificate for connections to your server. Without it the worker fails with a certificate
  error, by design: it never turns verification off.
- **Blocked downloads.** If Hugging Face is blocked, download the models on another network
  first (they are cached in `~/.cache/huggingface`). If MLX models are blocked but the CPU
  model is cached, alignment falls back to the CPU engine.
- **Tailscale** blocked? Give the server a public HTTPS address (for example `tailscale
  funnel` for `/api/worker/*` only, or Cloudflare Tunnel; see
  [deploy-tailscale.md](deploy-tailscale.md)). The worker key is all the worker needs; it never
  uses a user's sign-in.
- **Battery and Wi-Fi rules** keep a school laptop from running on battery in class. Use
  "Only on Wi-Fi" with `wifi_networks: ["Home"]` to run only at home.

## Running it elsewhere

`lectern worker --server … --capabilities align,tts-kokoro,transcode` runs the same worker in
a terminal on any machine (Linux with a GPU: `--engine cpu --device cuda`). It has no menu bar
and no power rules. `lectern worker-agent` runs the configured agent in the foreground.
