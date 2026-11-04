# Security model

Lectern is a private library: everything except a handful of public files needs an account,
every credential is a revocable secret stored only as a digest, and each person, device, AI
app, and worker can reach only what its role or key allows. This page lists the rules the
server enforces. The routes, tables, and interfaces behind them are in
[`contracts.md`](contracts.md).

## Authentication and public routes

Every route except `/healthz`, `/login`, and `/setup` requires authentication. The exception
is `/app/assets/*`: content-hashed JS, CSS, and fonts built from `frontend/`, with no data or
configuration in them, and the installed app's public files: `sw.js`, `manifest.webmanifest`,
icons and launch screens under `/app/pwa/`, pdf.js data under `/app/pdfjs/`, and
`/app/shell.html` (the same HTML everyone gets, for offline use). Every other page under
`/app` and every `/api` route require a session.

`POST /users/create` (KOReader's Register button) always returns 403; keys come from the web
UI. See [`koreader-plugin.md`](koreader-plugin.md) for connecting an e-reader.

## Passwords, secrets, and cookies

- Owner password: argon2id. TOTP optional. Passkeys preferred.
- Sessions, sync keys, app passwords, and the setup token are 256-bit random secrets; only
  their SHA-256 digests are stored.
- Cookies are `HttpOnly; Secure; SameSite=Lax`; state-changing browser requests need a
  per-session CSRF token and a same-origin `Origin`/`Sec-Fetch-Site`.

## Roles

- **Owner:** user admin, invites, audit log, worker keys.
- **Member:** whole library, uploads, can request alignments.
- **Reader:** only shelves shared with them; everything else answers 404, OPDS and KOReader
  progress projection included.

## The installed app

The installed app keeps only books you downloaded on the device; the service worker never
caches API responses, and sign-out (or an ended session, or someone else signing in) deletes
everything it stored. Details are in
[Privacy: what is stored where](offline.md#privacy-what-is-stored-where).

## Alerts and download caps

Lectern alerts you and the owner about new sign-ins, new devices, and download bursts, and
caps whole-file downloads per hour, per account and per device. How the caps are counted and
who may change them is described in
[Notifications, alerts, and download caps](contracts.md#notifications-alerts-and-download-caps-milestone-2).

## AI apps and workers

- AI apps authenticate at `/mcp` with a per-user `lak_…` key (stored hashed, revocable, scoped
  to `upload`/`read`/`organize`) that acts only with its user's permissions and opens nothing
  else; no MCP tool returns book files or audio. See [`mcp.md`](mcp.md).
- External alignment workers authenticate with an owner-issued `lw_…` key (stored hashed,
  revocable) that opens only `/api/worker/*`, and only the inputs of jobs they hold a lease
  on. See [`alignment.md`](alignment.md) and the
  [worker protocol](contracts.md#worker-protocol).

## Rate limits, lockout, and the audit log

Lectern applies per-IP rate limits and a per-account lockout, and keeps an audit log of
logins, credential changes, and downloads (narrated EPUBs included).

The client IP is the socket peer unless that peer is listed in `LECTERN_TRUSTED_PROXIES`; only
then is `X-Forwarded-For` honoured, read right to left so a client-supplied entry is never
believed. `CF-Connecting-IP` is ignored unless `LECTERN_TRUST_CF_CONNECTING_IP` is on. Details
are in [Client addresses behind a proxy](contracts.md#client-addresses-behind-a-proxy-implemented).

## Non-goals

- **Anonymous access.** Apart from the public files listed under
  [Authentication and public routes](#authentication-and-public-routes), nothing is served
  without an account.
- **DRM removal or circumvention.** Lectern only reads EPUBs and audio files you already own
  and can open. It contains no code to strip, bypass, or work around any copy protection.
- **Book searching or downloading integrations.** There is no catalogue search, no connection
  to any download site, store, or torrent/IRC/Usenet source, and no fetching of books or
  audiobooks from the Internet. Content enters the library only through your own uploads and
  the inbox folder. Optional metadata suggestions only fetch descriptive fields (title,
  authors, description, ISBN, …) from Open Library and Google Books when the owner enables
  them; they never search for, link to, or download book files.
