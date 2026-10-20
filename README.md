# Lectern

Self-hosted reading server for one household: a library of works, an authenticated
OPDS catalogue, and KOReader progress sync between devices. Milestone 1.

## Run

```sh
mkdir -p library/_inbox
LECTERN_BASE_URL=https://lectern.example docker compose up -d --build
docker compose logs lectern | grep -A3 "FIRST-RUN SETUP"
```

Open the printed `/setup?token=…` link within 30 minutes to create the owner account.
Registration closes permanently after that. Restarting the server mints a new token
only while setup is incomplete.

Put a TLS-terminating reverse proxy in front of port 8080. Passkeys require HTTPS and a
stable hostname; until then, password + TOTP sign-in works. For local plain-http testing
set `LECTERN_INSECURE_HTTP=1` and `LECTERN_BASE_URL=http://localhost:8080`.

Running at home without opening a port? See
[`docs/deploy-cloudflare-tunnel.md`](docs/deploy-cloudflare-tunnel.md) for Lectern +
cloudflared in one Compose file. When a proxy sits in front, set `LECTERN_TRUSTED_PROXIES` to
its address so rate limits and the audit log see your readers' IPs instead of the proxy's.

## Adding books

Upload EPUBs from the web app (**Upload** in the Library, or "Upload book" in the
Cmd/Ctrl+K palette), or drop `.epub` files into `library/_inbox/`. Within a few seconds each
one is read (OPF metadata via lxml), hashed (SHA-256 and KOReader's partial MD5), matched to an
existing work by ISBN or fuzzy title + author, and moved to `library/<Author>/<Title>/book.epub`
with `cover.jpg`. Rejected files land in `library/_inbox/_rejected/`. Browser uploads are
limited to owners and members and capped by `LECTERN_UPLOAD_MAX_BYTES`.

## KOReader

Open **Devices → Add device** (or press `N`). Each device gets a KOReader sync key and an OPDS
app password together. Both are shown once and revoked together. The sheet shows the exact
values to enter, then waits and confirms the device's first sync live.

- Progress sync: Tools → Progress sync → Custom sync server → `https://lectern.example/`,
  then **Login** (not Register) with your Lectern username and the sync key.
- Catalogue: OPDS catalog → add `https://lectern.example/opds` with your username and
  the app password.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `LECTERN_BASE_URL` | `http://localhost:8080` | Public origin; passkey RP ID derives from it |
| `LECTERN_DATA_DIR` | `/data` | SQLite database and generated signing key |
| `LECTERN_LIBRARY_DIR` | `/library` | Book storage |
| `LECTERN_INBOX_DIR` | `<library>/_inbox` | Watched folder |
| `LECTERN_INSECURE_HTTP` | `0` | Drop the Secure cookie flag (development only) |
| `LECTERN_SECRET_KEY` | generated | Signing key; persisted under the data dir if unset |
| `LECTERN_SESSION_TTL_SECONDS` | 30 days | Browser session lifetime |
| `LECTERN_LOGIN_MAX_FAILURES` | 5 | Failures per account before lockout |
| `LECTERN_LOGIN_LOCKOUT_SECONDS` | 900 | Lockout window |
| `LECTERN_RATE_LIMIT_PER_MINUTE` | 60 | Per-IP limit on OPDS/sync auth attempts |
| `LECTERN_UPLOAD_MAX_BYTES` | 200 MiB | Largest EPUB accepted by browser upload |
| `LECTERN_TRUSTED_PROXIES` | empty | Comma-separated IPs/CIDRs of reverse proxies whose `CF-Connecting-IP` / `X-Forwarded-For` are believed; empty trusts nobody |

## Security model

- Every route except `/healthz`, `/login`, and `/setup` requires authentication. The
  exception is `/app/assets/*`: content-hashed JS, CSS, and fonts built from `frontend/`,
  with no data or configuration in them. The app shell at `/app` and every `/api` route
  require a session.
- Owner password: argon2id. TOTP optional. Passkeys preferred.
- Sessions, sync keys, app passwords, and the setup token are 256-bit random secrets;
  only their SHA-256 digests are stored.
- Cookies are `HttpOnly; Secure; SameSite=Lax`; state-changing browser requests need a
  per-session CSRF token and a same-origin `Origin`/`Sec-Fetch-Site`.
- Roles: owner (user admin, audit log), member, reader.
- Per-IP rate limits, per-account lockout, and an audit log of logins, credential
  changes, and downloads. The client IP is the socket peer unless that peer is listed in
  `LECTERN_TRUSTED_PROXIES`; only then are `CF-Connecting-IP` and `X-Forwarded-For` honoured.
- `POST /users/create` (KOReader's Register button) always returns 403; keys come from
  the web UI.
- Non-goals: anonymous access, and the two below.

## What Lectern does not do

- **No DRM removal or circumvention.** Lectern only reads EPUBs and audio files you already own
  and can open. It contains no code to strip, bypass, or work around any copy protection.
- **No book searching or downloading integrations.** There is no catalogue search, no
  connection to any download site, store, or torrent/IRC/Usenet source, and no fetching of
  books or audiobooks from the Internet. Content enters the library only through your own
  uploads and the inbox folder. (The test suite contains one documented public-domain fixture
  from Project Gutenberg and LibriVox; see `tests/fixtures/magi/PROVENANCE.md`.)

The data model and API contracts for audiobooks, text–audio alignment, shelves, invites, and
external alignment workers are in [`docs/contracts.md`](docs/contracts.md).

## Development

```sh
uv sync --extra dev
uv run pytest
LECTERN_DATA_DIR=./data LECTERN_LIBRARY_DIR=./library LECTERN_INSECURE_HTTP=1 uv run lectern
```

### Web app

The UI is a React + Vite single-page app in `frontend/`, built into `src/lectern/static/app`
(git-ignored; the Docker image builds it) and served at `/app` behind the session cookie. It
talks only to the authenticated JSON API under `/api`; every write sends the per-session
`X-CSRF-Token` header. Setup and login stay server-rendered.

```sh
npm --prefix frontend ci
npm --prefix frontend run build      # one-off build that FastAPI serves at /app

# Live reload: run the backend as above on :8080, then
npm --prefix frontend run dev        # http://localhost:5173/app/
```

`npm run dev` proxies `/api`, `/login`, `/logout`, `/setup`, `/library`, and `/static` to
`http://127.0.0.1:8080` and keeps the browser's `Host`. With `LECTERN_INSECURE_HTTP=1`, as in
the command above, cookies and the CSRF/same-origin checks behave as in production. Sign in at
`http://localhost:5173/login`.

Keyboard: `⌘/Ctrl K` search, `/` filter, `N` add device, `G` then `H`/`L`/`D`/`S`/`A`,
`T` theme, `[` sidebar, `Esc` close, `?` all shortcuts. Screenshots: `docs/screenshots/`.
