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
   curl -fsSL https://lectern.example/worker/install.sh | LECTERN_WORKER_CODE=lwe_… sh
   ```

2. Paste it into Terminal on the Mac. The code travels in the environment, never in a
   process's arguments, so `ps` does not show it. It is single use and expires, so the copy
   left in the shell history is worthless once used. To keep it out of the history as well,
   run the command without `LECTERN_WORKER_CODE=…`; the installer then asks for the code. It installs [uv](https://docs.astral.sh/uv/) if needed,
   then `lectern[mac]` as a uv tool (Python 3.12, MLX Whisper, Kokoro with spaCy's English
   model, the menu bar), then ffmpeg with Homebrew if Homebrew is present. It then runs
   `lectern worker-doctor --prepare` (below), which downloads the alignment model, Kokoro's
   weights, and its voices with progress and tries each engine once. It exchanges the code for
   a worker key and installs two launch agents. Nothing runs as root. Run the same command again with a new code to upgrade or
   to re-enroll.

Options: `--name`, `--proxy`, `--ca-bundle` (see [School mode](#school-and-proxy-mode)),
`--no-login` (no menu bar at login), `--source` (where to install Lectern from, default
`LECTERN_WORKER_INSTALL_SOURCE` on the server, `git+https://github.com/jeninh/lectern`).
The installer pins that source to the server's own commit (`@<LECTERN_WORKER_INSTALL_REF>`),
so the worker speaks the same protocol as the server. The image sets the ref from the
`LECTERN_COMMIT` build argument: `LECTERN_COMMIT=$(git rev-parse HEAD) docker compose … up -d
--build`. **It is off unless you pass it** (the image cannot see the git checkout): without
it the newest code on the default branch is installed and the installer says so.
**The repository is private**, so the Mac needs read access to it. The installer checks first
(`git ls-remote`) and stops with instructions if it cannot read the repository: sign in with
`gh auth login` and `gh auth setup-git`, use an SSH key with
`--source git+ssh://git@github.com/jeninh/lectern`, or point `--source` at a wheel or a
checkout you can reach, for example `--source /Users/me/src/lectern`.

Without the installer:

```sh
uv tool install --python 3.12 'lectern[mac] @ git+https://github.com/jeninh/lectern'
LECTERN_WORKER_CODE=lwe_… lectern worker-enroll --server https://lectern.example   # also installs launchd
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
`model` is `mlx-whisper-<model>` (or the CPU label after a fallback; only the conversions
listed in `MLX_REPOS` are loaded); retries and "larger
model" suggestions treat both the same. No Mac timings are quoted here: none were measured.

Choose with `lectern worker --engine auto|mlx|cpu` or `"engine"` in `config.json`.

## Self-test: `lectern worker-doctor`

The worker offers a capability only after it has worked on this Mac. At every start the agent
(and `lectern worker`) runs these checks in a child process, downloading missing models first,
and leaves out every capability that fails. The reason goes to the server and appears under
**Admin → Workers** as "not offering tts-kokoro: …". Run the checks yourself any time:

```sh
lectern worker-doctor             # check only; downloads nothing
lectern worker-doctor --prepare   # download models and voices, repair, then check
```

| Capability | Checks |
| --- | --- |
| `align` | the engine loads (MLX, or faster-whisper), its model is on disk, one second of audio transcribes |
| `tts-kokoro` | `kokoro` and its English G2P import; spaCy's `en_core_web_sm` is installed; ffmpeg is on the PATH; the weights (`kokoro-v1_0.pth`, `config.json`) and a voice are cached (`--prepare`: every voice Lectern offers); one sentence is synthesized |
| `transcode` | ffmpeg encodes a fraction of a second of AAC |

`--prepare` also installs the pinned spaCy model when it is missing, and restarts the worker
service afterwards so it picks up what now passes. Downloads use the worker's proxy and extra
CA certificates. Set `"self_test": false` in `config.json` (or `lectern worker
--no-self-test`) to skip the checks at start.

## When a job crashes the worker

An engine that ends the process (a library calling `sys.exit()`, a segfault, an out-of-memory
kill) no longer takes the job down with the worker:

- A `SystemExit` or other non-`Exception` error from an engine is caught: the job is reported
  failed right away with the reason (for example `the engine called sys.exit(2)`), the full
  traceback goes to the worker log, the server queues it again after its usual backoff
  (30 s, then doubling, at most an hour), and the worker stops offering that capability for
  30 minutes.
- A process that dies outright leaves `current-job.json` in the worker's folder. The next start
  reports that job as crashed, before the self-test and even while paused; the marker stays
  until the server has answered. A worker that was being stopped (Quit, SIGTERM, `lectern
  worker-doctor --prepare` restarting it) and was killed before it could hand the job back
  marks the file `stopping`: the next start hands the job back instead, so a restart never
  counts as a crash. If the lease already expired the server reclaimed the job on its own (an
  attempt, not a crash). Segfaults also leave a Python traceback in the log (`faulthandler`).
- **A job that crashes a worker twice fails for good**, with the last error: "crashed the
  worker 2 times; last error: …". It shows in the book's Formats card and under Admin →
  Workers → Recently failed. Retry it once the cause is fixed.

**Admin → Workers → Queue** says why each waiting job waits: the capability it needs, every
worker and why it cannot take the job (offline, paused and why, not offering it and why, its
allowlist, busy with another job), the lease holder and expiry, when a backoff ends, attempts,
crashes, and the last error. `GET /api/jobs?status=active` returns the same as `waiting`.

### Troubleshooting: Kokoro jobs stuck at 0 % ("No virtual environment found")

Workers installed before this fix loop on Kokoro jobs: the worker claims the job, loads
Kokoro, and exits with `No virtual environment found; run uv venv … or pass --system` in its
log. Kokoro's English G2P (misaki) needs spaCy's `en_core_web_sm` and, when it is missing,
tries to install it with `uv pip install` in the middle of the job. A uv tool has no virtual
environment to install into, and spaCy then calls `sys.exit()`, killing the worker with the
lease held. The model is now a pinned dependency. To fix an existing Mac, either:

- run the installer again with a new code from Admin → Workers (upgrades everything), or
- upgrade in place without re-enrolling:

  ```sh
  uv tool install --force --python 3.12 'lectern[mac] @ git+https://github.com/jeninh/lectern'
  lectern worker-doctor --prepare     # downloads Kokoro's weights and voices, restarts the worker
  ```

- or, keeping the installed version, add only the model:

  ```sh
  uv pip install --python ~/.local/share/uv/tools/lectern/bin/python \
    https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
  lectern worker-service install      # restart the worker
  ```

Jobs that already failed stay failed; press **Retry** on the book.

## Credentials stay job-only

The worker key (`lw_…`) is accepted only by `/api/worker/*`. With it the Mac can claim jobs
it is allowed to run, download the inputs of jobs it holds a lease on (the lease token is
required on every call), upload artifacts and results for those jobs, and report its status.
It cannot read the library, progress, OPDS, or any user route. The owner can:

- **Revoke** it (Admin → Workers → Revoke): the next request fails, its leased jobs return
  to the queue at once without spending an attempt, and every artifact it uploaded is
  deleted, so no later attempt resumes from (or publishes) its output.
- **Limit** it to some capabilities ("Only align", or `PATCH /api/worker-credentials/{id}
  {"capabilities": ["align"]}`). Leases the narrower list no longer permits go back to the
  queue at once.
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
  certificate *in addition to* the public roots, so the same Mac still works at home.
  Without it the worker fails with a certificate error, by design: it never turns
  verification off. `proxy: none` ignores proxy environment variables too.
- **Model downloads** (Hugging Face, by mlx-whisper, faster-whisper, and Kokoro) go through
  the same proxy and trust the same extra CA: the agent exports `HTTPS_PROXY`/`HTTP_PROXY`
  and `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` (public roots plus your CA, written to
  `ca-bundle.pem` in the worker's folder) before any engine loads.
- **Blocked downloads.** If an engine still cannot load its model, the worker hands the job
  back without spending an attempt and stops offering that capability for 30 minutes, so the
  Pi or another worker takes it. Download the models on another network first (they are
  cached in `~/.cache/huggingface`). If MLX cannot load but the CPU model is cached,
  alignment falls back to the CPU for that job and tries MLX again on the next.
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
