# Running Lectern at home with Tailscale

This guide is for a first-time self-hoster. At the end, Lectern runs on a machine at home and
is reachable from your phone, laptop, and e-reader anywhere, at
`https://lectern.<your-tailnet>.ts.net`, with **no port opened on your router, nothing
published on your LAN, and nothing on the public Internet**. HTTPS is handled for you, so
passkeys work. Only devices signed in to your Tailscale network (your *tailnet*) can even see
the server.

How it works: Tailscale gives each of your devices a private address (`100.x.y.z`) and an
encrypted connection between them, through any router or firewall, without configuration.
`tailscale serve` is Tailscale's built-in HTTPS front door: it takes `https://lectern.<your-
tailnet>.ts.net`, terminates TLS with a certificate Tailscale provisions, and hands the request
to Lectern over loopback. Everything ships in the repository as
[`deploy/tailscale/`](../deploy/tailscale/compose.yaml); you only write a small `.env`, so
`git pull` keeps working. E-readers running KOReader talk to Lectern's port directly over the
tailnet (see [Kindle and Kobo](#kindle-and-kobo-with-koreader) for why).

```diagram
 phone / laptop (Tailscale app)            e-reader (KOReader + Tailscale plugin)
   │ https://lectern.<tailnet>.ts.net          │ http://100.x.y.z:8080  (OPDS + sync)
   │                                           │
   ▼            encrypted tailnet (WireGuard)  ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │ your home machine · Docker Compose · one shared network namespace   │
 │                                                                     │
 │  ┌──────────────────────┐  X-Forwarded-For: 100.x.y.z  ┌─────────┐  │
 │  │ tailscale container  │ ── http://127.0.0.1:8080 ──▶ │ lectern │  │
 │  │ tailscale serve :443 │                              │  :8080  │  │
 │  └──────────────────────┘        direct from tailnet ─▶│         │  │
 │                                                        └─────────┘  │
 │  no `ports:` anywhere — the LAN and the Internet see nothing        │
 └─────────────────────────────────────────────────────────────────────┘
```

Lectern's own sign-in (password + TOTP or passkey; app passwords and sync keys for KOReader)
stays mandatory. The tailnet decides *which devices* can reach the server; Lectern decides
*who* is reading, and keeps its audit log and rate limits regardless.

## You need

- A machine at home that runs Docker Engine with the Compose and Buildx plugins (or Docker
  Desktop). Check with `docker compose version`. A Raspberry Pi 4 or 5 is enough; see
  [Raspberry Pi 4](#raspberry-pi-4) for what to expect there. Any old laptop works too.
- A free Tailscale account (<https://login.tailscale.com/start>). Sign in with Google, Apple,
  Microsoft, GitHub, or an e-mail passkey; the Personal plan allows 100 devices and 3 users.
- About 30 minutes, plus the first build (20–40 minutes on a Pi 4).

## Sidecar or host install?

There are two places the Tailscale software can run. **This guide uses the sidecar.**

| | **Sidecar** (Tailscale in a container next to Lectern) | **Host install** (Tailscale on the machine itself) |
| --- | --- | --- |
| Setup | The repository ships it ready to use: `deploy/tailscale/compose.yaml` plus your `.env`; nothing to install on the host but Docker | Install Tailscale on the host, keep Lectern's stock `compose.yaml`, run two `tailscale serve` commands |
| Machine name | Lectern is its own tailnet machine (`lectern.<tailnet>.ts.net`), separate from the host | The host's name is the URL (`https://nas.<tailnet>.ts.net`); the host may already be on your tailnet for other reasons |
| Who Lectern sees | Exact: `127.0.0.1` for `tailscale serve`, the device's own tailnet address for e-readers | Docker's loopback publishing hides addresses behind the Docker gateway; e-readers through a TCP forwarder all look alike (details [below](#alternative-host-install)) |
| Needs | `/dev/net/tun` on the host (standard on Linux, including Raspberry Pi OS, and in Docker Desktop's VM) | Root on the host |

The sidecar is more self-contained and gives Lectern the accurate client addresses its rate
limits and audit log are built around. Pick the host install if the machine already runs
Tailscale and you would rather not add a second machine to your tailnet.

## 1. Prepare your tailnet (once)

In the admin console, <https://login.tailscale.com/admin>:

1. **DNS** page: make sure **MagicDNS** is on, then under **HTTPS Certificates** click
   **Enable HTTPS**. `tailscale serve` cannot issue a certificate without this. Note the
   **tailnet name** shown on the same page (something like `tail1234.ts.net` or
   `blue-fox.ts.net`): your Lectern URL will be `https://lectern.<that name>`.
   Tailscale warns that machine names become public in Certificate Transparency logs; the
   name `lectern` reveals nothing sensitive.
2. **Settings → Keys → Generate auth key**. Leave *Reusable* off, leave *Ephemeral* off; if
   you have device approval turned on, tick *Pre-approved*. Copy the key (`tskey-auth-…`).
   It is used once, at the first start, and then stored state takes over.

## 2. Get the code and fill in `.env`

There is no published image yet; Compose builds it on your machine. Get the code (if the
repository is private, clone it with a [read-only deploy key](#cloning-a-private-repository-with-a-deploy-key)
instead):

```sh
git clone https://github.com/jeninh/lectern.git ~/lectern
cd ~/lectern
mkdir -p library/_inbox data/lectern data/tailscale
```

The `mkdir` matters: Lectern runs as your user ID (1000 by default) and must own
`library/` and `data/lectern/`. Compose refuses to start rather than letting Docker create
them owned by root. All three folders are git-ignored, so `git pull` never touches them.

Copy the example settings and fill them in:

```sh
cp .env.example .env
chmod 600 .env        # it holds the auth key
nano .env
```

Set the two required values (the file explains the optional ones):

```sh
LECTERN_BASE_URL=https://lectern.tail1234.ts.net   # your tailnet name from step 1
TS_AUTHKEY=tskey-auth-paste-it-here
```

`LECTERN_BASE_URL` must be **exactly** the address you will type in the browser, HTTPS and
all. Passkeys are bound to it, and Lectern's CSRF check compares the browser's `Origin`
against it. The first part, `lectern`, is the Tailscale machine name (`TS_HOSTNAME`); change
both together if you want another name. If `id -u` does not print `1000` (it does for the
first user on Raspberry Pi OS), set `LECTERN_UID` and `LECTERN_GID` to your `id -u` and
`id -g`.

**Edit only `.env`.** The two deployment files are tracked templates that you never edit,
so `git pull` never conflicts:

- [`deploy/tailscale/compose.yaml`](../deploy/tailscale/compose.yaml): the Tailscale
  sidecar and Lectern. Relative paths in it resolve against its own folder, so it finds the
  repository (build context), `library/`, `data/`, and `serve.json` wherever you cloned.
- [`deploy/tailscale/serve.json`](../deploy/tailscale/serve.json): `tailscale serve` as a
  file, HTTPS on 443 proxied to Lectern on loopback. `${TS_CERT_DOMAIN}` is filled in by the
  container with the machine's full name. There is no Funnel entry.

What the unusual parts of the Compose file do:

- `network_mode: service:tailscale` makes the Lectern container share the Tailscale
  container's network stack. Lectern's port 8080 is therefore *the tailnet machine's* port
  8080, and `tailscale serve`'s `http://127.0.0.1:8080` is Lectern. Nothing has `ports:`, so
  nothing is published to the host or the LAN.
- `TS_USERSPACE: "false"` runs Tailscale with a real network device (`/dev/net/tun`, hence
  `cap_add: NET_ADMIN`). Lectern then sees each device's actual tailnet address when it
  connects directly (e-readers), and sees `127.0.0.1` only for `tailscale serve`, which is why
  `LECTERN_TRUSTED_PROXIES=127.0.0.1` is exact (and fixed in the file, not in `.env`).
- Lectern waits until the sidecar's health check reports a tailnet address, and is
  restarted with it. The health endpoint listens on loopback only.
- The Tailscale image is pinned to a release and digest; upgrades arrive with `git pull`.

**Every command runs from the repository root with the same prefix:**

```sh
docker compose --env-file .env -f deploy/tailscale/compose.yaml <command>
```

Why both flags: with `-f deploy/tailscale/compose.yaml`, Compose treats `deploy/tailscale/`
as the project folder and would look for `.env` *there*; `--env-file .env` points it at the
root `.env` explicitly. The alternative, `COMPOSE_FILE=deploy/tailscale/compose.yaml` inside
`.env`, mixes Compose's own settings into the file that holds your secrets, and which `.env`
gets read then depends on your working directory and Compose version. The explicit command
behaves the same everywhere. If typing it gets old, add an alias to `~/.bashrc`; with
absolute paths it works from any folder:

```sh
alias lectern-compose='docker compose --env-file ~/lectern/.env -f ~/lectern/deploy/tailscale/compose.yaml'
```

(The rest of this guide spells the command out.)

## 3. Start it and create the owner account

```sh
cd ~/lectern
docker compose --env-file .env -f deploy/tailscale/compose.yaml up -d --build
docker compose --env-file .env -f deploy/tailscale/compose.yaml logs -f tailscale
```

The first build takes a few minutes on a laptop and roughly 20–40 minutes on a Raspberry
Pi 4 (it builds the web app and installs Python packages). In the Tailscale log, wait for a
line saying the serve config was applied (if it instead says it *cannot issue TLS certs*,
HTTPS is not enabled for your tailnet; go back to step 1). The machine `lectern` now appears
on your **Machines** page with its `100.x.y.z` address. Lectern starts once the sidecar is
healthy; `ps` shows both as `healthy` after a minute:

```sh
docker compose --env-file .env -f deploy/tailscale/compose.yaml ps
docker compose --env-file .env -f deploy/tailscale/compose.yaml logs lectern | grep -A3 "FIRST-RUN SETUP"
```

The log shows a link like `https://lectern.tail1234.ts.net/setup?token=…`. Open it, within 30
minutes, from a device that is on your tailnet (the computer you are typing on is fine if it
runs Tailscale; otherwise your phone, see next step) and create the owner account.
Registration closes permanently after that. If the token expired, restart Lectern
(`… restart lectern`) for a fresh one; that only works while setup is incomplete. Passkeys
work immediately because the address is HTTPS and `LECTERN_BASE_URL` matches it exactly.

**Do this now:** on the Machines page, open the `⋯` menu of `lectern` and choose
**Disable key expiry**. Otherwise the node's key expires after 180 days (the default) and the
server silently drops off your tailnet until you re-authenticate it. The auth key in `.env`
is no longer needed; you may delete its value.

## 4. Phones and laptops

Install the Tailscale app on each device (App Store, Play Store, <https://tailscale.com/download>),
sign in with the **same account**, and turn it on. Open `https://lectern.tail1234.ts.net` in
the browser. That is all; there is nothing to configure per device. With Tailscale off, the
address simply does not resolve.

On a phone, install it as an app and download books to read and listen without a connection
(or with Tailscale off): Safari **Share › Add to Home Screen** on iPhone, Chrome **Install app**
on Android. Sign in again inside the installed app; passkeys work there. See
[`offline.md`](offline.md) for downloads, offline sync, and what iOS may delete.

## 5. Your household

Each person needs two things: a way onto the tailnet, and a Lectern account.

1. **Tailnet access.** The simplest way: do not share your Tailscale login. Instead, on the
   Machines page open `⋯` next to `lectern` → **Share…** and send the link (or enter their
   e-mail). They create their own free Tailscale account, accept, and `lectern` appears in
   *their* Tailscale app as a shared machine. They reach it at the **full** name,
   `https://lectern.tail1234.ts.net` (short names like `https://lectern` only work inside
   your own tailnet). Shared machines are one-way: they can reach Lectern, Lectern cannot
   reach their devices, and they see nothing else of yours. Share invites expire after 30 days
   if not accepted.
2. **Lectern account.** As owner, open **Admin → Household invites → New invite**, choose
   *member* (whole library, can add books) or *reader* (only shelves you share), and send them
   the one-use link privately; it expires (3 days by default). They pick their own username and
   password at `https://lectern.tail1234.ts.net/invite/…`, which only works on the tailnet like
   everything else. For a reader, share shelves from **Library → Shelves**. They then set up a
   passkey or TOTP and add their e-reader under **Devices**. Lectern's sign-in is required on
   the tailnet too, and you get a notification when someone signs in from a new browser.

## Kindle and Kobo with KOReader

E-readers cannot run the Tailscale app. Two community projects run Tailscale *inside* the
device. Everything in this section was checked against the projects' and KOReader's source
code on 2026‑10‑20 (versions in [Sources](#sources)); the device-specific notes come from the
projects' own documentation, not from testing by us.

**Recommended: the KOReader Tailscale plugin** ([victoria-riley-barnett/koreader-tailscale](https://github.com/victoria-riley-barnett/koreader-tailscale),
v1.3.0). It is a KOReader plugin, so it works wherever KOReader does: Kindle (jailbroken),
Kobo, and PocketBook, ARMv7 or ARM64. The author lists Kindle Paperwhite 5 and 6, Kobo, and
PocketBook as tested; the plugin issue tracker has reports of trouble on an 11th‑generation
Kindle.

1. Copy `tailscale.koplugin` into KOReader's plugin folder: Kindle `/mnt/us/koreader/plugins/`,
   Kobo `/mnt/onboard/.adds/koreader/plugins/`, PocketBook `/mnt/ext1/koreader/plugins/`.
   Restart KOReader.
2. **Settings (gear) → Network → Tailscale VPN → Install/Update Tailscale.** It downloads
   about 57 MB over Wi‑Fi; do not close KOReader meanwhile.
3. Sign in: **Network setup → Scan QR code** with your phone, *or* put an auth key alone in
   `bin/auth.key` inside the plugin folder (same folders as above, PocketBook uses
   `/mnt/ext1/tailscale/bin/auth.key`) and tap **Check again**. Use a fresh key from
   **Settings → Keys**; a one-off key is fine.
4. On the **Machines** page, **disable key expiry** for the e-reader too. Otherwise it stops
   syncing after 180 days and you have to sign it in again; a fresh `auth.key` or the QR code
   does that.
5. Note the Lectern server's address from your Machines page, say `100.101.102.103`.

The easiest way to set up KOReader is the **Lectern plugin**: download a per-device zip
under **Devices**, copy it to `koreader/plugins`, and it configures progress sync and the
catalogue by itself, with automatic sync ([docs/koreader-plugin.md](koreader-plugin.md)). To
set it up by hand instead, in KOReader use **`http://100.101.102.103:8080`**, not the `https://lectern…ts.net`
name, for both:

- **Progress sync:** Settings → Progress sync → Custom sync server `http://100.101.102.103:8080/`,
  then **Login** with your username and the device's *sync key* from Lectern's **Devices**
  page.
- **OPDS catalogue:** `http://100.101.102.103:8080/opds` with your username and the device's
  *app password* (shown together with the sync key).

Why the plain `http://` address by IP, and why it is still private:

- The plugin brings Tailscale up with MagicDNS **off** (`--accept-dns=false`), so
  `lectern.tail1234.ts.net` does not resolve on the e-reader. Its README says the same: give
  other plugins the Tailscale IP.
- KOReader's HTTPS library (LuaSec 1.3.2) refuses to use an HTTP proxy for `https://` URLs
  ("proxy not supported"), and `tailscale serve`'s certificate is for the name, not the IP.
  Plain HTTP to port 8080 avoids both problems.
- The traffic is still encrypted end to end **by Tailscale** (WireGuard) between the e-reader
  and the Tailscale container, where Lectern lives. No one on your LAN or the Internet sees
  it. There is no TLS layer on top, which is why KOReader credentials are per-device keys you
  can revoke individually, never your password. Lectern's HTML, cookies, and passkeys are not
  involved on this path; OPDS and sync use the app password and sync key only.

**Kernel TUN vs. userspace mode.** The plugin picks kernel TUN when the device has
`/dev/net/tun` and falls back to userspace networking otherwise (**Plugin config → Force
userspace mode** forces the fallback). This matters more than it sounds:

- With **kernel TUN**, every program on the device can reach the tailnet. OPDS and progress
  sync both work with the settings above and no proxy. Leave *Automatically configure HTTP
  proxy* off.
- In **userspace mode**, nothing reaches the tailnet except through the proxies the plugin
  runs on the device: SOCKS5 on `127.0.0.1:1055` and an HTTP proxy on `127.0.0.1:1056`.
  KOReader has only an *HTTP proxy* setting (Settings → Network → **HTTP proxy**, long-press
  to edit; the plugin's *Automatically configure HTTP proxy* sets it to
  `http://127.0.0.1:1056` for you). What that setting covers, from KOReader's code:
  - **OPDS over `http://` works.** The OPDS browser uses LuaSocket's `socket.http`, which
    sends the request through the configured proxy, and tailscaled's HTTP proxy accepts it.
  - **OPDS over `https://` fails** ("proxy not supported", see above). Another reason to use
    the `http://…:8080` address.
  - **Progress sync does not go through the proxy.** KOReader's sync plugin sends its
    progress requests with a different HTTP client (Turbo, `frontend/httpclient.lua`) that
    does not read the proxy setting. We found no supported way to make it work in userspace
    mode, so if your device only runs in userspace mode, expect OPDS downloads to work and
    sync not to. The plugin README's statement that TUN mode gives "transparent routing for
    OPDS, progress sync" and that userspace mode needs the proxy is consistent with this.
  - The SOCKS5 proxy on 1055 is for other software on the device; KOReader does not use it.

Other things the plugin's documentation says to expect: Tailscale starts with KOReader and
remembers whether it was connected; it stops while the device is plugged in as USB storage;
it needs the device's CA bundle (KOReader ships one) only for its own TLS, not for your plain
HTTP connection to Lectern; logs are `bin/tailscale.log` and `bin/tailscaled.log` in the
plugin folder; and the Wi‑Fi radio sleeps when the screen is off, so a sync happens when you
open or close a book, not in the background.

**Kindle alternative: tailscale_kual** ([mitanshu7/tailscale_kual](https://github.com/mitanshu7/tailscale_kual)),
the project the KOReader plugin is based on. It is a KUAL extension for jailbroken Kindles
and runs Tailscale outside KOReader, so it also covers SSH into the Kindle. Its README (read
2026‑10‑20) documents three modes in a KUAL submenu: *Standard (userspace)*, *Proxy Mode*
(userspace plus a combined SOCKS5/HTTP proxy on `localhost:1055`, changeable in
`bin/proxy.address`), and *Kernel TUN* ("not available on all Kindle firmware versions"). In
Proxy Mode, set KOReader's HTTP proxy to `http://localhost:1055`; the same KOReader limits as
above apply (OPDS over `http://` yes, progress sync no), and Kernel TUN mode is what makes
sync work. Two things to know: its default `up.args` is `--ssh`, which turns on Tailscale
SSH *into the Kindle* for anyone your tailnet policy allows (edit `bin/up.args` if you do not
want that), and the README lists the Kindle Paperwhite 1, Kindle Keyboard, and Kindle 4 as
unsupported (kernel too old for Go). It also recommends disabling key expiry for the Kindle.

**Kobo without a jailbreak:** Kobo devices do not need jailbreaking for KOReader, and the
KOReader plugin above is the route; tailscale_kual is Kindle-only.

## Adding books, especially big ones

- Small EPUBs: use **Upload** in the web app as usual, from any device on the tailnet.
- Audiobooks upload from the web app too. Uploads are resumable: files go up in ≤ 50 MiB
  chunks, so a dropped connection on a home uplink resumes where it stopped instead of
  starting over. For anything really large, on the LAN, **copying into the inbox folder on the
  host is still fastest**: `~/lectern/library/_inbox/`. Lectern notices new files within a few seconds. On
  your LAN this is a plain file copy (Finder, Explorer, `scp`, Syncthing, a Samba share) and
  faster than any upload.
- Finished books are stored under `~/lectern/library/<Author>/<Title>/`.
- Listening away from home goes over your home upload bandwidth through Tailscale. Turn on
  **Data saver** in the player: Lectern makes a ~64 kb/s copy once (under
  `derived/audio/`, originals untouched) and streams that instead.

## Raspberry Pi 4

What we recommend, and what we have and have not measured:

- **OS:** Raspberry Pi OS Lite (64-bit), the Debian 13 "trixie" release, written with
  Raspberry Pi Imager (set a user, enable SSH with your public key). 64-bit matters: the
  image is built for `linux/arm64`, and CI builds and starts that image (emulated) on every
  pull request. The 32-bit OS is not supported.
- **Docker:** install Docker Engine with the Compose and Buildx plugins from Docker's own
  apt repository by following Docker's [Debian instructions](https://docs.docker.com/engine/install/debian/)
  (they apply to 64-bit Raspberry Pi OS), then `sudo usermod -aG docker $USER` and log in
  again. `ls -l /dev/net/tun` should show the device; it is part of the stock kernel.
- **Memory:** an 8 GB Pi 4 has plenty of room for this. Without transcription, Lectern and
  Tailscale should need a few hundred megabytes; the first build briefly needs more (the
  web app build and package installs). We have not measured either on a Pi, and we have
  not tried Pis with less memory.
- **First build:** roughly 20–40 minutes; later rebuilds reuse Docker's cache and take a
  few minutes unless dependencies changed. These are estimates; we have not timed a Pi.
- **Storage:** put the library on a USB 3 SSD rather than the SD card if you can (an SSD is
  faster and does not wear out from SQLite writes; a Pi 4 can also boot from it). Plan for
  about 5 GB of Docker images and build cache (`docker builder prune` reclaims cache), plus
  your library: EPUBs are a few MB each, audiobooks hundreds of MB to over 1 GB each, plus
  a ~64 kb/s data-saver copy per audiobook you listen to that way. To keep the checkout on
  the SD card and the library on the SSD, set `LECTERN_LIBRARY_PATH` (and optionally
  `LECTERN_DATA_PATH`) in `.env` to absolute paths on the SSD, created and owned by your
  user. Never keep backups on the SD card: see [SD-card wear](backups.md#sd-card-wear).
- **Image:** keep the default image (`LECTERN_EXTRAS` empty). Do **not** build the `align`
  image on a Pi.

### Aligning audiobooks from a Mac or another computer

A Pi is too small to transcribe audiobooks well, so the default image does not try: new
alignment jobs wait in a queue until an external worker takes them. Run the worker on a Mac
(Apple Silicon or Intel) or any Linux/Windows computer with Python 3.12+ that is on your
tailnet. It connects out to Lectern's HTTPS address with an owner-issued worker key, so it
needs nothing opened anywhere:

1. As the owner, create a worker credential (see
   [External workers](alignment.md#external-workers); the `lw_…` secret is shown once).
2. On the Mac, install the worker from your clone of the repository and point it at the Pi:

   ```sh
   python3 -m venv ~/.lectern-worker
   ~/.lectern-worker/bin/pip install "$HOME/src/lectern[align]"   # path to your clone
   printf '%s\n' 'lw_paste-it-here' > ~/.lectern-worker/key && chmod 600 ~/.lectern-worker/key
   ~/.lectern-worker/bin/lectern worker --server https://lectern.tail1234.ts.net \
     --key-file ~/.lectern-worker/key --name mac
   ```

The worker downloads only the inputs of the job it holds, transcribes locally, uploads the
result, and loops; `Ctrl-C` hands the current job back. Models download on first use. Leave
it running while there is a backlog; nothing breaks while it is off, jobs just wait.

## Cloning a private repository with a deploy key

If the repository is private, give the Pi a **read-only deploy key**: an SSH key that can
read this one repository and nothing else, cannot push, and is not tied to your GitHub
account. Never put a token or password in the clone URL or in `.git/config`.

On the Pi:

```sh
install -d -m 700 ~/.ssh
ssh-keygen -t ed25519 -f ~/.ssh/lectern_deploy -C "lectern deploy key ($(hostname))"
chmod 600 ~/.ssh/lectern_deploy
chmod 644 ~/.ssh/lectern_deploy.pub
cat ~/.ssh/lectern_deploy.pub
```

A passphrase is optional: without one, `git pull` works unattended, and the key can only
read this repository; with one, you type it on each pull.

On GitHub (repository admin): the repository's **Settings → Deploy keys → Add deploy key**.
Paste the `.pub` line, name it after the Pi, and leave **Allow write access unticked**.

Back on the Pi, add a host alias so this key is used only for this repository. Append to
`~/.ssh/config` (create it with `chmod 600` if it does not exist):

```text
Host github-lectern
    HostName github.com
    User git
    IdentityFile ~/.ssh/lectern_deploy
    IdentitiesOnly yes
```

Verify GitHub's host key before trusting it. Fetch it, then compare the fingerprint with the
one GitHub publishes at
<https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints>
(for Ed25519, `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU` when this was written):

```sh
ssh-keyscan -t ed25519 github.com > /tmp/github.key
ssh-keygen -lf /tmp/github.key          # must print the published fingerprint
cat /tmp/github.key >> ~/.ssh/known_hosts && rm /tmp/github.key
ssh -T github-lectern                   # "Hi jeninh/lectern! You've successfully authenticated…"
```

Only append the key if the fingerprints match. Then clone through the alias and continue
with step 2 (`mkdir`, `.env`):

```sh
git clone git@github-lectern:jeninh/lectern.git ~/lectern
```

`git pull` in `~/lectern` then uses the deploy key automatically. To revoke the Pi's access,
delete the key under **Deploy keys**.

## Upgrading

```sh
cd ~/lectern
git pull --ff-only
docker compose --env-file .env -f deploy/tailscale/compose.yaml up -d --build
docker image prune -f        # optional: drop the previous image
```

`git pull` only changes tracked files; your `.env`, `data/`, and `library/` stay as they are,
so there is nothing to merge. Upgrades to the Tailscale image arrive the same way (it is
pinned in `deploy/tailscale/compose.yaml`). Lectern updates its database schema on start, so
[back up](#backups-and-restore) first. If `git pull --ff-only` refuses because a tracked file
was changed locally, `git status` shows which; move your change into `.env` and
`git restore <file>`.

## Logs and status

```sh
docker compose --env-file .env -f deploy/tailscale/compose.yaml ps
docker compose --env-file .env -f deploy/tailscale/compose.yaml logs -f lectern
docker compose --env-file .env -f deploy/tailscale/compose.yaml logs --since 1h tailscale
docker compose --env-file .env -f deploy/tailscale/compose.yaml exec tailscale tailscale status
docker compose --env-file .env -f deploy/tailscale/compose.yaml exec tailscale tailscale serve status
```

Lectern's own record of sign-ins and admin actions is the **Activity** page in the web app.

## Backups and restore

Lectern has built-in nightly backups, **off until you turn them on** in **Admin → Nightly
backups**: a consistent copy of the database while it runs (SQLite online backup), the
signing key, settings, sentence maps, every book's metadata and cover, and optionally the
library files, into an encrypted restic repository, with retention and failure alerts.
Point `LECTERN_BACKUP_PATH` in `.env` at a USB drive, not the SD card. Setup, SD-card wear,
and the tested `lectern restore` command are in [Backups and restore](backups.md).

Four things hold state:

| What | Where | In Lectern's backup |
| --- | --- | --- |
| Library | `~/lectern/library` (or `LECTERN_LIBRARY_PATH`) | metadata and covers always; files if you include them |
| Lectern database and signing key | `~/lectern/data/lectern` (or `LECTERN_DATA_PATH`) | yes |
| Settings | `~/lectern/.env` | the `LECTERN_*` values only; keep `.env` in your password manager |
| Tailscale node state | `~/lectern/data/tailscale` (or `TS_STATE_PATH`), owned by root | no; it holds the node's private key |

The Tailscale state makes a restore seamless (same machine name and address). Back it up
yourself, and protect the archive:

```sh
sudo tar czf "/media/usb/lectern-backups/tailscale-state-$(date +%F).tgz" -C ~/lectern/data/tailscale .
```

**Restore** onto a fresh clone (or a new Pi) with `.env` copied back: follow
[Restore](backups.md#restore), then, with the Tailscale backup:

```sh
sudo install -d -m 700 data/tailscale
sudo tar xzf /path/to/tailscale-state-2026-10-20.tgz -C data/tailscale
docker compose --env-file .env -f deploy/tailscale/compose.yaml up -d --build
```

`data/lectern` and `library` must belong to `LECTERN_UID:LECTERN_GID`
(`sudo chown -R 1000:1000 data/lectern library` if you restored as another user). Without
the Tailscale backup, first **remove the old `lectern` machine** on the Machines page, then
put a fresh auth key in `.env`: otherwise the new node is named `lectern-1`, the address
changes, and passkeys (bound to the old name) stop working.

## Troubleshooting

- **`required variable LECTERN_BASE_URL is missing a value`**: you ran Compose without
  `--env-file .env`, from another folder, or `.env` lacks the line. Run from `~/lectern`
  with the full command.
- **`bind source path does not exist`**: create the folders as your user,
  `mkdir -p library/_inbox data/lectern` (or the paths set in `.env`).
- **Lectern restarts with `Permission denied` or `unable to open database file`**:
  `data/lectern` or `library` is not owned by `LECTERN_UID:LECTERN_GID`;
  `sudo chown -R "$(id -u):$(id -g)" data/lectern library` and match `.env`.
- **Lectern stays `Created`; `dependency failed to start: container … is unhealthy`**: the
  sidecar never got onto the tailnet. Read `logs tailscale`: an invalid or already-used
  auth key (make a new one, and delete `data/tailscale/*` only if the node never joined),
  device approval pending in the admin console, or no Internet.
- **`error gathering device information while adding custom device "/dev/net/tun"`**: the
  host has no TUN device. On Linux, `sudo modprobe tun` and add `tun` to
  `/etc/modules-load.d/tun.conf`. Raspberry Pi OS and Docker Desktop have it. If your host
  cannot provide it, use the [host install](#alternative-host-install) instead.
- **Serve log says it cannot issue TLS certificates**: enable HTTPS for the tailnet (step 1),
  then `… restart tailscale`.
- **The site loads but passkey creation or sign-in fails, or forms say the origin is not
  allowed**: `LECTERN_BASE_URL` does not match the address in the browser exactly (name,
  tailnet, `https://`, no trailing path). Fix `.env`, then `… up -d`.
- **`https://lectern…ts.net` does not resolve**: the device is not on the tailnet, MagicDNS
  is off, or the machine got another name (`lectern-1`; see restore above).
- **First build fails on a Pi with `no space left on device` or the Pi becomes unresponsive**:
  free disk (`docker system df`, `docker builder prune`) or add swap, then rerun `up -d --build`.
- **Setup link expired**: `… restart lectern` and read the new link from the log; this only
  works while no owner exists.

## Moving from the old instructions

Earlier versions of this guide had you **replace** the root `compose.yaml` and store state in
the Docker volumes `lectern_lectern-data` and `lectern_tailscale-state`. To switch without
losing anything:

```sh
cd ~/lectern
docker compose down                      # the old stack, using your edited compose.yaml
mkdir -p data/lectern data/tailscale library/_inbox
docker run --rm -v lectern_lectern-data:/from -v "$PWD/data/lectern":/to alpine cp -a /from/. /to/
docker run --rm -v lectern_tailscale-state:/from -v "$PWD/data/tailscale":/to alpine cp -a /from/. /to/
sudo chown -R "$(id -u):$(id -g)" data/lectern
git restore compose.yaml && rm -f tailscale/serve.json && rmdir tailscale
git pull --ff-only
docker compose --env-file .env -f deploy/tailscale/compose.yaml up -d --build
```

Check the volume names with `docker volume ls` first (they start with your checkout folder's
name). The node keeps its name and address because its state moved with it. Once everything
works, `docker volume rm lectern_lectern-data lectern_tailscale-state`.

## Security caveats, in plain words

- **Lectern's sign-in is not optional and the tailnet is not a login.** Tailscale decides
  which devices can connect; Lectern still checks who you are, every time, and rate-limits
  and logs failures. `tailscale serve` adds `Tailscale-User-Login` headers naming the
  tailnet user; Lectern ignores them, by design. A borrowed or stolen tailnet device gets as
  far as the login page.
- **Nothing is public.** The Compose file publishes no ports and uses `tailscale serve`, which
  is tailnet-only. Do **not** use `tailscale funnel`, which would put Lectern on the public
  Internet with no Tailscale login in front; nothing in this guide needs it.
- **Your tailnet's access rules apply.** Tailscale's default policy lets every device on a
  tailnet reach every other one, which is right for a household. You can tighten it in
  **Access controls**, for example so e-readers reach only `lectern` on port 8080.
- **The auth key is a secret** until used; it is in `.env` only (git-ignored, `chmod 600`).
  `data/tailscale` holds the node's private key; treat the Docker host as you would any
  server, and its backups too.
- **`LECTERN_TRUSTED_PROXIES` must stay `127.0.0.1`.** Anything listed there can tell Lectern
  "this request came from address X", which affects rate limiting and the audit log (never
  authentication). The Compose file fixes the value and runs Tailscale with kernel TUN, so
  only `tailscale serve` connects from loopback. In Tailscale's userspace mode (which this
  setup does not use), its network stack delivers *every* tailnet connection, including
  e-readers, from `127.0.0.1`; a device on your tailnet could then send its own
  `X-Forwarded-For` to pick a rate-limit bucket or audit address, and devices without the
  header all share one. That is why the template needs `/dev/net/tun` rather than falling
  back silently.
- **E-reader traffic is `http://` inside the tailnet.** It is encrypted by WireGuard between
  the device and your Tailscale container, but there is no TLS and the device checks no
  certificate. Use the per-device sync key and app password, never your password, and revoke
  a lost device from **Devices**.
- **Passkeys are bound to the hostname.** Renaming the machine (or your tailnet) later means
  registering passkeys again. Password + TOTP keep working.
- **Keep things updated**: see [Upgrading](#upgrading). The Tailscale image is pinned, so
  it changes only when the repository updates the pin.
- If you need a public hostname rather than a private network, Cloudflare Tunnel also works
  (set `LECTERN_TRUSTED_PROXIES` to the `cloudflared` container's address *only* and
  explicitly set `LECTERN_TRUST_CF_CONNECTING_IP=1` so Lectern reads `CF-Connecting-IP` from
  it; the flag is off by default and must stay off with `tailscale serve`), at the price of
  Cloudflare terminating TLS and seeing your traffic, and of KOReader's routes having to
  bypass any Cloudflare Access login.

## Alternative: host install

If Tailscale already runs on the machine (a NAS, a home server), you can skip the sidecar:

1. Install Tailscale on the host (`curl -fsSL https://tailscale.com/install.sh | sh`, then
   `sudo tailscale up`), and enable HTTPS in the admin console as in step 1.
2. Keep the repository's own `compose.yaml` (it publishes `127.0.0.1:8080` to the host only).
   In `.env` set `LECTERN_BASE_URL=https://<host-name>.<tailnet>.ts.net`.
3. `sudo tailscale serve --bg 8080` gives the browser URL. For e-readers,
   `sudo tailscale serve --bg --tcp=8080 tcp://127.0.0.1:8080` forwards raw TCP on the host's
   tailnet address, port 8080, to Lectern; KOReader then uses `http://<host 100.x.y.z>:8080`
   as above. `sudo tailscale serve status` shows both.
4. **Client addresses are less exact here.** Docker's loopback publishing goes through its
   userland proxy, so Lectern sees connections from the Docker network's gateway (for the
   stock file, the `lectern_default` network, e.g. `172.18.0.1`) rather than `127.0.0.1`.
   Find the address Lectern logs for your own login on the **Activity** page and set
   `LECTERN_TRUSTED_PROXIES` to it so the browser path gets real addresses from
   `X-Forwarded-For`. The raw TCP forwarder adds no header, so all e-readers share that one
   address for rate limiting, and the userspace caveat above applies to them too.

Everything else (owner setup, sharing, key expiry, KOReader, backups) is the same.

## Sources

Checked 2026‑10‑20.

- Tailscale: [Tailscale Serve](https://tailscale.com/kb/1312/serve) and the
  [`tailscale serve` CLI reference](https://tailscale.com/docs/reference/tailscale-cli/serve)
  (validated 2026‑01‑26; `--bg`, `--tcp`, HTTPS certificate requirement, tailnet-only);
  [Enabling HTTPS](https://tailscale.com/kb/1153/enabling-https);
  [Key expiry](https://tailscale.com/kb/1028/key-expiry) (180-day default, *Disable key
  expiry*, validated 2026‑01‑05); [Sharing](https://tailscale.com/kb/1084/sharing) (full
  name required, one-way, 30-day invites);
  [Userspace networking mode](https://tailscale.com/kb/1112/userspace-networking) (validated
  2025‑11‑12); [Docker](https://tailscale.com/kb/1282/docker) and the `TS_*` variables
  documented in `cmd/containerboot/main.go`.
- Tailscale source, [`tailscale/tailscale`](https://github.com/tailscale/tailscale) at
  `db03d4e`: `ipn/ipnlocal/serve.go` (`addProxyForwardedHeaders` *sets* `X-Forwarded-For`
  to the caller's tailnet address and `X-Forwarded-Proto: https`; the backend is dialled from
  the local system, hence peer `127.0.0.1`); `wgengine/netstack/netstack.go` (in userspace
  mode, inbound connections to the node's own address are dialled to `127.0.0.1` on the same
  port); `cmd/containerboot/serve.go` (`${TS_CERT_DOMAIN}` substitution; HTTPS serve config
  is held back with a log message until the tailnet can issue certificates);
  `cmd/tailscaled/proxy.go` (the outbound HTTP proxy accepts plain absolute-URI requests and
  `CONNECT`).
- KOReader, [`koreader/koreader`](https://github.com/koreader/koreader) master:
  `frontend/ui/network/manager.lua` `NetworkMgr:setHTTPProxy` sets only
  `socket.http.PROXY`; `plugins/opds.koplugin/opdsclient.lua` fetches with `socket.http`;
  `plugins/kosync.koplugin/KOSyncClient.lua` sends `update_progress`/`get_progress` through
  the `AsyncHTTP` middleware, which uses `frontend/httpclient.lua` (Turbo, no proxy
  support). LuaSec [v1.3.2 `src/https.lua`](https://github.com/lunarmodules/luasec/blob/v1.3.2/src/https.lua)
  returns `"proxy not supported"` when `http.PROXY` is set; KOReader pins that version in
  `koreader-base/thirdparty/luasec`.
- [`victoria-riley-barnett/koreader-tailscale`](https://github.com/victoria-riley-barnett/koreader-tailscale)
  at `114917c` (`_meta.lua` 1.3.0): README; `bin/start_tailscale.sh` (`--socks5-server=127.0.0.1:1055
  --outbound-http-proxy-listen=127.0.0.1:1056`); `main.lua` (`--tun=tailscale0` when
  `/dev/net/tun` exists, else `--tun=userspace-networking`; `tailscale up --accept-routes
  --accept-dns=false --netfilter-mode=off`; auto-proxy `http://127.0.0.1:1056`).
- [`mitanshu7/tailscale_kual`](https://github.com/mitanshu7/tailscale_kual) README (main).
- Tailscale blog, [*More Tailscale tricks for your jailbroken Kindle*](https://tailscale.com/blog/jailbroken-kindle-proxy-tun-modes)
  (2026‑06‑12): device coverage and the author's Kindle experience.
