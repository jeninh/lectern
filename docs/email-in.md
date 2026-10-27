# Email books in

Mail an EPUB, PDF, CBZ comic, or DRM-free MOBI/AZW3 to your own Lectern address and it
waits in **Library → Review** like an upload: you check the details, then file it. Nothing
is filed by itself.

Email-in is **off by default**. The owner turns it on by giving Lectern one dedicated
mailbox to read; Lectern then signs in to that mailbox every few minutes over IMAP with TLS
(port 993) and picks up new mail. Lectern runs no mail server and opens no port: the
connection goes out from the server to your mail provider, like a phone's mail app. Nothing
new can reach your Pi.

## Using it

**Settings → Email books in → Get my address** gives you an address like
`lectern.books+3f9c0a1b2d4e5f60@gmail.com`. Send books to it from an allowed sender (the
owner chooses who, see [Who may send books](#who-may-send-books)). Within a few minutes they
appear for review, and the bell tells you.

- **Allowed files:** EPUB, PDF, CBZ, MOBI, AZW3, up to 10 per message, each checked exactly
  like an upload (format, DRM, damaged or oversized archives). Other attachments are
  ignored. A whole message may be at most 25 MB unless the owner changes it.
- **A shelf for emailed books:** choose one (nested shelves too, for example
  *Inboxes / Inbox (email)*) and every book you file from an email lands on it. If that
  shelf is deleted later, books are filed without a shelf and you get a notice; choose
  another one any time.
- **New address:** if your address leaks, **New address** replaces it; mail to the old one
  is rejected from then on. **Turn off** removes it.

Every handled message is moved out of the inbox into one of two folders in the mailbox,
**Processed** or **Rejected** (Lectern creates them; with Gmail they are labels). Mail is
never deleted.

## Who may send books

The owner sets a sender filter in **Admin → Email-in**:

- **Allowlist** (the default): only the listed senders. An empty list accepts nobody.
- **Blocklist**: everyone *except* the listed senders. Anyone who learns an address can
  then send files to it, so Lectern asks you to confirm before switching. The file checks
  and the review step still apply.

Entries are addresses (`alice@example.org`) or whole domains (`@example.org`: exactly
that domain, not its subdomains). Lectern compares the sender's actual address, ignoring
case and the display name, so `"alice@example.org" <someone@else.example>` is
`someone@else.example`.

Mail is also rejected, into **Rejected**, when:

- it is not addressed to exactly one person's Lectern address;
- it has more than one sender, or your mail provider's own check says the sender was forged
  (DMARC fail);
- it is too large, or has no allowed attachment.

The server log names the reason (for example `sender_not_allowed`) but never an address,
subject, file name, or password.

## Setting it up (owner)

You need a **new mailbox used only for Lectern**, at **Gmail** or **Fastmail**, with an app
password. Outlook, Hotmail, and other Microsoft accounts are not supported: Microsoft no
longer allows app passwords for IMAP, only its own sign-in flow, which Lectern does not do.

### Gmail

1. Create a new Google account for Lectern, for example `lectern.books@gmail.com`.
2. Turn on 2-Step Verification for it: <https://myaccount.google.com/signinoptions/twosv>.
3. Make an app password: <https://myaccount.google.com/apppasswords>, name it *Lectern*.
   Google shows 16 letters once; copy them without the spaces.
4. Host `imap.gmail.com`, port `993`, user: the full address.

### Fastmail

1. Create a Fastmail account (or a separate user) for Lectern.
2. Turn on two-step verification (Settings → Privacy & Security), recommended.
3. Settings → Privacy & Security → **Manage app passwords and access** → **New app
   password**, access *Mail (IMAP/POP/SMTP)*. Copy the password.
4. Host `imap.fastmail.com`, port `993`, user: the full address.

### The `.env` keys

Add these to `.env` next to the compose file (never to a tracked file):

```sh
LECTERN_EMAIL_IN=1
LECTERN_EMAIL_IN_HOST=imap.gmail.com
LECTERN_EMAIL_IN_PORT=993
LECTERN_EMAIL_IN_USER=lectern.books@gmail.com
LECTERN_EMAIL_IN_PASSWORD=abcdefghijklmnop
```

Optional, with their defaults:

| Key | Default | What it does |
|---|---|---|
| `LECTERN_EMAIL_IN_ADDRESS` | the user | The mailbox's address, when it differs from the user name |
| `LECTERN_EMAIL_IN_MAILBOX` | `INBOX` | The folder new mail arrives in |
| `LECTERN_EMAIL_IN_POLL_MINUTES` | `5` | How often to check (1 to 1440) |
| `LECTERN_EMAIL_IN_MAX_MB` | `25` | Largest message and attachment, in MB (1 to 200) |
| `LECTERN_EMAIL_IN_SENDER_MODE` | `allowlist` | `allowlist` or `blocklist` |
| `LECTERN_EMAIL_IN_SENDERS` | (empty) | Comma-separated addresses and `@domains` |

Setting `LECTERN_EMAIL_IN_SENDER_MODE` or `LECTERN_EMAIL_IN_SENDERS` puts the sender filter in
`.env`: Admin then shows it read-only (how many entries, not the entries). Leave both empty to
manage the filter in **Admin → Email-in** instead.

Then recreate Lectern so it reads `.env` again (on the Tailscale setup, from the repository
root):

```sh
docker compose --env-file .env -f deploy/tailscale/compose.yaml up -d --build
```

**Admin → Email-in** shows whether it is running. A missing or invalid key keeps email-in off
and is named there and in the log (`email-in is off: missing or invalid in .env: …`); Lectern
itself starts normally. The password is never shown, logged, or sent anywhere but to the
mail server.

## Offline mode

Email-in needs the internet. While [offline mode](offline-mode.md) is on it does not run,
and Settings and Admin say so. Mail waits in the mailbox until offline mode is off.
