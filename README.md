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

## Adding books

Drop `.epub` files into `library/_inbox/`. Within a few seconds each one is read (OPF
metadata via lxml), hashed (SHA-256 and KOReader's partial MD5), matched to an existing
work by ISBN or fuzzy title + author, and moved to `library/<Author>/<Title>/book.epub`
with `cover.jpg`. Rejected files land in `library/_inbox/_rejected/`.

## KOReader

On the Account page create one **sync key** per device and one **OPDS app password** per
device. Each is shown once and can be revoked individually.

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

## Security model

- Every route except `/healthz`, `/login`, and `/setup` requires authentication.
- Owner password: argon2id. TOTP optional. Passkeys preferred.
- Sessions, sync keys, app passwords, and the setup token are 256-bit random secrets;
  only their SHA-256 digests are stored.
- Cookies are `HttpOnly; Secure; SameSite=Lax`; state-changing browser requests need a
  per-session CSRF token and a same-origin `Origin`/`Sec-Fetch-Site`.
- Roles: owner (user admin, audit log), member, reader.
- Per-IP rate limits, per-account lockout, and an audit log of logins, credential
  changes, and downloads.
- `POST /users/create` (KOReader's Register button) always returns 403; keys come from
  the web UI.
- Non-goals: DRM circumvention, download/search integrations, anonymous access.

## Development

```sh
uv sync --extra dev
uv run pytest
LECTERN_DATA_DIR=./data LECTERN_LIBRARY_DIR=./library LECTERN_INSECURE_HTTP=1 uv run lectern
```
