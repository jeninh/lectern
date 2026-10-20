# Sending books to a device

"Send to device" puts a book at the top of one e-reader's OPDS catalogue, under **Queued for
this device**. The e-reader downloads it from there like any catalogue book, and the queue
forgets it once that device has the whole EPUB. Nothing is pushed: e-ink devices only ever
pull, so the queue is just a personal shelf for one catalogue password.

![Devices with their queues](screenshots/m5-queues-devices.png)

## Using it

- **One book:** open it in the Library and choose **Send to device**. The dialog lists the
  devices you can send to, which ones already have the book waiting, and remembers your last
  choice.
- **Several books:** in the Library choose **Select**, tap the books (books without an EPUB
  cannot be selected), then **Send to device…**. The toast says what happened to each book:
  sent, already waiting, not shared with that person, or without an EPUB.
- **The queue:** on **Devices**, each device with a catalogue password shows how many books
  are waiting; **Open queue** lists them in catalogue order. Move them up or down, remove one,
  or queue a delivered or removed book again from **Recently**.
- **On the e-reader:** open the Lectern catalogue. The root lists, in this order:
  1. **Continue reading**: what you are reading or listening to, most recent first;
  2. **Up next**: books on your shelves you have not started;
  3. **Queued for this device**: what was sent to *this* device;
  4. **All books** and **Recently added**, as before.

  Each entry says how many books it holds ("2 books sent to Kobo Libra 2"), so a slow e-ink
  screen shows what is new without opening anything.

| Desktop | Phone (dark) |
| --- | --- |
| ![Queue sheet](screenshots/m5-queues-sheet.png) | ![Queue sheet on a phone](screenshots/m5-queues-sheet-mobile-dark.png) |
| ![Send dialog](screenshots/m5-queues-send-dialog.png) | ![Selecting books on a phone](screenshots/m5-queues-library-select-mobile.png) |

## Who can send what to whom

A queue belongs to one OPDS **app password** (one device), not to a person: your Kobo and
your phone have separate queues, and nobody else's catalogue ever shows yours.

| | Own devices | Other people's devices |
| --- | --- | --- |
| Owner | send, reorder, remove | send, reorder, remove (household administration) |
| Member | send, reorder, remove | invisible (404) |
| Reader | send, reorder, remove | invisible (404) |

- A book can be sent only if the sender can see it **and** the device's user can see it. The
  owner cannot put a book on a reader's e-reader that is not on a shelf shared with that
  reader (`not_visible_to_device`).
- Visibility is checked again whenever a queue is read. Unsharing a shelf hides its books from
  the reader's queue (feed and web) and its downloads answer 404, as everywhere else; sharing
  it again brings them back.
- A revoked device's catalogue closes (401) and nothing more can be sent to it; its queue can
  still be viewed and tidied.
- Only works with an EPUB can be queued (`no_ebook`).

## Delivery: when a queued book counts as downloaded

An item becomes **delivered** when *that exact device* (the app password that signed the
request) has been sent **every byte of an EPUB of that work in one response**:

| Request from the queued device | Delivers? |
| --- | --- |
| `GET /opds/files/{id}/download` (or `/library/files/{id}/download` with the same Basic credentials), `200`, body fully sent | **yes** |
| `Range: bytes=0-` answered `206` with the whole file | **yes** |
| any other range (`bytes=0-99`, `bytes=100-`, `bytes=-500`), even if the pieces add up to the file | no |
| `If-None-Match` with the file's ETag (its SHA-256): `304`, not counted against the caps either | no |
| `HEAD` (`405`), `416`, `404` (not visible), `429` (download cap), or a transfer the client abandons | no |
| the same book downloaded by another device, the web app, or another person | no |

"Sent" means the server handed the last block to the connection (the response body generator
resumed after it), the same point at which streaming bytes are metered. The update is one
`UPDATE … WHERE status = 'pending'`, so concurrent downloads deliver and audit once. A delivered
book stays downloadable from **All books**; **Queue again** puts it back at the end.

Downloads keep their caps and audit rows: every whole-file `GET` counts against the hourly
caps as before, including ranges; only a `304` is now free, since it transfers nothing.

## Ordering and idempotency

- Queues are first in, first out. **Queue again** moves a book to the end; reordering
  rewrites positions for the whole visible queue at once and is refused (`409
  queue_changed`) if the queue changed since the client loaded it.
- There is one row per device and work (`uq_device_queue_device_work`). Sending a book that is
  already waiting changes nothing (`already_queued`); sending a delivered or removed one makes
  the same row pending again (`requeued`). The insert is `INSERT … ON CONFLICT DO UPDATE …
  WHERE status <> 'pending'`, so two people sending the same book at once leave one item.
- The audit log records `queue.add`, `queue.remove`, and `queue.delivered` (Activity shows
  them).

## What "Continue reading" and "Up next" mean

Both are computed from data Lectern already keeps (`lectern.reading_lists`), only for works the
person can see:

- **Continue reading** is exactly the web app's Continue list (`GET /api/continue`): works with
  a KOReader progress record for any of their EPUBs, or a canonical position from the web
  reader, read-along, or audio player, most recent activity first (newest 50). Finished books
  stay until newer activity pushes them down, as on Home.
- **Up next** is the books on the person's shelves (their own shelves by name, then shelves
  shared with them by name; each in shelf order) that are in neither source of Continue, so
  starting a book moves it from Up next to Continue. Someone without shelves has nothing up
  next.

## API

All under the session cookie; writes need the CSRF token and a same-origin request.

| Route | |
| --- | --- |
| `GET /api/device-queues[?work_id=]` | devices you may send to (`own` first; the owner also gets everyone's), each with `pending`, and `queued` for `work_id` |
| `GET /api/device-queues/{app_password_id}` | `pending` (catalogue order) and `history` (latest 50 delivered or removed) |
| `POST /api/device-queues/{app_password_id}/items` `{"work_ids": [...]}` (≤ 200) | per-work `status`: `queued`, `requeued`, `already_queued`, `not_found`, `not_visible_to_device`, `no_ebook` |
| `DELETE /api/device-queues/{app_password_id}/items/{item_id}` | `{"removed": true}`; `false` if it was not pending |
| `POST /api/device-queues/{app_password_id}/order` `{"item_ids": [...]}` | exactly the visible pending items; `409 queue_changed` otherwise |

OPDS (HTTP Basic with the device's app password; the per-IP Basic rate limit applies):

| Route | |
| --- | --- |
| `GET /opds` | navigation feed: Continue reading, Up next, Queued for this device, All books, Recently added |
| `GET /opds/continue`, `/opds/up-next`, `/opds/queue` | acquisition feeds, 50 per page with `next`/`previous` links and OpenSearch counts; entries are identical to `/opds/all` (same `urn:lectern:work:<id>` ids and acquisition links) |

## Storage

`device_queue_items` (migration `0007`): `app_password_id`, `user_id` (the device's owner),
`work_id`, `status` (`pending`/`delivered`/`removed`), `position`, `queued_by`, `queued_at`,
`delivered_at`, `delivered_file_id`, `removed_at`, `removed_by`, `updated_at`. Deleting a
device's app password, a user, or a work deletes their items.
