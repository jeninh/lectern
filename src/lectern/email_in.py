"""Email-in (issue #67, docs/email-in.md): books mailed to a person's own address wait for
their review, like an upload.

Lectern runs no mail server and listens on nothing new. When the owner configures one
dedicated mailbox in ``.env`` (Gmail or Fastmail, IMAP over TLS with an app password),
:class:`EmailPoller` signs in every few minutes and reads new mail: outbound only, off by
default. A missing or invalid key keeps it off and is named (key names only) in Admin and the
log; it never stops the server (:func:`load`).

Each person who turns it on gets an address on that mailbox, ``<local>+<tag>@<domain>``
(sub-addressing, which Gmail and Fastmail deliver to the same mailbox). A message is accepted
only when

- it is addressed to exactly one person's tag (To, Cc, or the delivery headers);
- its ``From`` holds exactly one address and that address passes the sender filter
  (:class:`SenderFilter`: an allowlist, the default, or an explicit opt-in blocklist; entries
  are addresses or whole ``@domain`` names, matched case-insensitively on the address itself,
  never the display name);
- the mailbox provider's own DMARC verdict (the topmost ``Authentication-Results``) is not
  ``fail``, so a forged ``From`` of an allowed sender is refused;
- it is at most ``LECTERN_EMAIL_IN_MAX_MB`` (asked for before anything is downloaded; only the
  headers of a message are read before it passes the checks above);
- the person may still upload (owner or member, active).

Attachments in the allowed types (:data:`ALLOWED_TYPES`: EPUB, PDF, CBZ, MOBI, AZW3, by file
name and magic bytes) go through the same staging and validation as an upload
(:func:`lectern.imports.stage_file`) into one new import of that person, which waits for review:
email-in never files anything by itself. With a destination shelf chosen, the book is put on it
when it is filed; a shelf deleted meanwhile is skipped with a notice.

Each handled message is moved to the ``Processed`` or ``Rejected`` folder (created when
missing); mail is never deleted. Logs carry reason codes and counts only: no addresses,
subjects, file names, host names, or credentials. Disabled while offline mode (#31) is on.
"""

from __future__ import annotations

import hashlib
import imaplib
import logging
import re
import secrets
import ssl
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from lectern import egress
from lectern.config import Settings
from lectern.models import EmailInbox, InstanceSetting, Role, Shelf, User, utcnow

log = logging.getLogger("lectern.email_in")

STATE_KEY = "email_in_state"  # instance_settings: {"uidvalidity", "last_uid"}
FILTER_KEY = "email_in_senders"  # instance_settings: {"mode", "entries"} set in Admin
PROCESSED, REJECTED = "Processed", "Rejected"
ALLOWLIST, BLOCKLIST = "allowlist", "blocklist"
TOKEN_RE = re.compile(r"^[a-z0-9]{12,32}$")
MAX_FILTER_ENTRIES = 100
MAX_ATTACHMENTS = 10
MAX_MESSAGES_PER_POLL = 25
IMAP_TIMEOUT_S = 60
#: File types taken from mail, by extension (the magic bytes are checked too).
ALLOWED_TYPES = (".epub", ".pdf", ".cbz", ".mobi", ".azw3")
RECIPIENT_HEADERS = (
    "to",
    "cc",
    "delivered-to",
    "x-original-to",
    "x-delivered-to",
    "envelope-to",
    "x-envelope-to",
)
_ATOM = r"[^@\s\"<>()\[\],;:\\]+"
_ADDRESS = re.compile(rf"^{_ATOM}@{_ATOM}\.{_ATOM}$")
_DOMAIN = re.compile(rf"^@{_ATOM}\.{_ATOM}$")
_TRUE = ("1", "true", "yes", "on")


# --- configuration ------------------------------------------------------------------------------


@dataclass(frozen=True)
class SenderFilter:
    """Who may mail books in. ``entries``: lowercased ``name@domain`` or ``@domain``."""

    mode: str = ALLOWLIST
    entries: tuple[str, ...] = ()
    source: str = "app"  # "env" (read-only in Admin) or "app"

    def check(self, address: str | None) -> str | None:
        """None when ``address`` may send, else the reason code it is refused with."""
        if address is None:
            return "sender_unclear"
        domain = "@" + address.rpartition("@")[2]
        listed = address in self.entries or domain in self.entries
        if self.mode == BLOCKLIST:
            return "sender_blocked" if listed else None
        return None if listed else "sender_not_allowed"


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    address: str
    mailbox: str
    poll_s: float
    max_bytes: int


def normalize_sender(value: str) -> str:
    """An address as it is compared (lowercased); ``ValueError`` if it is not one."""
    pairs = getaddresses([value.strip()])
    if len(pairs) != 1 or not _ADDRESS.match(pairs[0][1]) or len(pairs[0][1]) > 254:
        raise ValueError("not an email address")
    return pairs[0][1].lower()


def normalize_entry(value: str) -> str:
    """A filter entry: an address, or ``@domain`` for a whole domain."""
    v = value.strip().lower()
    if v.startswith("@"):
        if not _DOMAIN.match(v) or len(v) > 254:
            raise ValueError("not a domain")
        return v
    return normalize_sender(v)


def parse_entries(raw: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[,\s;]+", raw):
        if part:
            e = normalize_entry(part)
            if e not in out:
                out.append(e)
    return out


def _int(raw: str, default: int, lo: int, hi: int) -> int | None:
    raw = raw.strip()
    if not raw:
        return default
    return int(raw) if raw.isdigit() and lo <= int(raw) <= hi else None


def load(settings: Settings) -> tuple[Config | None, list[str]]:
    """The mailbox configuration, or None with the names of the keys that are missing or
    invalid. Off (``LECTERN_EMAIL_IN`` not on) is ``(None, [])``. Never raises."""
    if settings.email_in.strip().lower() not in _TRUE:
        return None, []
    problems: list[str] = []
    host = settings.email_in_host.strip()
    user = settings.email_in_user.strip()
    password = settings.email_in_password.get_secret_value()
    for key, value in (("HOST", host), ("USER", user), ("PASSWORD", password)):
        if not value:
            problems.append(f"LECTERN_EMAIL_IN_{key}")
    port = _int(settings.email_in_port, 993, 1, 65535)
    if port is None:
        problems.append("LECTERN_EMAIL_IN_PORT")
    address = settings.email_in_address.strip().lower() or (user.lower() if "@" in user else "")
    if not _ADDRESS.match(address) or "+" in address.partition("@")[0]:
        problems.append("LECTERN_EMAIL_IN_ADDRESS")
    minutes = _int(settings.email_in_poll_minutes, 5, 1, 1440)
    if minutes is None:
        problems.append("LECTERN_EMAIL_IN_POLL_MINUTES")
    max_mb = _int(settings.email_in_max_mb, 25, 1, 200)
    if max_mb is None:
        problems.append("LECTERN_EMAIL_IN_MAX_MB")
    mailbox = settings.email_in_mailbox.strip() or "INBOX"
    if len(mailbox) > 200 or any(ord(c) < 0x20 for c in mailbox):
        problems.append("LECTERN_EMAIL_IN_MAILBOX")
    try:
        _env_filter(settings)
    except ValueError as exc:
        problems.append(str(exc))
    if problems:
        return None, problems
    return Config(
        host=host,
        port=port,
        user=user,
        password=password,
        address=address,
        mailbox=mailbox,
        poll_s=minutes * 60.0,
        max_bytes=max_mb * 1024 * 1024,
    ), []


def _env_filter(settings: Settings) -> SenderFilter | None:
    """The sender filter from ``.env``, when one is set there. ``ValueError(key)`` if invalid."""
    mode = settings.email_in_sender_mode.strip().lower()
    raw = settings.email_in_senders.strip()
    if not mode and not raw:
        return None
    if mode not in ("", ALLOWLIST, BLOCKLIST):
        raise ValueError("LECTERN_EMAIL_IN_SENDER_MODE")
    try:
        entries = parse_entries(raw)
    except ValueError:
        raise ValueError("LECTERN_EMAIL_IN_SENDERS") from None
    if len(entries) > MAX_FILTER_ENTRIES:
        raise ValueError("LECTERN_EMAIL_IN_SENDERS")
    return SenderFilter(mode or ALLOWLIST, tuple(entries), "env")


def sender_filter(db: Session, settings: Settings) -> SenderFilter:
    """``.env`` wins (Admin shows it read-only); else what the owner saved in Admin; else an
    empty allowlist, which accepts nobody."""
    try:
        env = _env_filter(settings)
    except ValueError:
        env = SenderFilter(ALLOWLIST, (), "env")  # invalid: load() keeps email-in off anyway
    if env is not None:
        return env
    row = db.get(InstanceSetting, FILTER_KEY)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    mode = value.get("mode") if value.get("mode") in (ALLOWLIST, BLOCKLIST) else ALLOWLIST
    return SenderFilter(mode, tuple(value.get("entries") or ()), "app")


def save_filter(db: Session, mode: str, entries: list[str], user_id: int) -> None:
    row = db.get(InstanceSetting, FILTER_KEY)
    value = {"mode": mode, "entries": entries}
    if row is None:
        db.add(InstanceSetting(key=FILTER_KEY, value=value, updated_by=user_id))
    else:
        row.value, row.updated_by = value, user_id
    db.flush()


def configured(settings: Settings) -> bool:
    return load(settings)[0] is not None


def unavailable(settings: Settings) -> str | None:
    """Why email-in does not run: ``off``, ``not_configured``, ``offline``, or None."""
    cfg, problems = load(settings)
    if cfg is None:
        return "not_configured" if problems else "off"
    return "offline" if egress.offline() else None


def new_token() -> str:
    return secrets.token_hex(8)  # 16 characters, 64 bits


def address_for(cfg: Config, token: str) -> str:
    local, _, domain = cfg.address.partition("@")
    return f"{local}+{token}@{domain}"


# --- one message ---------------------------------------------------------------------------------


def recipient_tokens(msg: EmailMessage, cfg: Config) -> set[str]:
    """The tags of every ``<local>+<tag>@<domain>`` the message is addressed to."""
    local, _, domain = cfg.address.partition("@")
    values = [str(v) for h in RECIPIENT_HEADERS for v in (msg.get_all(h) or [])]
    found = set()
    for _name, addr in getaddresses(values):
        user, _, host = addr.lower().rpartition("@")
        if host == domain and user.startswith(local + "+"):
            tag = user[len(local) + 1 :]
            if TOKEN_RE.match(tag):
                found.add(tag)
    return found


def sender_of(msg: EmailMessage) -> str | None:
    """The one ``From`` address, lowercased; None when there is not exactly one."""
    values = [str(v) for v in (msg.get_all("from") or [])]
    pairs = [a for _n, a in getaddresses(values) if a]
    if len(pairs) != 1:
        return None
    try:
        return normalize_sender(pairs[0])
    except ValueError:
        return None


def dmarc_failed(msg: EmailMessage) -> bool:
    """The receiving provider's verdict: the topmost ``Authentication-Results`` (added by the
    mailbox's own servers, above anything a sender could put in) says ``dmarc=fail``."""
    first = (msg.get_all("authentication-results") or [None])[0]
    return first is not None and re.search(r"\bdmarc\s*=\s*fail\b", str(first), re.I) is not None


@dataclass
class Attachment:
    filename: str
    media_type: str
    data: bytes


def book_attachments(msg: EmailMessage, max_bytes: int) -> list[Attachment]:
    """Attachments of an allowed type, judged by file name (mail clients often send
    ``application/octet-stream``); the magic bytes and the format are checked when staged."""
    from lectern.ingest import formats

    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = Path(filename.replace("\\", "/")).name.strip()[:200]
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_TYPES:
            continue
        data = part.get_payload(decode=True)
        if data and len(data) <= max_bytes:
            out.append(Attachment(filename, formats.BY_SUFFIX[suffix], data))
        if len(out) >= MAX_ATTACHMENTS:
            break
    return out


class _Busy(Exception):
    """A parser slot stayed taken: leave the message for the next poll."""


class _NoBooks(Exception):
    pass


def check(db: Session, settings: Settings, cfg: Config, msg: EmailMessage) -> tuple:
    """``(user, None)`` when the message passes every check on its headers, else
    ``(None, reason)``."""
    tokens = recipient_tokens(msg, cfg)
    if not tokens:
        return None, "not_for_anyone"
    if len(tokens) > 1:
        return None, "several_addresses"
    inbox = db.scalar(select(EmailInbox).where(EmailInbox.token == next(iter(tokens))))
    if inbox is None:
        return None, "unknown_address"
    reason = sender_filter(db, settings).check(sender_of(msg))
    if reason is not None:
        return None, reason
    if dmarc_failed(msg):
        return None, "dmarc_fail"
    user = db.get(User, inbox.user_id)
    if user is None or not user.is_active or user.role not in (Role.OWNER, Role.MEMBER):
        return None, "cannot_upload"
    return user, None


def destination_shelf(db: Session, inbox: EmailInbox, user: User) -> Shelf | None:
    """The shelf emailed books go on, if it still exists and ``user`` may add to it."""
    from lectern.access import can_edit_shelf, takes_items

    if inbox.shelf_id is None:
        return None
    shelf = db.get(Shelf, inbox.shelf_id)
    if shelf is None or not takes_items(shelf) or not can_edit_shelf(db, user, shelf):
        return None
    return shelf


def shelf_gone_notice(db: Session, user: User) -> None:
    """Tell ``user`` (at most once a day) that their email-in shelf is gone."""
    from lectern import notify

    if notify.recently_sent(db, user.id, "email_in_shelf", "gone", timedelta(days=1)):
        return
    notify.notify(
        db,
        user,
        "email_in_shelf",
        "The shelf for emailed books is gone",
        "Emailed books are not put on a shelf until you choose another one in Settings → "
        "Email books in.",
        severity="warning",
        data={"key": "gone"},
    )


def deliver(db: Session, settings: Settings, cfg: Config, raw: bytes) -> str:
    """Handle one message: ``accepted``, ``busy`` (try again later), or a rejection reason."""
    from lectern import imports, notify
    from lectern.fsops import LIBRARY_LOCK
    from lectern.ingest import formats
    from lectern.security import audit
    from lectern.uploads import uploads_dir

    msg = BytesParser(policy=policy.default).parsebytes(raw)
    user, reason = check(db, settings, cfg, msg)
    if user is None:
        return reason
    attachments = book_attachments(msg, cfg.max_bytes)
    if not attachments:
        return "no_books"
    inbox = db.get(EmailInbox, user.id)
    shelf = destination_shelf(db, inbox, user)
    subject = " ".join(str(msg.get("subject") or "").split())[:200]
    imp = None
    staged = 0
    with LIBRARY_LOCK:
        try:
            imp, _ = imports.create(db, user, subject or attachments[0].filename, None)
            imp.shelf_id = shelf.id if shelf is not None else None
            for att in attachments:
                part = uploads_dir(settings) / f"email-{secrets.token_hex(8)}.part"
                try:
                    part.write_bytes(att.data)
                    if not formats.magic_ok(att.media_type, formats.read_head(part)):
                        continue
                    imports.stage_file(
                        db, settings, imp, part, filename=att.filename,
                        media_type=att.media_type, size=len(att.data),
                        sha256=hashlib.sha256(att.data).hexdigest(),
                    )  # fmt: skip
                    staged += 1
                except imports.ImportError_ as exc:
                    if exc.code in formats.BUSY_CODES:
                        raise _Busy() from None
                    # unreadable, DRM-protected, ...: the rest of the message still counts
                finally:
                    part.unlink(missing_ok=True)
            if not staged:
                raise _NoBooks()
            imports.complete(db, settings, user, imp)  # never auto-filed
            # No subject, file name, or sender: the audit row is mirrored to the log.
            audit.record(
                db,
                "email_in.receive",
                actor_user_id=user.id,
                target=f"import:{imp.public_id}",
                detail=f"{staged} file(s)",
            )
            if shelf is None and inbox.shelf_id is not None:
                shelf_gone_notice(db, user)  # filed without a shelf; say why
            n = "One book" if staged == 1 else f"{staged} books"
            notify.notify(
                db,
                user,
                "email_in",
                f"{n} arrived by email",
                "Check the details in Library → Review before they are filed.",
                data={"import": imp.public_id},
            )
            db.commit()
        except (_Busy, _NoBooks) as exc:
            db.rollback()
            if imp is not None:
                imports.cleanup_staging(settings, imp)
            return "busy" if isinstance(exc, _Busy) else "no_books"
        except BaseException:
            db.rollback()
            if imp is not None:
                imports.cleanup_staging(settings, imp)
            raise
    return "accepted"


# --- polling ----------------------------------------------------------------------------------

_status_lock = threading.Lock()
_status: dict = {"at": None, "ok": None, "error": None, "counts": {}}


def last_poll() -> dict:
    with _status_lock:
        return {**_status, "counts": dict(_status["counts"])}


def _record(ok: bool, error: str | None = None, counts: Counter | None = None) -> None:
    with _status_lock:
        _status.update(at=utcnow().isoformat(), ok=ok, error=error, counts=dict(counts or {}))


def connect(cfg: Config) -> imaplib.IMAP4:
    """IMAP over TLS (implicit, port 993 by default), the certificate checked."""
    conn = imaplib.IMAP4_SSL(
        cfg.host, cfg.port, ssl_context=ssl.create_default_context(), timeout=IMAP_TIMEOUT_S
    )
    try:
        conn.login(cfg.user, cfg.password)
    except BaseException:
        _logout(conn)
        raise
    return conn


def _logout(conn) -> None:
    try:
        conn.logout()
    except Exception:  # noqa: BLE001, S110 - the connection is going away either way
        pass


def _quote(mailbox: str) -> str:
    return '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _state(db: Session) -> dict:
    row = db.get(InstanceSetting, STATE_KEY)
    return row.value if row is not None and isinstance(row.value, dict) else {}


def _save_state(db: Session, uidvalidity: int, last_uid: int) -> None:
    row = db.get(InstanceSetting, STATE_KEY)
    value = {"uidvalidity": uidvalidity, "last_uid": last_uid}
    if row is None:
        db.add(InstanceSetting(key=STATE_KEY, value=value))
    else:
        row.value = value
    db.commit()


def _size(data) -> int | None:
    for item in data or []:
        text = item[0] if isinstance(item, tuple) else item
        if isinstance(text, bytes):
            m = re.search(rb"RFC822\.SIZE (\d+)", text)
            if m:
                return int(m.group(1))
    return None


def _headers(raw: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)


def _body(data) -> bytes | None:
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


def _file_away(conn, uid: int, folder: str) -> None:
    """Move the message to ``folder`` (MOVE, which Gmail and Fastmail have). Without MOVE it
    is copied there and left in place, marked seen: nothing is ever deleted."""
    capabilities = getattr(conn, "capabilities", ()) or ()
    if "MOVE" in capabilities:
        typ, _ = conn.uid("MOVE", str(uid), _quote(folder))
        if typ == "OK":
            return
    conn.uid("COPY", str(uid), _quote(folder))
    conn.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")


def _handle(conn, session_factory, settings: Settings, cfg: Config, uid: int) -> str:
    _t, data = conn.uid("FETCH", str(uid), "(RFC822.SIZE)")
    size = _size(data)
    if size is None:
        return "unreadable"
    if size > cfg.max_bytes:
        return "too_large"
    # The headers first: mail that fails a check is never downloaded.
    _t, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[HEADER])")
    head = _body(data)
    if head is None:
        return "unreadable"
    with session_factory() as db:
        user, reason = check(db, settings, cfg, _headers(head))
    if user is None:
        return reason
    _t, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
    raw = _body(data)
    if raw is None:
        return "unreadable"
    with session_factory() as db:
        return deliver(db, settings, cfg, raw)


def poll_once(session_factory: sessionmaker, settings: Settings, connect=connect) -> Counter:
    """Read new mail once. Returns what happened to each message (also kept for
    :func:`last_poll`)."""
    counts: Counter = Counter()
    cfg, _problems = load(settings)
    reason = unavailable(settings)
    if cfg is None or reason is not None:
        _record(False, reason)
        return counts
    try:
        conn = connect(cfg)
    except imaplib.IMAP4.error:
        _record(False, "login_failed")
        log.warning("email-in: could not sign in to the mailbox (check the app password)")
        return counts
    except OSError:
        _record(False, "offline" if egress.offline() else "connection_failed")
        log.warning("email-in: could not reach the mail server")
        return counts
    try:
        typ, _ = conn.select(_quote(cfg.mailbox))
        if typ != "OK":
            _record(False, "no_mailbox")
            log.warning("email-in: the configured mailbox folder does not exist")
            return counts
        for folder in (PROCESSED, REJECTED):
            conn.create(_quote(folder))  # "already exists" is fine
        _t, data = conn.response("UIDVALIDITY")
        uidvalidity = int((data or [b"0"])[0] or 0)
        with session_factory() as db:
            state = _state(db)
        last = state.get("last_uid", 0) if state.get("uidvalidity") == uidvalidity else 0
        typ, data = conn.uid("SEARCH", None, f"UID {last + 1}:*")
        uids = sorted(int(u) for u in (data[0].split() if data and data[0] else []))
        # "n:*" always includes the highest UID, even one below n
        for uid in [u for u in uids if u > last][:MAX_MESSAGES_PER_POLL]:
            try:
                outcome = _handle(conn, session_factory, settings, cfg, uid)
            except (imaplib.IMAP4.error, OSError):
                raise  # the connection, not this message
            except Exception as exc:  # noqa: BLE001 - one bad message must not wedge the rest
                # Anyone can mail the mailbox, and the parser can be made to raise; such a
                # message is rejected and passed like any other. The type only: messages
                # may quote the mail.
                log.warning("email-in: could not handle a message: %s", type(exc).__name__)
                outcome = "malformed"
            if outcome == "busy":
                counts["busy"] += 1
                break  # the same message again next time
            _file_away(conn, uid, PROCESSED if outcome == "accepted" else REJECTED)
            if outcome != "accepted":
                log.info("email-in: rejected a message: %s", outcome)
            counts[outcome] += 1
            with session_factory() as db:
                _save_state(db, uidvalidity, uid)
    except (imaplib.IMAP4.error, OSError):
        _record(False, "connection_failed", counts)
        log.warning("email-in: the mail server connection failed during a poll")
        return counts
    finally:
        _logout(conn)
    _record(True, None, counts)
    if counts:
        log.info("email-in: %s", ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    return counts


class EmailPoller:
    """The background thread: one :func:`poll_once` every ``LECTERN_EMAIL_IN_POLL_MINUTES``
    (the first one right after start)."""

    def __init__(self, session_factory: sessionmaker, settings: Settings, cfg: Config) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.cfg = cfg
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lectern-email-in", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                poll_once(self.session_factory, self.settings)
            except Exception as exc:  # noqa: BLE001 - the type only: messages may quote mail
                _record(False, "error")
                log.warning("email-in poll failed: %s", type(exc).__name__)
            self._stop.wait(self.cfg.poll_s)


def start(session_factory: sessionmaker, settings: Settings) -> EmailPoller | None:
    """The poller when email-in is configured; otherwise None, with the missing or invalid
    keys named in the log (never their values)."""
    cfg, problems = load(settings)
    if problems:
        log.warning("email-in is off: missing or invalid in .env: %s", ", ".join(problems))
    if cfg is None:
        return None
    return EmailPoller(session_factory, settings, cfg)
