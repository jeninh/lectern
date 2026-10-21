# Backups and restore

Lectern can back itself up every night. **Backups are off by default.** The owner turns them
on under **Admin → Nightly backups** and chooses the time, how many to keep, and whether
library files are included. Where they go is deployment configuration
(`LECTERN_BACKUP_PATH` in `.env`), not a web setting, so nobody can redirect your backups
from a browser.

![Admin → Nightly backups](screenshots/backups-admin.png)

## What a backup contains

Every backup is a snapshot in an encrypted, deduplicated [restic](https://restic.net)
repository at `<backup folder>/restic`. restic ships in the Docker image. After the first
night only changes are stored, which suits a large audiobook library.

| Part | Always | Notes |
| --- | --- | --- |
| Database (`lectern.db`) | yes | SQLite's **online backup API**: a consistent copy while Lectern keeps running, WAL included. No stop, no torn file. Checked with `PRAGMA integrity_check` before it is kept. Copied together with the parts below under the library lock, so an edit cannot land between them. |
| Secrets | yes | the signing key (`secret_key`) and the repository password |
| Config | yes | the `LECTERN_*` settings the server ran with (`config/lectern.env`) and a JSON summary without the signing key |
| Sentence maps and Undo cover backups | yes | `data/align/` (hours of transcription to redo) and `data/revisions/` |
| Cover and book metadata | yes | each book's `book.json` and cover art |
| Library files | optional | EPUBs, PDFs, comics, audiobooks. `_inbox/`, the regenerable `derived/` folders, and symlinks are left out. Read after the lock is released, so a long backup never blocks adding books; a book filed meanwhile is in the next one. |

Not included: uploads in progress, scratch space, logs, downloaded models, the Tailscale node
state (back that up as root; see [the Tailscale guide](deploy-tailscale.md#backups-and-restore)),
and your host `.env` (keep it in your password manager; `config/lectern.env` helps rebuild it).

**The repository password** is created on first use in
`data/lectern/backup-restic-password` (or `LECTERN_BACKUP_RESTIC_PASSWORD_FILE`). **Copy it
to your password manager now**: without it the backups are unreadable. A copy inside the
backups does not help, because they are encrypted with it.

Retention is `restic forget --keep-last N --prune` after every backup. A file restic cannot
read (wrong permissions, say) is left out of that snapshot; the backup still counts and is
pruned like the others, and the Admin card names the file so you can fix it.

## Setting it up on a Raspberry Pi with a USB drive

1. Plug in the drive and mount it at a fixed place, e.g. `/media/usb` (add it to `/etc/fstab`
   with `nofail` so the Pi still boots without it).
2. Create the folder, owned by Lectern's user:

   ```sh
   sudo install -d -o "$(id -u)" -g "$(id -g)" -m 700 /media/usb/lectern-backups
   ```

3. In `~/lectern/.env`: `LECTERN_BACKUP_PATH=/media/usb/lectern-backups`, then
   `lectern-compose up -d`.
4. **Admin → Nightly backups**: turn it on, pick a time, press **Back up now** once and check
   that *Last success* fills in. Then save the repository password (above).

Without `LECTERN_BACKUP_PATH`, backups go to `backups/` in the checkout (git-ignored).
Compose creates the folder if it is missing; if it was created as root, `chown` it to
`LECTERN_UID:LECTERN_GID`. The Admin card says when the folder is missing, not writable, or
on the same disk as Lectern's data.

The snapshot is staged in `<backup folder>/.staging/` (never in the data directory, so the
card or disk Lectern runs from only sees reads) and removed after each run. A run that was
killed part-way leaves it behind; the next run removes it before it starts.

## SD-card wear

SD cards wear out with writes and fail without warning, often taking everything on them.
On a Pi booting from an SD card:

- **Do not back up to the SD card.** A backup on the same card dies with it, and nightly
  writes shorten its life. Use a USB drive or SSD (Lectern warns when the backup folder is on
  the same disk as its data).
- Prefer keeping `data/` and `library/` on a USB SSD too (`LECTERN_DATA_PATH`,
  `LECTERN_LIBRARY_PATH`); SQLite writes on every sync and page turn.
- restic writes only what changed, so including library files costs little after the first
  night.
- A backup reads the library in full; reads do not wear flash.
- Keep a second copy off the Pi (another machine on the tailnet, cloud storage, a drive you
  rotate). One USB drive next to the Pi protects against a dead SD card, not against a fire or
  a power surge.

## Status and alerts

The Admin card shows the last success (time, snapshot, size), whether one is running, free
space, files that could not be read, and the last error.

![A failed backup](screenshots/backups-failed.png)

When a backup fails, every owner gets a warning notification (at most once a day), and
earlier snapshots stay as they were. If a backup is already running (say you started one
from the command line during the nightly run), the second one is skipped, not counted as a
failure. A server that was off at backup time catches up once when it starts.

From the host you can also run one by hand or list the snapshots:

```sh
lectern-compose exec lectern lectern backup
lectern-compose exec lectern lectern backup --list
```

## Restore

`lectern restore [SNAPSHOT]` restores a snapshot (the newest by default) into the data
directory and library. It:

- **refuses while Lectern is running** on that data directory;
- verifies the database copy and refuses a backup made by a newer Lectern (update first);
- treats the backup's contents as untrusted: it never follows symlinks and refuses a manifest
  that points outside the backup;
- copies everything next to its destination first, then swaps it in. Every file it replaces,
  library files included, is moved into `data/lectern/pre-restore-<time>/` (library files
  under `library/`). Library files are never deleted;
- **rolls back** if anything fails part-way (a full disk, an I/O error): the server is left
  exactly as it was, and the error says so.

**Free space.** The restored files are staged next to their destinations, so the data and
library volumes each need room for what is being restored. Replaced library files are kept in
`data/lectern/pre-restore-<time>/library/`, on the *data* volume: when that is a Pi's SD card,
check `df -h` first, and expect the copy to take a while and add wear if many large library
files differ from the backup. Use `--no-library` to restore the data side alone, and delete
`pre-restore-*` once you are happy.

A crash or power loss in the middle of the swap is not recovered automatically: the files
already swapped in stay, and the ones they replaced are in `pre-restore-<time>/`. Run the
restore again.

The server migrates the schema on its next start. With the Tailscale compose file
(`lectern-compose` is the alias from the guide):

```sh
cd ~/lectern
lectern-compose stop lectern
lectern-compose run --rm --no-deps lectern lectern backup --list
lectern-compose run --rm --no-deps lectern lectern restore            # the newest
lectern-compose run --rm --no-deps lectern lectern restore 1a2b3c4d   # or a given one
lectern-compose start lectern
```

`run --rm --no-deps` starts a one-off container with the same user and mounts as the server;
it joins the still-running Tailscale container's network but serves nothing. Options:

- `--no-library`: restore the data side only (database, secrets, maps); leave library files.
- `--restic-password-file PATH`: on a fresh machine, the repository password you saved. Put it
  in the backup folder or data folder first so the container can read it.

After a restore, compare `data/lectern/restored-lectern.env` with your `.env`. Once you are
happy, delete `data/lectern/pre-restore-*`.

**On a new Pi:** clone, write `.env` (same `LECTERN_BASE_URL`, so passkeys keep working),
create `data/lectern` and `library`, mount the USB drive, put the saved password in
`data/lectern/backup-restic-password` (mode 600), run the restore above, then
`lectern-compose up -d --build`. Restore the Tailscale node state as the guide describes, or
remove the old machine and use a fresh auth key.

The restore path is tested in CI: `tests/test_backups.py` backs up a running server (with and
without library files), changes and deletes things, runs `lectern restore`, and signs in to
the restored server. It also covers a crafted manifest, symlinks, unreadable files, a busy
target, and a restore that fails half-way.
