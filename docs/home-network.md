# Home Wi-Fi access for e-readers (optional)

By default Lectern is reachable only through Tailscale ([deploy-tailscale.md](deploy-tailscale.md)).
That works everywhere, but an e-reader then needs Tailscale running, which on a Kindle means a
jailbreak extension that can be fiddly. **Home Wi-Fi access** lets e-readers sync over your
home network without Tailscale, while nothing is put on the Internet.

It is **off by default**. Turn it on only if you need it.

```
 at home, Tailscale off                       anywhere, Tailscale on
 ┌──────────┐ http://192.168.1.20:8080        ┌──────────┐ http://100.x.y.z:8080
 │  Kindle  │──────────────┐                  │  Kindle  │───────────┐
 └──────────┘              ▼                  └──────────┘           ▼
                 ┌─────────────────────────────────────────────────────────────┐
                 │ Pi: LAN IP 192.168.1.20:8080 ─► Lectern's home listener     │
                 │       (only 192.168.x / 10.x / 172.16–31.x / link-local     │
                 │        clients; only OPDS, sync, and the plugin's routes)   │
                 │     tailnet ─► Lectern's main listener (everything)         │
                 └─────────────────────────────────────────────────────────────┘
 phones, laptops, the web app, passkeys: always https://lectern.<tailnet>.ts.net
```

## What it does and does not do

- **A second listener**, published on one address of the Pi: its own LAN IP and a port you
  choose. Lectern answers it only for clients with a private address (RFC 1918
  `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`; link-local `169.254.0.0/16` and `fe80::/10`;
  IPv6 unique-local `fc00::/7`). Everything else gets *403*, including tailnet addresses
  (`100.64.0.0/10`), so this listener is for the home network only.
- **Only what e-readers use:** the OPDS catalogue (`/opds`), KOReader progress sync
  (`/users/auth`, `/syncs/progress`), the KOReader plugin's routes (`/koplugin/…`), and
  `/healthz`. The web app, sign-in pages, AI apps (`/mcp`), and the Mac worker's routes are not
  served there. They answer *403* and point to the Tailscale address. The web app's cookies,
  passkeys, and cross-site checks stay bound to `https://lectern.<tailnet>.ts.net`
  (`LECTERN_BASE_URL`), which does not change.
- **Same security as everywhere else.** The same per-device credentials (sync key, catalogue
  password), the same rate limits per address, the same alerts and Activity entries. The
  trusted-proxy rule is unchanged (`LECTERN_TRUSTED_PROXIES` stays `127.0.0.1`), so a device on
  your Wi-Fi cannot choose the address Lectern records for it.
- **The KOReader plugin knows both addresses** and uses the home one when it works, Tailscale
  otherwise ([below](#the-koreader-plugin)).

> **Anyone on your home Wi-Fi can reach this address** and try to sign in as a device. The
> catalogue, progress sync, and pairing codes all need credentials, and rate limits and alerts
> apply. Still, this is a door for every phone, TV, and guest that has your Wi-Fi password. If
> that is not acceptable, leave it off and use Tailscale on the e-reader. **Admin → Home Wi-Fi
> access** shows the same warning while it is on.

## Turn it on

1. **Give the Pi a fixed LAN address.** In your router's DHCP settings, reserve an address for
   the Pi (often called *DHCP reservation* or *static lease*), for example `192.168.1.20`.
   `hostname -I` on the Pi prints its current address. The e-reader stores the address, so it
   must not change.
2. **Add two lines to `.env`** (the values are examples):

   ```sh
   LECTERN_LAN_BIND=192.168.1.20:8080   # the Pi's LAN IP and a free port on it
   LECTERN_LAN_URL=                     # empty = http://192.168.1.20:8080
   ```

   Use the Pi's own address, never `0.0.0.0`. Before anything is published, a small
   `lan-check` step makes sure `LECTERN_LAN_BIND` is a private IPv4 address and a port; a
   bare port, `0.0.0.0`, or a public address stops `up` with an error, and the Tailscale
   container (which publishes the port) does not start, so nothing is published. Fix
   `.env` and run `up` again.
3. **Start with the extra file**, and add it to every compose command from now on (`up`,
   `logs`, upgrades):

   ```sh
   docker compose --env-file .env -f deploy/tailscale/compose.yaml -f deploy/tailscale/lan.yaml up -d --build
   ```

   Without `-f deploy/tailscale/lan.yaml` the next `up` turns home Wi-Fi access off again. That
   is safe, but the e-readers then fall back to Tailscale.
4. **Check it** from a laptop on the same Wi-Fi:

   ```sh
   curl http://192.168.1.20:8080/healthz    # {"status":"ok"}
   curl http://192.168.1.20:8080/           # 403: Lectern's home Wi-Fi address for e-readers…
   ```

   **Admin → Home Wi-Fi access** says *On* with the address.
5. **Download a new plugin zip** for each e-reader (**Devices → the device → KOReader
   plugin**). The sheet shows the home address it carries, next to the Tailscale one. Install
   it as in [koreader-plugin.md](koreader-plugin.md#install).

## The KOReader plugin

The zip carries the Tailscale address (*Server address for the e-reader*) and the home address.
At home the plugin uses the home address for **setup, pairing, progress sync, and Books sent to
this device**. KOReader's **OPDS catalogue** (Search → OPDS catalog → Lectern) always uses the
Tailscale address: while it is open, KOReader's catalogue browser keeps its own copies of
links and the catalogue password that the plugin cannot take back when the device leaves
home. Without Tailscale, get books onto the e-reader with **Send to device** in Lectern and
**Tools → Lectern → Books sent to this device**.

The home address is trusted only after a check, and only until the next start, wake from
sleep, or network change. At each of those the plugin goes back to the Tailscale address at
once (KOReader's own Progress sync settings included), then checks the home
address a few seconds later; it also checks again on **Sync now** and every 10 minutes. The
check: the plugin asks the home address to prove it is this device's Lectern: it sends a random nonce, and Lectern answers with
an HMAC of the nonce keyed by the SHA-256 of a secret the device holds (its catalogue password,
or before setup its one-time setup secret). Lectern stores exactly those hashes, so it can
answer, and nobody else can. Only then does the plugin send credentials there. In a café or
at a friend's, a different machine may well have the same private address, and the plugin
does not send it anything secret. It uses Tailscale instead.

The Tailscale address is the fallback, used without a check, so it can never be a home
address: Lectern refuses a home-network address as the zip's *Server address*, and
**Pair with a code** over the home address keeps the Tailscale address Lectern reports (or the
one the plugin already had). If neither is known, pairing stops with *Not paired* and asks
for the Tailscale address instead. If a device ends up with no trusted address at all (for
example its stored address is itself a home-network one), the plugin removes its credentials
from KOReader's Progress sync and catalogue settings until an address is trusted again, rather
than pointing them at a placeholder.

When the choice changes, the plugin rewrites KOReader's Progress sync server, so KOReader's
own sync follows. **Tools → Lectern → status** shows both
addresses and which one is in use. If the home address stops answering mid-session, the next
request retries over Tailscale.

A Kindle **without Tailscale** works at home only. Its first start (the setup) must happen at
home, and positions read elsewhere wait in the plugin's outbox until it is home again. Right
after it wakes up or joins the Wi-Fi, a few seconds pass before the check confirms home; a
position pushed in that moment waits in the outbox too.

When you turn home Wi-Fi access off or change its address, plugins pick that up from Lectern
at their next contact over Tailscale. Turning it off removes the home address from them.

## A name instead of an address (mDNS)

Raspberry Pi OS runs `avahi-daemon`, so the Pi already answers `<hostname>.local` on your
network (`lectern-pi.local` if you named it that in Raspberry Pi Imager). To put that name in
plugin zips, set `LECTERN_LAN_URL=http://lectern-pi.local:8080` (any `*.local` name works;
`LECTERN_LAN_BIND` still needs the IP). Only do this if your e-readers resolve `.local` names:
**most Kindles do not**, so for a Kindle keep the IP address. Lectern has no mDNS responder of
its own; its container cannot announce names on your LAN, and the host already does.

## Router and firewall

- **Never forward this port** (or any Lectern port) on your router, and do not put the Pi in
  a "DMZ". Home Wi-Fi access is for devices that are already at home. Lectern refuses
  non-private addresses anyway, but a forwarded port invites scanning and fills Activity with
  noise.
- **UPnP:** Lectern and Docker never ask the router to open ports. If your router lists a
  mapping to the Pi you did not make, remove it.
- **Guest Wi-Fi** usually isolates clients from each other and from the Pi. Put e-readers on
  your main network, or keep them on Tailscale.
- **A firewall on the Pi** (for example `ufw`) does not filter ports Docker publishes: Docker
  inserts its own rules before them. `LECTERN_LAN_BIND`'s single address is what limits
  exposure, together with Lectern's own address check.
- **IPv4 only.** The listener is published on the IPv4 address you give.
- **Keep Docker's iptables integration on** (the default). With `"iptables": false` in Docker's
  `daemon.json`, Docker relays published ports through its own proxy, every request then
  appears to come from Docker's private bridge address, and Lectern's address check can no
  longer tell home from elsewhere.

## Security notes

- **Plain `http://` on your Wi-Fi.** Unlike Tailscale, nothing encrypts the e-reader's traffic
  beyond the Wi-Fi itself, and anyone with the Wi-Fi password can in principle capture it.
  What travels is the e-reader's own sync key and catalogue password, never your account
  password, and **Devices → ⋯ → Revoke device** stops them at once.
- **Pairing codes** (6 digits, 5 minutes, 5 tries, limited per address and per username)
  work on the home address too, so a re-pair at home needs no Tailscale.
- **Addresses in Activity and alerts** are the device's LAN address (for example
  `192.168.1.42`), so rate limits apply per device at home too.

## Troubleshooting

- **`curl` says *connection refused*:** the overlay is not in your compose command, or
  `LECTERN_LAN_BIND` is not the Pi's current address (`hostname -I`).
- **403 *Lectern answers only the home network here*:** the request reached Lectern from an
  address outside the private ranges, for example through a VPN on the testing laptop.
- **The plugin keeps using Tailscale at home:** **Tools → Lectern → status** shows whether it
  knows a home address. If not, download a new zip (zips from before home Wi-Fi access was on
  do not carry it), or let it reach Lectern once over Tailscale. If it does, check the address
  with `curl` from a laptop on the same Wi-Fi.
- **`up` stops with *service "lan-check" didn't complete successfully*:** `docker compose …
  logs lan-check` says why; `LECTERN_LAN_BIND` must look like `192.168.1.20:8080`.
- **Lectern does not start:** `docker compose … logs lectern` names the setting:
  `LECTERN_LAN_URL` must be `http://` with a private IP or a `.local` name.
