# Health and alerts

**Health** (owners only, in the sidebar under Manage) gathers the signals that something is
wrong on the server in one page, with one overall status: OK, warning (amber), or critical
(red). While it is not OK, owners also see a small badge in the top bar.

## What it checks

| Group | Check | Amber | Red |
| --- | --- | --- | --- |
| Backups | Nightly backups | the last backup failed, or none for 26 hours | the backup folder is missing or not writable, restic is missing, or none for 3 days |
| Disk space | Each volume (data, library, backups; a volume they share is shown once) | less than 10 % free | less than 5 % free |
| Jobs | Failed jobs (alignment, narration, text recognition, PDF conversion, data-saver copies) in the last 7 days that were not retried successfully since | any | |
| Jobs | Stuck jobs: queued for a day with nobody taking them, or running without a heartbeat for an hour | any | |
| Workers | Kokoro narration, Piper narration, alignment | a worker that offered it has not been seen for 30 minutes, or its self-test failed | |
| Other | Database integrity (checked by every backup) | | the last backup's copy failed the check |
| Other | Database schema at the latest revision | | not at head |
| Other | Offline mode, push alerts | push alerts set up wrongly, or the last push was not delivered | |

Checks that need nothing from you (backups off, a capability nobody set up) show as *Info*.
Failed jobs link to their book and have **Retry** where the app has one. The thresholds are
settings in `.env`:

- `LECTERN_HEALTH_DISK_WARN_PERCENT` (default 10) and `LECTERN_HEALTH_DISK_CRITICAL_PERCENT`
  (default 5);
- `LECTERN_HEALTH_WORKER_GRACE_MINUTES` (default 30): raise it if the Mac that runs Kokoro
  sleeps at night.

The server checks every 5 minutes (with background jobs on); the page reuses a result that is
less than 15 seconds old, so leaving it open costs almost nothing on a Raspberry Pi. **Check
now** runs every check at once. `/healthz` stays a bare liveness check and reveals nothing.

## Alerts

When a check gets worse (OK → amber, amber → red), every owner gets one notification in the
bell; when it is back to OK, one recovery notification. Each check alerts at most once a day
per level, so a value that bounces around a threshold does not flood you.

### Push alerts (optional)

To also get alerts on your phone, set **one** push target in `.env` and restart. Lectern only
makes an outbound HTTP(S) request to that server when an alert fires: it opens no port, adds
no callback or webhook receiver, follows no redirect, and needs nothing exposed. A self-hosted
[ntfy](https://ntfy.sh) server on your tailnet is the recommended setup.

- **ntfy**: `LECTERN_ALERT_NTFY_URL` is the topic URL (for example
  `https://ntfy.your-tailnet.ts.net/lectern`), `LECTERN_ALERT_NTFY_TOKEN` an optional access
  token.
- **Webhook** (Gotify and similar): `LECTERN_ALERT_WEBHOOK_URL` receives a JSON POST of
  `title`, `message`, `priority` (0-10), and `level`; `LECTERN_ALERT_WEBHOOK_TOKEN` is sent as
  `Authorization: Bearer`. For Gotify use `https://<gotify>/message` with an application token.

Only Health alerts are pushed, and only the check's name, its level, and a short reason (such
as "4% free (3.1 GiB of 64 GiB)"): never book titles, usernames, worker names, or paths. The
URLs and tokens can only be set in `.env`, are never logged or shown (Health shows the scheme
and host only), and are kept in the encrypted backups' `config/lectern.env` like the rest of
`.env` ([Backups](backups.md)). **Send a test alert** on the Health page sends one sample.

### Offline mode

While [offline mode](offline-mode.md) is on, push alerts are paused: no connection is
attempted at all, alerts stay in the bell, and Health says so. Allowing a tailnet-only push
server in offline mode is not supported.

## For developers

`lectern.health.register_check(name, provider)` adds checks from another module (the restore
drill, for example): `provider(db, settings, now)` returns a `lectern.health.Check` (or
several, or None) with a stable `id`, a `group` (`backups`, `disk`, `jobs`, `workers`, or
`other`), a level, and a short `summary` that is safe to push. It is shown and alerted like the
built-in checks.
