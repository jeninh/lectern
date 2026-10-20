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
to Lectern over loopback. E-readers running KOReader talk to Lectern's port directly over the
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

- A machine at home that runs Docker (Docker Desktop, or Docker Engine on Linux with the
  Compose plugin). Check with `docker compose version`. A Raspberry Pi 4/5 or any old laptop
  is enough.
- A free Tailscale account (<https://login.tailscale.com/start>). Sign in with Google, Apple,
  Microsoft, GitHub, or an e-mail passkey; the Personal plan allows 100 devices and 3 users.
- About 30 minutes.

## Sidecar or host install?

There are two places the Tailscale software can run. **This guide uses the sidecar.**

| | **Sidecar** (Tailscale in a container next to Lectern) | **Host install** (Tailscale on the machine itself) |
| --- | --- | --- |
| Setup | Everything is in one `compose.yaml`; nothing to install on the host | Install Tailscale on the host, keep Lectern's stock `compose.yaml`, run two `tailscale serve` commands |
| Machine name | Lectern is its own tailnet machine (`lectern.<tailnet>.ts.net`), separate from the host | The host's name is the URL (`https://nas.<tailnet>.ts.net`); the host may already be on your tailnet for other reasons |
| Who Lectern sees | Exact: `127.0.0.1` for `tailscale serve`, the device's own tailnet address for e-readers | Docker's loopback publishing hides addresses behind the Docker gateway; e-readers through a TCP forwarder all look alike (details [below](#alternative-host-install)) |
| Needs | `/dev/net/tun` on the host (standard on Linux and in Docker Desktop's VM) | Root on the host |

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

## 2. Put Lectern and Tailscale in one Compose file

Get the code (there is no published image yet; Compose builds it for you):

```sh
git clone https://github.com/jeninh/lectern.git ~/lectern
cd ~/lectern
mkdir -p library/_inbox tailscale
```

Create `.env` (never commit this; it holds the auth key). Replace the tailnet name with yours:

```sh
LECTERN_BASE_URL=https://lectern.tail1234.ts.net
TS_AUTHKEY=tskey-auth-paste-it-here
```

`LECTERN_BASE_URL` must be **exactly** the address you will type in the browser, HTTPS and
all. Passkeys are bound to it, and Lectern's CSRF check compares the browser's `Origin`
against it.

Create `tailscale/serve.json`. This is `tailscale serve` as a file: HTTPS on 443, proxied to
Lectern on loopback. `${TS_CERT_DOMAIN}` is filled in by the container with your machine's
full name.

```json
{
  "TCP": { "443": { "HTTPS": true } },
  "Web": {
    "${TS_CERT_DOMAIN}:443": {
      "Handlers": { "/": { "Proxy": "http://127.0.0.1:8080" } }
    }
  }
}
```

Then **replace** the repository's `compose.yaml` with this one:

```yaml
services:
  tailscale:
    image: tailscale/tailscale:latest
    container_name: lectern-tailscale
    restart: unless-stopped
    environment:
      TS_AUTHKEY: ${TS_AUTHKEY}
      TS_HOSTNAME: lectern              # -> lectern.<your-tailnet>.ts.net
      TS_STATE_DIR: /var/lib/tailscale  # node identity; keep it across restarts
      TS_AUTH_ONCE: "true"              # reuse the stored login instead of the key
      TS_USERSPACE: "false"             # kernel TUN: real peer addresses (see text)
      TS_SERVE_CONFIG: /config/serve.json
    volumes:
      - tailscale-state:/var/lib/tailscale
      - ./tailscale/serve.json:/config/serve.json:ro
    devices:
      - /dev/net/tun:/dev/net/tun
    cap_add:
      - NET_ADMIN
    # No `ports:` on purpose, here or below. The only way in is the tailnet.

  lectern:
    build: .
    image: lectern:local
    container_name: lectern
    restart: unless-stopped
    network_mode: service:tailscale     # Lectern lives inside the Tailscale container's network
    environment:
      LECTERN_BASE_URL: ${LECTERN_BASE_URL}
      # tailscale serve connects from loopback and sets X-Forwarded-For to the caller's
      # tailnet address. Nothing else is trusted.
      LECTERN_TRUSTED_PROXIES: 127.0.0.1
      LECTERN_LOG_LEVEL: INFO
    volumes:
      - lectern-data:/data
      - ./library:/library
    read_only: true
    tmpfs:
      - /tmp
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL

volumes:
  lectern-data:
  tailscale-state:
```

What the two unusual lines do:

- `network_mode: service:tailscale` makes the Lectern container share the Tailscale
  container's network stack. Lectern's port 8080 is therefore *the tailnet machine's* port
  8080, and `tailscale serve`'s `http://127.0.0.1:8080` is Lectern. Nothing is published to
  the host or the LAN.
- `TS_USERSPACE: "false"` runs Tailscale with a real network device (`/dev/net/tun`, hence
  `cap_add: NET_ADMIN`). Lectern then sees each device's actual tailnet address when it
  connects directly (e-readers), and sees `127.0.0.1` only for `tailscale serve`, which is why
  `LECTERN_TRUSTED_PROXIES=127.0.0.1` is exact. If the container fails to start with a
  `/dev/net/tun` error, change it to `"true"` and remove the `devices:` and `cap_add:` blocks;
  everything still works, with one difference described under
  [Security caveats](#security-caveats-in-plain-words).

## 3. Start it and create the owner account

```sh
cd ~/lectern
docker compose up -d --build
docker compose logs -f tailscale
```

The first build takes a few minutes (it compiles the web app and installs Python packages).
In the Tailscale log, wait for a line saying the serve config was applied (if it instead says
it *cannot issue TLS certs*, HTTPS is not enabled for your tailnet; go back to step 1). The
machine `lectern` now appears on your **Machines** page with its `100.x.y.z` address.

Then:

```sh
docker compose logs lectern | grep -A3 "FIRST-RUN SETUP"
```

The log shows a link like `https://lectern.tail1234.ts.net/setup?token=…`. Open it, within 30
minutes, from a device that is on your tailnet (the computer you are typing on is fine if it
runs Tailscale; otherwise your phone, see next step) and create the owner account.
Registration closes permanently after that. Passkeys work immediately because the address is
HTTPS and `LECTERN_BASE_URL` matches it exactly.

**Do this now:** on the Machines page, open the `⋯` menu of `lectern` and choose
**Disable key expiry**. Otherwise the node's key expires after 180 days (the default) and the
server silently drops off your tailnet until you re-authenticate it.

## 4. Phones and laptops

Install the Tailscale app on each device (App Store, Play Store, <https://tailscale.com/download>),
sign in with the **same account**, and turn it on. Open `https://lectern.tail1234.ts.net` in
the browser. That is all; there is nothing to configure per device. With Tailscale off, the
address simply does not resolve.

Add it to the home screen on a phone if you like; the web app is responsive.

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

Now in KOReader, use **`http://100.101.102.103:8080`**, not the `https://lectern…ts.net`
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

## Backups

Three things hold state:

| What | Where | Why it matters |
| --- | --- | --- |
| Library | `~/lectern/library` | your books and covers |
| Lectern database and signing key | Docker volume `lectern-data` (`/data` in the container) | users, passkeys, devices, reading progress, audit log |
| Tailscale node state | Docker volume `tailscale-state` | the machine identity; losing it only means signing the node in again with a new auth key |

Back up the first two; the third is optional. A simple nightly job:

```sh
cd ~/lectern
mkdir -p backups
docker compose stop lectern
docker run --rm --volumes-from lectern -v "$PWD/backups":/backup alpine \
  tar czf "/backup/lectern-data-$(date +%F).tgz" -C /data .
docker compose start lectern
rsync -a --delete library/ backups/library/
```

Stopping Lectern for the few seconds the copy takes guarantees a consistent SQLite file.
Keep `backups/` somewhere other than the same disk (an external drive, another machine on
the tailnet, cloud storage); the whole point of a personal library is not losing it.

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
- **The auth key is a secret** until used; it is in `.env` only. The `tailscale-state` volume
  holds the node's private key; treat the Docker host as you would any server.
- **`LECTERN_TRUSTED_PROXIES` must stay `127.0.0.1`.** Anything listed there can tell Lectern
  "this request came from address X", which affects rate limiting and the audit log (never
  authentication). In the default kernel-TUN setup only `tailscale serve` connects from
  loopback. If you had to fall back to `TS_USERSPACE: "true"`, Tailscale's userspace network
  stack delivers *every* tailnet connection, including e-readers, from `127.0.0.1`; a device
  on your tailnet could then send its own `X-Forwarded-For` to pick a rate-limit bucket or
  audit address, and devices without the header all share one. Harmless for a household;
  know that it is there.
- **E-reader traffic is `http://` inside the tailnet.** It is encrypted by WireGuard between
  the device and your Tailscale container, but there is no TLS and the device checks no
  certificate. Use the per-device sync key and app password, never your password, and revoke
  a lost device from **Devices**.
- **Passkeys are bound to the hostname.** Renaming the machine (or your tailnet) later means
  registering passkeys again. Password + TOTP keep working.
- **Keep things updated**: `cd ~/lectern && git pull && docker compose pull tailscale &&
  docker compose up -d --build`.
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
