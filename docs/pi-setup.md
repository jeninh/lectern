# Raspberry Pi setup, end to end

This is the whole path from a blank SD card to a working Lectern on a Raspberry Pi: the server,
updates, the Mac worker, a Kindle, phones, AI apps, backups, and what to do when something goes
wrong. Work through it in order. Each step is short and links to the page that explains it in
depth; when the two disagree, the linked page is the reference and this guide is the bug.

It replaces the old Pi setup PDF. If you want a copy to read offline, print this page from
GitHub; it changes with the code, so print it again after you update.

Placeholders used throughout: the Pi is `lectern-pi`, your user on it is `pi`, your tailnet is
`tail1234.ts.net`, and Lectern's address is `https://lectern.tail1234.ts.net`. Use your own.

```diagram
┌──────────────────────────────┐   ssh (LAN, or tailnet)   ┌──────────────────────────────────┐
│ Mac                          │ ────────────────────────▶ │ Raspberry Pi (lectern-pi)        │
│  your SSH key                │                           │  Docker Compose:                 │
│  Lectern worker (align, TTS) │ ──── HTTPS, tailnet ────▶ │   tailscale (machine "lectern")  │
└──────────────────────────────┘                           │   lectern :8080                  │
┌──────────────┐  https://lectern.<tailnet>.ts.net         │  ~/lectern/{library,data}        │
│ phone (PWA)  │ ────────────────────────────────────────▶ │  USB drive: backups              │
└──────────────┘                                           └──────────────────────────────────┘
┌──────────────┐  http://100.x.y.z:8080 (OPDS + sync)             ▲
│ Kindle       │ ─────────────────────────────────────────────────┘
└──────────────┘
```

## 1. Flash Raspberry Pi OS, SSH keys, hostname

**Use Raspberry Pi OS Lite (64-bit).** Lectern's image is built for `linux/arm64` (CI builds
and starts it on every pull request); the 32-bit OS is not supported. A Pi 4 or 5 is enough.

1. **An SSH key on your Mac**, if you do not have one yet (`ls ~/.ssh/id_ed25519.pub`):

   ```sh
   ssh-keygen -t ed25519 -C "mac"
   cat ~/.ssh/id_ed25519.pub        # the line Imager asks for
   ```

2. **Write the card** with [Raspberry Pi Imager](https://www.raspberrypi.com/software/):
   device → *Raspberry Pi OS (other)* → *Raspberry Pi OS Lite (64-bit)* → storage. In the
   customisation step:
   - **Hostname:** `lectern-pi`. Do **not** call the Pi itself `lectern`: that is the name of
     Lectern's own Tailscale machine (step 3), and if the Pi ever joins the tailnet under the
     same name, one of them becomes `lectern-1` and the address your passkeys are bound to moves.
   - **User:** a username and password. The password is for `sudo`; keep it in your password
     manager.
   - **Wi-Fi:** only if the Pi will not be on Ethernet (Ethernet is better for a server).
   - **SSH:** enable it with **public-key authentication only** and paste the `.pub` line.
3. **Network install gotchas** (booting the Pi straight into Imager with no other computer,
   Pi 4 and later):
   - It downloads over **Ethernet only**; Wi-Fi is not used. Plug in a cable, a keyboard, and a
     screen.
   - It needs a recent bootloader. An old Pi 4 may not offer it until its bootloader is updated
     (Imager: *Misc utility images → Bootloader*).
   - If the card or drive already holds a bootable system, the Pi boots that instead; hold
     **Shift** at power-on to get the network installer.
   - The customisation step is the same as on a computer; do not skip it, or you have no SSH
     access on first boot.
   Details: [Raspberry Pi documentation, *Install over the network*](https://www.raspberrypi.com/documentation/computers/getting-started.html).
4. **First boot.** Give it a few minutes, then from the Mac:

   ```sh
   ssh pi@lectern-pi.local
   sudo apt update && sudo apt full-upgrade -y && sudo reboot
   ```

   `.local` names come from mDNS on your LAN. If it does not resolve, use the Pi's address from
   your router. A `~/.ssh/config` entry on the Mac saves typing (`ssh lectern-pi`):

   ```text
   Host lectern-pi
       HostName lectern-pi.local
       User pi
   ```

   To rename the Pi later: `sudo raspi-config` → *System Options → Hostname* (it updates
   `/etc/hosts` too), then reboot.

If commands such as `nano` complain about `xterm-ghostty`, see
[Troubleshooting](#xterm-ghostty-unknown-terminal-type).

## 2. Docker, and cloning the private repository

**Docker Engine with the Compose and Buildx plugins**, from Docker's own apt repository:
follow Docker's [Debian instructions](https://docs.docker.com/engine/install/debian/) (they
apply to 64-bit Raspberry Pi OS; not the 32-bit Raspbian page). Then let your user run it and
check:

```sh
sudo usermod -aG docker "$USER"
exit                                     # log in again so the group applies
docker compose version && docker buildx version
docker run --rm hello-world
ls -l /dev/net/tun                       # the Tailscale sidecar needs it; stock kernel has it
```

**Clone with a read-only deploy key.** The repository is private. Give the Pi an SSH key that
can read this one repository and nothing else; never put a token or password in the clone URL.
The full walkthrough, including checking GitHub's host key fingerprint, is
[Cloning a private repository with a deploy key](deploy-tailscale.md#cloning-a-private-repository-with-a-deploy-key).
In short, on the Pi:

```sh
install -d -m 700 ~/.ssh
ssh-keygen -t ed25519 -f ~/.ssh/lectern_deploy -C "lectern deploy key ($(hostname))"
cat ~/.ssh/lectern_deploy.pub
```

On GitHub, the repository's **Settings → Deploy keys → Add deploy key**: paste it, name it after
the Pi, leave **Allow write access** unticked. Add the `github-lectern` host alias to
`~/.ssh/config` and verify GitHub's host key as the linked section shows, then:

```sh
ssh -T github-lectern                    # "Hi jeninh/lectern! You've successfully authenticated…"
git clone git@github-lectern:jeninh/lectern.git ~/lectern
```

## 3. Tailscale sidecar, `.env`, first run

The repository ships the deployment in [`deploy/tailscale/`](../deploy/tailscale/compose.yaml):
Lectern runs inside a Tailscale container's network, `tailscale serve` gives it HTTPS on your
tailnet, and no port is published to your LAN or the Internet. Why it is built this way, and
every option: [Running Lectern at home with Tailscale](deploy-tailscale.md).

1. **Prepare the tailnet** (once, in <https://login.tailscale.com/admin>): **DNS** → MagicDNS on,
   **Enable HTTPS**, note the tailnet name. **Settings → Keys → Generate auth key**: not
   reusable, not ephemeral, pre-approved if device approval is on. Copy the `tskey-auth-…` key.
2. **Folders.** Lectern runs as your user (1000:1000 for the first user on Raspberry Pi OS) and
   must own them; Compose refuses to start rather than let Docker create them as root:

   ```sh
   cd ~/lectern
   mkdir -p library/_inbox data/lectern data/tailscale
   ```

   To keep the library on a USB SSD instead of the SD card (recommended: faster, and SQLite
   writes wear cards out), create the folders there, owned by you, and set
   `LECTERN_LIBRARY_PATH` / `LECTERN_DATA_PATH` in `.env` to their **absolute** paths.
3. **`.env`**, the only file you edit (tracked files stay untouched, so `git pull` never
   conflicts):

   ```sh
   cp .env.example .env && chmod 600 .env
   nano .env
   ```

   ```sh
   LECTERN_BASE_URL=https://lectern.tail1234.ts.net   # exactly what you will type in the browser
   TS_AUTHKEY=tskey-auth-…                            # used once, at the first start
   ```

   `LECTERN_BASE_URL` must match the browser address exactly: passkeys and the CSRF check are
   bound to it. Its first label is `TS_HOSTNAME` (default `lectern`). If `id -u` is not `1000`,
   set `LECTERN_UID` / `LECTERN_GID`. The other values are explained in `.env.example`.
4. **The `lectern-compose` alias.** Every command needs both `--env-file` and `-f`; add this to
   `~/.bashrc` (and `source ~/.bashrc`) so it works from any folder:

   ```sh
   alias lectern-compose='docker compose --env-file ~/lectern/.env -f ~/lectern/deploy/tailscale/compose.yaml'
   ```

5. **First run.** The first build takes roughly 20–40 minutes on a Pi 4 (an estimate, not
   timed), so start it as in [step 4](#4-updating) with `nohup` and watch the log, or for now:

   ```sh
   LECTERN_COMMIT=$(git -C ~/lectern rev-parse HEAD) lectern-compose up -d --build
   lectern-compose logs -f tailscale      # wait for the serve config to be applied; Ctrl-C
   lectern-compose ps                     # both "healthy" after a minute
   lectern-compose logs lectern | grep -A3 "FIRST-RUN SETUP"
   ```

6. **Owner account and passkey.** Open the printed `https://lectern.tail1234.ts.net/setup?token=…`
   link within 30 minutes, from a device on your tailnet (install the Tailscale app on it and
   sign in with the same account). Create the owner account, then add a passkey under
   **Security**. Registration closes after this. Expired link: `lectern-compose restart lectern`
   prints a new one while no owner exists.
7. **Disable key expiry.** In the admin console's **Machines** page, `lectern` → `⋯` →
   **Disable key expiry**. Otherwise the server drops off your tailnet after 180 days. You may
   now delete the `TS_AUTHKEY` value from `.env`.

Household members, sharing the machine with their own Tailscale accounts, and invites:
[Your household](deploy-tailscale.md#5-your-household).

## 4. Updating

```sh
cd ~/lectern
git pull --ff-only
LECTERN_COMMIT=$(git rev-parse HEAD) \
  nohup docker compose --env-file .env -f deploy/tailscale/compose.yaml up -d --build \
  > ~/lectern-build.log 2>&1 &
tail -f ~/lectern-build.log              # Ctrl-C stops watching, not the build
```

- **`LECTERN_COMMIT`** records the commit in the image, so a Mac worker installed from the
  server's `/worker/install.sh` installs the same code as the server. Without it, workers get
  the newest code on the default branch. It comes from the shell for each build; leave it empty
  in `.env`.
- **`nohup … &`** keeps a long build running if your SSH session drops (a laptop lid, school
  Wi-Fi). Spell the full command out: `nohup` cannot run an alias, so
  `nohup lectern-compose …` fails with *No such file or directory*. (`tmux` works too.)
- **Back up first** when an update may change the database: Lectern migrates its schema on
  start ([Backups](#10-backups-and-restore), **Back up now**).
- `git pull --ff-only` refuses if a tracked file was edited: `git status` shows which; move the
  change into `.env` and `git restore <file>`.
- `docker image prune -f` drops the previous image; `docker builder prune` reclaims build
  cache when the SD card fills up.

**Logs and status:**

```sh
lectern-compose ps
lectern-compose logs -f lectern
lectern-compose logs --since 1h tailscale
lectern-compose exec tailscale tailscale status
lectern-compose exec tailscale tailscale serve status
```

Sign-ins and admin actions are on the **Activity** page in the web app.

## 5. Build extras and Piper voices

`LECTERN_EXTRAS` in `.env` picks what the image can do itself; rebuild after changing it
([step 4](#4-updating)).

| `LECTERN_EXTRAS` | Adds | On a Pi |
| --- | --- | --- |
| empty (default) | nothing: alignment and Kokoro wait for a worker | **use this**, with the Mac worker |
| `tts` | Piper, CPU text-to-speech for generated audiobooks | fine: lets the Pi narrate without the Mac |
| `align` | faster-whisper (~0.6 GB, ~1.4 GB RAM per job) | **no**; align on the Mac |

Only one extra can be built at a time today ([#30](https://github.com/jeninh/lectern/issues/30)).

**Piper voices** (with `LECTERN_EXTRAS=tts`): put `<voice>.onnx` and `<voice>.onnx.json` in
`data/lectern/tts/piper/`, or download them into the running container:

```sh
lectern-compose exec lectern python -m piper.download_voices \
    --download-dir /data/tts/piper en_US-lessac-medium
```

Voices are listed at <https://huggingface.co/rhasspy/piper-voices>; check each voice's licence
before sharing what you make. `low` voices are fastest, `medium` sound better. Engines,
Kokoro, ElevenLabs (bring your own key, **Admin → ElevenLabs**): [Generated audiobooks](tts.md).
Alignment: [Alignment](alignment.md#turning-it-on).

## 6. Mac worker

The Mac aligns audiobooks (MLX on the GPU) and narrates with Kokoro, then hands the results to
the Pi. It connects out to Lectern, so the Mac needs only Tailscale. Full reference:
[The Mac worker](mac-worker.md).

1. Install Tailscale on the Mac, signed in to your tailnet, and check that
   `https://lectern.tail1234.ts.net` opens.
2. **Read access to the private repository** on the Mac (the installer checks first). Prefer
   `gh auth login` then `gh auth setup-git`: the installer then installs exactly the server's
   commit (the `LECTERN_COMMIT` pin from [step 4](#4-updating)). `--source` replaces that pinned
   source, so with an SSH key add the commit yourself,
   `--source git+ssh://git@github.com/jeninh/lectern@<commit>` (`git -C ~/lectern rev-parse HEAD`
   on the Pi), or with `--source /path/to/your/clone` check that commit out in the clone first.
3. In Lectern as the owner: **Admin → Workers → Add a worker**, name it. Paste the command it
   shows into Terminal on the Mac:

   ```sh
   curl -fsSL https://lectern.tail1234.ts.net/worker/install.sh | LECTERN_WORKER_CODE=lwe_… sh
   ```

   The code is single use and expires in an hour. Leave out `LECTERN_WORKER_CODE=…` to be
   prompted for it instead, so it stays out of your shell history. The installer sets up the
   worker and a menu bar app (📗 ready, 📖 busy, 📙 paused, 📕 off). Run it again with a new code
   to upgrade after you update the Pi.

**School and proxy mode.** The worker's `proxy` setting (`--proxy` to the installer, or
`proxy` in `config.json`) applies to **every** request it makes, Lectern's included; the
default, `system`, uses `HTTPS_PROXY`, else the macOS proxy settings, and ignores their
exception list. A school
proxy cannot reach a tailnet address, so with Lectern only on the tailnet:

- If Tailscale connects on that network, set `"proxy": "none"` in `config.json` so the worker
  talks to Lectern directly over Tailscale. The agent reads it only at start: run
  `lectern worker-service install` afterwards to restart it (add `--no-login` if you installed
  with it), or pass `--proxy none` to the installer. Model downloads then go direct too, so **download the models at home
  first** (run one job there; they are cached in `~/.cache/huggingface`).
- If the network forces everything through its proxy and blocks Tailscale, the worker cannot
  reach Lectern from there. Nothing breaks: jobs wait until the Mac is home.
- `--ca-bundle /path/school-ca.pem` (or `ca_bundle`) matters only for a network that inspects
  HTTPS, for the model downloads. The worker never turns certificate checks off.

Use **Only on Wi-Fi** with `wifi_networks: ["Home"]` to run only at home. Details:
[School and proxy mode](mac-worker.md#school-and-proxy-mode). While the Mac is off or paused,
alignment and Kokoro narrations wait for it; what the Pi can run itself (data-saver
transcodes, Piper) runs on the Pi.

## 7. Kindle

Needs a **jailbroken Kindle with KUAL, MRPI, and KOReader**. How to jailbreak depends on the
model and firmware; follow the [Kindle Modding Wiki](https://kindlemodding.org/), including its
hotfix (so the jailbreak survives updates) and its advice to keep the Kindle in airplane mode
until updates are blocked. Lectern cannot help with this part.

1. **Tailscale on the Kindle: the KUAL extension in Kernel TUN mode**
   ([mitanshu7/tailscale_kual](https://github.com/mitanshu7/tailscale_kual)). Put a fresh
   one-off auth key alone in `tailscale/bin/auth.key`, remove `--ssh` from
   `tailscale/bin/up.args` unless you want Tailscale SSH *into* the Kindle, copy the
   `tailscale` folder into `extensions/` on the Kindle, then in KUAL: **Install / Update
   Binaries** → **Start Tailscaled → Kernel TUN** → **Start Tailscale**. Kernel TUN matters:
   in userspace or proxy mode KOReader's progress sync cannot reach the tailnet. Leave
   KOReader's HTTP proxy empty. Then **Disable key expiry** for the Kindle on the Machines page.
   Steps and caveats: [Tailscale on the e-reader](koreader-plugin.md#tailscale-on-the-e-reader).
2. **The Lectern plugin.** In Lectern: **Devices → Add device**, name it, **Create
   credentials**, check the **server address for the e-reader** (`http://100.x.y.z:8080`, the
   `lectern` machine's tailnet IP; the `.ts.net` name does not work on the Kindle), and
   **Download lectern.koplugin.zip**. Unzip it and copy the `lectern.koplugin` folder into
   `koreader/plugins/` on the Kindle's USB drive. Eject and start KOReader with Wi-Fi and
   Tailscale on: it says *Lectern is set up*.
3. **Auto sync** is on from then on: positions are pulled when you open a book, pushed when you
   close it, on sleep, and every 10 page turns, and queued while Wi-Fi is off. Settings, the
   catalogue, and pairing with a code after a reset: **Tools → Lectern** and
   [The Lectern plugin for KOReader](koreader-plugin.md).

Kobo and PocketBook use the KOReader Tailscale plugin instead:
[Kindle and Kobo with KOReader](deploy-tailscale.md#kindle-and-kobo-with-koreader).

## 8. Phone app and offline downloads

1. Install the Tailscale app, sign in to the same tailnet, turn it on.
2. Open `https://lectern.tail1234.ts.net` in **Safari** (iPhone) or **Chrome** (Android).
3. Install it: Safari **Share → Add to Home Screen**; Chrome **Install app**.
4. **Sign in again inside the installed app** (iOS keeps its storage separate from Safari's);
   passkeys work there.
5. Open a book and tap **Download**; downloaded books read, play, and read along offline, and
   positions sync when Lectern is reachable again. **Downloads** lists them.

What iOS may delete, updates, and sign-out: [Lectern on your phone](offline.md).

## 9. AI apps (MCP) and AI keys

AI apps on a computer in your tailnet (Claude Desktop, Claude Code) can add, search, and file
books through `https://lectern.tail1234.ts.net/mcp`. **Devices → AI tools → Connect an AI app**:
choose the app, name the key, pick its scopes, and copy the configuration it shows. The
`lak_…` key is shown once; treat it like a password and revoke it from the same page. Adding
scopes later asks for your password or passkey again. Do not use Tailscale Funnel to reach it
from the Internet. Scopes and tools: [AI access (MCP)](mcp.md).

## 10. Backups and restore

Lectern backs itself up nightly with restic, **off until you turn it on**. Full reference,
including what is in a backup: [Backups and restore](backups.md).

1. **A USB drive, never the SD card.** Format it ext4 (FAT and exFAT cannot store Unix owners,
   so the ownership step below fails), mount it at a fixed place, and add it to `/etc/fstab`
   by UUID (`sudo blkid`) with `nofail`, so the Pi still boots without it:

   ```text
   UUID=…  /media/usb  ext4  defaults,nofail  0  2
   ```

2. ```sh
   sudo install -d -o "$(id -u)" -g "$(id -g)" -m 700 /media/usb/lectern-backups
   ```

   In `.env`: `LECTERN_BACKUP_PATH=/media/usb/lectern-backups`, then `lectern-compose up -d`.
3. **Admin → Backups → Nightly backups**: turn it on, pick a time, press **Back up now**, and
   check *Last success*.
4. **Save the repository password now**: copy `data/lectern/backup-restic-password` into your
   password manager. Without it the backups cannot be read. Keep `.env` there too.
5. **Tailscale node state** is not in Lectern's backup (it holds the node's private key). Back
   it up as root after setup, and keep the archive as safe as a password:

   ```sh
   sudo tar czf "/media/usb/lectern-backups/tailscale-state-$(date +%F).tgz" -C ~/lectern/data/tailscale .
   ```

6. Keep a second copy away from the Pi (another machine, a rotated drive): one drive next to
   the Pi does not survive a fire or a power surge.

**Restore** (with Lectern stopped; it refuses otherwise):

```sh
cd ~/lectern
lectern-compose stop lectern
lectern-compose run --rm --no-deps lectern lectern backup --list
lectern-compose run --rm --no-deps lectern lectern restore            # the newest snapshot
lectern-compose start lectern
```

**On a new Pi:** do steps 1, 2, and 3.1–3.4 of this guide, **not** the first run (no new owner
account). Restore your saved `.env`, or write one with the **same** `LECTERN_BASE_URL`
(passkeys are bound to it) and `LECTERN_BACKUP_PATH` set to the drive's backup folder;
without it the restore looks in the empty default `backups/` and finds nothing.
Then mount the drive, put the saved password in `data/lectern/backup-restic-password`
(mode 600), and restore the Tailscale state
([as root](deploy-tailscale.md#backups-and-restore)). The restore container runs inside the
Tailscale container's network, so build the image and start Tailscale first, then restore and
start everything:

```sh
cd ~/lectern
LECTERN_COMMIT=$(git rev-parse HEAD) lectern-compose build
lectern-compose up -d tailscale
lectern-compose run --rm --no-deps lectern lectern restore
lectern-compose up -d
```

Without the Tailscale state, remove the old `lectern` machine on the Machines page first and
put a fresh auth key in `.env`, or the new one is named `lectern-1`. Details, and what to do
if a restore is interrupted:
[Restore](backups.md#restore) and [Backups and restore](deploy-tailscale.md#backups-and-restore).

## 11. Troubleshooting

More in [the Tailscale guide](deploy-tailscale.md#troubleshooting).

### Alignment stuck on "Aligning…" or "cannot run"

- **No worker.** The default image never aligns on the Pi; jobs wait for a worker. The book's
  Formats card says alignment cannot run when no worker has connected in 5 minutes. Check
  **Admin → Workers**: is the Mac listed, online, and not paused (battery, Wi-Fi rules, menu
  bar **Worker on**)? Jobs start as soon as it connects.
- **Model download blocked** (school network): the worker hands the job back and stops offering
  that capability for 30 minutes. Let it download the models once at home.
- **A run that stopped moving** is taken back after `LECTERN_ALIGN_STALL_SECONDS` (1 hour) and
  retried, up to three attempts. After that the Formats card shows the error and **Retry**.
- `lectern-compose logs lectern | grep -i align` shows what the server thinks.

Background: [Jobs, leases, and the ETA](alignment.md#jobs-leases-and-the-eta).

### Bind-mount errors

- **`bind source path does not exist`**: create the folders as your user,
  `mkdir -p library/_inbox data/lectern` (or the paths in `.env`, which must be absolute).
- **`Permission denied` / `unable to open database file`**: the folders are not owned by
  `LECTERN_UID:LECTERN_GID`: `sudo chown -R "$(id -u):$(id -g)" data/lectern library`.
- **Backup folder not writable**: Compose created it as root;
  `sudo chown "$(id -u):$(id -g)" /media/usb/lectern-backups`.
- **`required variable LECTERN_BASE_URL is missing a value`**: Compose ran without
  `--env-file .env`; use the alias or the full command.

### `xterm-ghostty`: unknown terminal type

SSH from the [Ghostty](https://ghostty.org/docs/help/terminfo) terminal, and the Pi does not
know its terminal type: `missing or unsuitable terminal: xterm-ghostty`, or `nano` refuses to
start. From the Mac, copy the terminal description to the Pi once:

```sh
infocmp -x xterm-ghostty | ssh lectern-pi -- tic -x -
```

Or fall back to a common type for this host in the Mac's `~/.ssh/config` (OpenSSH 8.7 or
newer): `SetEnv TERM=xterm-256color` under `Host lectern-pi`.

### `lectern.….ts.net` does not resolve

- The device is not on the tailnet or Tailscale is off: check the Tailscale app.
- MagicDNS is off for the tailnet, or for this device ("Use Tailscale DNS" in the app; on a
  Linux computer `tailscale set --accept-dns=true`).
- The browser uses its own DNS-over-HTTPS (Chrome *Secure DNS*, Firefox *DNS over HTTPS*), or
  another VPN or a school network's settings override DNS. Turn those off for this, or test
  with the `lectern` machine's `100.x.y.z` address in a terminal (`curl -I http://100.x.y.z:8080/healthz`).
- **On the Pi itself** the name does not resolve, by design: only the Tailscale container is on
  the tailnet, not the Pi. Use `lectern-compose exec tailscale tailscale status`.
- The machine is now called `lectern-1` (a restore without its Tailscale state): see
  [step 10](#10-backups-and-restore).
- E-readers use the `http://100.x.y.z:8080` address, never the name.

### SSH to the Pi from school

`lectern-pi.local` only works on your home network, and SSH does not use the HTTP proxy the
Mac worker uses in school mode. To reach the Pi from elsewhere, put the **Pi itself** on your
tailnet, next to the sidecar (we have not tested this combination on a Pi; the two run in
separate network namespaces):

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname lectern-pi        # never "lectern": that name is the server's
```

Disable key expiry for `lectern-pi` too, then `ssh pi@lectern-pi` from the Mac with Tailscale
on. Tailscale falls back to its relays over HTTPS when a network blocks its direct
connections, so this often works on restrictive Wi-Fi; if the network blocks Tailscale
altogether, use your phone's hotspot. Do not open port 22 on your router.
