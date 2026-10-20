# Running Lectern at home with Cloudflare Tunnel

This guide is for a first-time self-hoster. At the end you will have Lectern reachable at
`https://books.example.com` from anywhere, with **no port opened on your router**, HTTPS handled
for you (so passkeys work), and KOReader still able to sync.

How it works: a small program, `cloudflared`, runs next to Lectern and opens an *outbound*
connection to Cloudflare. Visitors talk to Cloudflare; Cloudflare hands the request down that
connection to `cloudflared`, which forwards it to Lectern over the private Docker network.
Nothing on your LAN is exposed.

```diagram
  reader's browser / KOReader
            │  https://books.example.com
            ▼
   ┌──────────────────┐
   │ Cloudflare edge  │  TLS ends here; adds CF-Connecting-IP = reader's IP
   └────────┬─────────┘
            │  outbound tunnel (opened from your home)
   ┌────────▼─────────┐          Docker network "lectern" (172.30.0.0/24)
   │ cloudflared      │ 172.30.0.3
   │ (container)      │ ──── http://lectern:8080 ────▶ ┌──────────────┐
   └──────────────────┘                                │ lectern      │ 172.30.0.2
                                                       │ (container)  │ no ports published
                                                       └──────────────┘
```

## You need

- A domain whose DNS is managed by Cloudflare (a free account is enough).
- A machine at home that runs Docker (Docker Desktop, or Docker Engine on Linux with the
  Compose plugin). Check with `docker compose version`.
- About 20 minutes.

## 1. Create the tunnel in Cloudflare

1. Open <https://one.dash.cloudflare.com> (Zero Trust). Pick or create a team name if asked
   (free plan is fine).
2. Go to **Networks → Tunnels → Create a tunnel**. Choose **Cloudflared**, name it `lectern`,
   click **Save tunnel**.
3. On the next screen, under *Install and run a connector*, you will see a long command that
   ends in a token (a long string after `--token`). **Copy only the token.** Treat it like a
   password: anyone with it can attach to your tunnel.
4. Click **Next**. Under *Public Hostname* enter:
   - Subdomain: `books`, Domain: `example.com` (your domain).
   - Service type: `HTTP`, URL: `lectern:8080`.
   
   Click **Save tunnel**. Cloudflare creates the DNS record for you.

## 2. Put Lectern and cloudflared in one Compose file

Get the code (there is no published image yet; Compose builds it for you):

```sh
git clone https://github.com/jeninh/lectern.git ~/lectern
cd ~/lectern
```

Then **replace** the repository's `compose.yaml` with the one below and create `.env` next to
it.

`.env` (never commit this; it holds the tunnel token):

```sh
LECTERN_BASE_URL=https://books.example.com
TUNNEL_TOKEN=paste-the-token-here
```

`compose.yaml`:

```yaml
services:
  lectern:
    build: .
    image: lectern:local
    container_name: lectern
    restart: unless-stopped
    # No `ports:` on purpose. The only way in is through the tunnel.
    environment:
      LECTERN_BASE_URL: ${LECTERN_BASE_URL}
      # cloudflared is the only trusted proxy, by its fixed address below.
      LECTERN_TRUSTED_PROXIES: 172.30.0.3
      LECTERN_LOG_LEVEL: INFO
    volumes:
      - lectern-data:/data
      - ./library:/library
    networks:
      lectern:
        ipv4_address: 172.30.0.2
    read_only: true
    tmpfs:
      - /tmp
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL

  cloudflared:
    image: cloudflare/cloudflared:latest
    container_name: cloudflared
    restart: unless-stopped
    command: tunnel --no-autoupdate run
    environment:
      TUNNEL_TOKEN: ${TUNNEL_TOKEN}
    depends_on:
      - lectern
    networks:
      lectern:
        ipv4_address: 172.30.0.3
    read_only: true
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL

networks:
  lectern:
    ipam:
      config:
        - subnet: 172.30.0.0/24

volumes:
  lectern-data:
```

Why the fixed addresses: Lectern believes the `CF-Connecting-IP` header (the reader's real IP,
used for rate limits and the audit log) **only** from the peer listed in
`LECTERN_TRUSTED_PROXIES`. Pinning cloudflared to `172.30.0.3` makes that list exact. Do not
widen it to the whole subnet or to `0.0.0.0/0`: the Docker gateway is inside the subnet, so if
you ever publish a port, LAN clients could then forge their address.

## 3. Start it and create the owner account

```sh
cd ~/lectern
mkdir -p library/_inbox
docker compose up -d --build
docker compose logs lectern | grep -A3 "FIRST-RUN SETUP"
```

The first build takes a few minutes (it compiles the web app and installs Python packages).

The log shows a link like `https://books.example.com/setup?token=…`. Open it within 30 minutes
and create the owner account. Registration closes permanently after that. Passkeys work
immediately because the address is HTTPS and `LECTERN_BASE_URL` matches it exactly.

Check the tunnel: `docker compose logs cloudflared` should say *Registered tunnel connection*
a few times. In the Cloudflare dashboard the tunnel shows **HEALTHY**.

## 4. KOReader

Exactly as in the README, using the public hostname:

- Progress sync: Custom sync server `https://books.example.com/`, **Login** with your username
  and the device's sync key.
- OPDS: `https://books.example.com/opds` with your username and the device's app password.

If sync or the catalogue fails with an HTML error page rather than 401, see
*Cloudflare Access* below and the note on Bot Fight Mode.

## 5. Adding books, especially big ones

- Small EPUBs: use **Upload** in the web app as usual.
- **Cloudflare caps each request body at 100 MB** on the free and Pro plans. A browser upload
  larger than that fails at the edge before it reaches Lectern. Until the resumable upload
  (milestone 2, see `contracts.md`) ships, and for anything really large (audiobooks),
  **copy the files into the inbox folder on the host instead**: `~/lectern/library/_inbox/`.
  Lectern notices new files within a few seconds. Over your LAN this is a plain file copy
  (Finder, Explorer, `scp`, Syncthing), so it is faster than any upload through the Internet.
- Finished books are stored under `~/lectern/library/<Author>/<Title>/`. Back that folder up.

## Optional: protect the web UI with Cloudflare Access

Cloudflare Access puts a login page (Google, GitHub, email one-time code, …) *in front of*
Lectern, so attackers never reach Lectern's own login form. Lectern still requires its own
sign-in afterwards; Access is an extra wall, not a replacement.

The catch: **KOReader cannot complete an Access login.** It talks plain HTTP(S) with Basic
auth or two custom headers and would just receive Cloudflare's HTML login page. So the paths
KOReader uses must bypass Access. Set it up in this order:

1. Zero Trust → **Access → Applications → Add an application → Self-hosted**.
2. First application, name `Lectern KOReader`, with **three** public hostnames/paths, all on
   `books.example.com`:
   - path `opds*`
   - path `users/*`
   - path `syncs/*`
   
   Add one policy, action **Bypass**, include **Everyone**. Save.
3. Second application, name `Lectern`, hostname `books.example.com` with an **empty** path.
   Add a policy, action **Allow**, include the e-mail addresses (or identity-provider group) of
   your household. Save.

Access evaluates the more specific path first, so the bypass application wins for KOReader's
routes and everything else requires an Access login. Those three prefixes are only ever
served with Lectern's own authentication (sync key or app password; `/users/create` always
answers 403), so bypassing Access there does not open anything anonymous.

Note: Cloudflare's **Bot Fight Mode** (Security → Bots) can also challenge KOReader with an
HTML page. If syncs fail after enabling it, turn it off for this hostname or add a WAF skip
rule for `/opds*`, `/users/*`, `/syncs/*`.

## Security caveats, in plain words

- **Cloudflare sees your traffic.** TLS ends at Cloudflare's edge; between the edge and
  `cloudflared` it is encrypted again, but Cloudflare can read requests in between. This is
  the normal tunnel trade-off; if that is not acceptable to you, use a VPN (Tailscale,
  WireGuard) and the README's reverse-proxy setup instead.
- **The tunnel token is a secret.** It lives in `.env` only. If it leaks, delete the tunnel in
  the dashboard and create a new one.
- **`LECTERN_TRUSTED_PROXIES` must name only cloudflared.** Anything listed there can tell
  Lectern "this request came from IP X", which affects rate limiting and the audit log (never
  authentication). Empty means trust nobody, which is the safe default.
- **Passkeys are bound to the hostname.** Changing `books.example.com` later means registering
  passkeys again. Password + TOTP keep working.
- **Access is optional, and bypassed paths are protected by Lectern alone.** That is by
  design (see above); keep sync keys and app passwords per device so one can be revoked.
- **Keep both containers updated**: `git pull && docker compose pull cloudflared &&
  docker compose up -d --build`. cloudflared is started with `--no-autoupdate` so updates
  happen only when you do this.
- **Backups**: the `lectern-data` volume (database, signing key) and `./library`.
