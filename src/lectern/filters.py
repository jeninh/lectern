"""Library filters: one syntax for the Library, Cmd/Ctrl+K, smart shelves, OPDS, and MCP.

A filter is a list of clauses, ANDed, plus free words::

    format:epub,pdf -has:audio author:"Tolstoy" year:1850..1900 progress:<50% war

- ``key:value`` is a clause; ``key:a,b`` matches either value (OR within a clause); a leading
  ``-`` negates it ("is not"): everything visible that the clause does not match, books with
  no value for the field included.
- Numbers, sizes, lengths, percentages, and dates take ``<``, ``<=``, ``>``, ``>=``, or a
  range ``a..b`` (either end may be left open). Dates are ``2026``, ``2026-03``,
  ``2026-03-15`` (UTC), or ages: ``30d``, ``2w``, ``6m``, ``1y`` (``added:<30d``: added less
  than 30 days ago).
- Anything else is a free word, matched against title, author, series, and ISBN (accents and
  case ignored); ``-word`` excludes it. A ``key:`` Lectern does not know is a free word too,
  so a title like ``Re:Zero`` still finds itself. Quote a value or word that has spaces,
  commas, or quotes in it (``\\"`` inside quotes).

:func:`parse` reads that syntax into a :class:`Query` (validating each value for its field)
and :func:`format_query` writes it back canonically; ``frontend/src/lib/filters.ts`` is the
same grammar for the web app, and ``tests/fixtures/filters.json`` holds the cases both must
agree on. :func:`predicate` turns a query into SQL on ``Work`` for one person. It is always
ANDed with :func:`lectern.access.visible_works`, and every look-up a clause makes (shelves,
duplicates, devices, uploaders) is limited to what that person may see, so a filter never
tells anyone about a work, shelf, or count they cannot see.

docs/filters.md is the reference for every field.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    Boolean,
    ColumnElement,
    Integer,
    and_,
    case,
    cast,
    exists,
    false,
    func,
    not_,
    or_,
    select,
)
from sqlalchemy.orm import Session

from lectern import reviews
from lectern.access import shelf_perms, visible_path, visible_shelf_ids, visible_works
from lectern.models import (
    Alignment,
    AlignmentStatus,
    AppPassword,
    AudioTrack,
    BookFile,
    DeviceQueueItem,
    FileKind,
    GeneratedAudiobook,
    Import,
    ImportStatus,
    Job,
    JobStatus,
    MatchSuggestion,
    PdfText,
    QueueStatus,
    ReadingState,
    Role,
    ShelfItem,
    User,
    Work,
)

EPUB, PDF, CBZ = "application/epub+zip", "application/pdf", "application/vnd.comicbook+zip"
FINISHED = 0.985  # lectern.reading_state.FINISHED

MAX_CHARS = 1000
MAX_CLAUSES = 24
MAX_VALUES = 20
MAX_WORDS = 12
MAX_VALUE_CHARS = 200


def fold(s: str | None) -> str | None:
    """``frontend/src/lib/fuzzy.ts`` ``fold``: compatibility-decompose, drop combining
    diacritical marks (U+0300–U+036F), lower-case. Also the SQL function ``lectern_fold``."""
    if s is None:
        return None
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not "\u0300" <= c <= "\u036f"
    ).lower()


class FilterError(ValueError):
    """A filter that cannot be run, with a code for programs and a sentence for people."""

    def __init__(self, code: str, message: str, key: str | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.key = code, message, key

    def json(self) -> dict:
        return {"detail": self.code, "message": self.message, "field": self.key}


# --- the fields -------------------------------------------------------------------------------

ENUM, TEXT, NUMBER, SIZE, DURATION, PERCENT, DATE, SHELF, USER, DEVICE, RATING = (
    "enum", "text", "number", "size", "duration", "percent", "date", "shelf", "user", "device",
    "rating",
)  # fmt: skip
COMPARABLE = {NUMBER, SIZE, DURATION, PERCENT, DATE, RATING}


@dataclass(frozen=True)
class Field:
    key: str
    kind: str
    label: str
    group: str
    aliases: tuple[str, ...] = ()
    # canonical value -> (label, aliases)
    options: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)
    members_only: bool = False


def _opts(**kw: tuple[str, tuple[str, ...]]) -> dict[str, tuple[str, tuple[str, ...]]]:
    return kw


FIELDS: dict[str, Field] = {
    f.key: f
    for f in [
        Field(
            "format",
            ENUM,
            "Format",
            "Files",
            options=_opts(
                epub=("EPUB", ()),
                pdf=("PDF", ()),
                cbz=("CBZ comic", ("comic", "comics")),
                audiobook=("Audiobook", ("audio", "m4b", "mp3")),
            ),
        ),  # fmt: skip
        Field(
            "kind",
            ENUM,
            "Ebook or audio",
            "Files",
            options=_opts(
                ebook=("Ebook only", ("ebook-only", "text")),
                audio=("Audio only", ("audio-only",)),
                both=("Ebook and audio", ()),
            ),
        ),  # fmt: skip
        Field("size", SIZE, "File size", "Files"),
        Field(
            "has",
            ENUM,
            "Has",
            "Book details",
            options=_opts(
                audio=("Audiobook", ("audiobook",)),
                cover=("Cover art", ("art",)),
                author=("Author", ("authors",)),
                series=("Series", ()),
                isbn=("ISBN", ()),
                tags=("Tags", ("tag",)),
                description=("Description", ("summary",)),
            ),
        ),  # fmt: skip
        Field(
            "audio",
            ENUM,
            "Narration",
            "Audio & read-along",
            aliases=("narration",),
            options=_opts(
                narrated=("Real narrator", ("human", "real")),
                generated=("Generated", ("tts", "synthesized")),
            ),
        ),  # fmt: skip
        Field(
            "align",
            ENUM,
            "Read-along",
            "Audio & read-along",
            aliases=("alignment",),
            options=_opts(
                aligned=("Aligned", ("done",)),
                running=("In progress", ("in-progress", "pending", "queued")),
                failed=("Failed", ()),
                none=("Not aligned", ("not-aligned", "no")),
            ),
        ),  # fmt: skip
        Field("length", DURATION, "Audio length", "Audio & read-along", aliases=("duration",)),
        Field("narrator", TEXT, "Narrator", "Audio & read-along", aliases=("narrators", "reader")),
        Field(
            "status",
            ENUM,
            "Reading status",
            "Reading",
            options=_opts(
                unread=("Not started", ("not-started", "new")),
                reading=("Reading", ("started",)),
                finished=("Finished", ("done", "read")),
            ),
        ),  # fmt: skip
        Field("progress", PERCENT, "Progress", "Reading"),
        Field("read", DATE, "Last read", "Reading", aliases=("last-read", "lastread")),
        Field("started", DATE, "Started", "Reading"),
        Field("finished", DATE, "Finished", "Reading"),
        Field("rating", RATING, "Your rating", "Reading", aliases=("rated", "stars")),
        Field("title", TEXT, "Title", "Book details"),
        Field("author", TEXT, "Author", "Book details", aliases=("by-author", "authors")),
        Field("series", TEXT, "Series", "Book details"),
        Field("lang", TEXT, "Language", "Book details", aliases=("language",)),
        Field("tag", TEXT, "Tag", "Book details", aliases=("tags",)),
        Field("publisher", TEXT, "Publisher", "Book details"),
        Field("year", NUMBER, "Year", "Book details", aliases=("published",)),
        Field("pages", NUMBER, "Page count", "Book details"),
        Field("added", DATE, "Added", "Library"),
        Field("shelf", SHELF, "Shelf", "Library", aliases=("shelves", "in")),
        Field(
            "by",
            USER,
            "Uploaded by",
            "Library",
            aliases=("uploaded-by", "uploader"),
            members_only=True,
        ),  # fmt: skip
        Field("queue", DEVICE, "Send-to-device queue", "Devices", aliases=("queued",)),
        Field(
            "is",
            ENUM,
            "Is",
            "Cleanup",
            options=_opts(
                offline=("Downloaded on this device", ("downloaded",)),
                duplicate=("Possible duplicate", ("duplicates", "dup")),
            ),
        ),  # fmt: skip
        Field("needs", ENUM, "Needs", "Cleanup", options=_opts(ocr=("Text recognition (OCR)", ()))),
    ]
}
_KEYS = {a: f.key for f in FIELDS.values() for a in (f.key, *f.aliases)}


def field_of(key: str) -> Field | None:
    k = _KEYS.get(key.lower())
    return FIELDS[k] if k else None


# --- the syntax -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Value:
    """One alternative of a clause: ``op`` is ``=``, ``<``, ``<=``, ``>``, ``>=``, or ``..``
    (``a``..``b``, either end may be empty)."""

    op: str
    a: str
    b: str = ""


@dataclass(frozen=True)
class Clause:
    key: str
    values: tuple[Value, ...]
    negate: bool = False


@dataclass(frozen=True)
class Word:
    text: str
    negate: bool = False


@dataclass
class Query:
    clauses: list[Clause] = field(default_factory=list)
    words: list[Word] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.clauses or self.words)


_KEY_RE = re.compile(r"([A-Za-z][A-Za-z-]*):")
_OP_RE = re.compile(r"^(<=|>=|<|>|=)")
_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(b|k|kb|kib|m|mb|mib|g|gb|gib|t|tb|tib)?$")
_DUR_RE = re.compile(r"^(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?:in)?)?(?:(\d+(?:\.\d+)?)s)?$")
_PCT_RE = re.compile(r"^(\d+(?:\.\d+)?)%?$")
_RATING_RE = re.compile(r"^[0-5](?:\.[05])?$")
_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?$")
_AGE_RE = re.compile(r"^(\d+)(d|w|m|y)$")


def _scan(s: str) -> list[tuple[bool, str | None, list[tuple[str, bool]]]]:
    """Tokens: (negated, key or None, items). A filter token's items are its comma-separated
    values; a free word has one item. Each item is (text, quoted)."""
    out = []
    i, n = 0, len(s)
    while i < n:
        if s[i].isspace():
            i += 1
            continue
        neg = False
        if s[i] in "-!" and i + 1 < n and not s[i + 1].isspace():
            neg, i = True, i + 1
        key = None
        m = _KEY_RE.match(s, i)
        if m and field_of(m.group(1)):
            key, i = field_of(m.group(1)).key, m.end()  # type: ignore[union-attr]
        items: list[tuple[str, bool]] = []
        cur: list[str] = []
        quoted = False
        while i < n and not s[i].isspace():
            c = s[i]
            if c == '"':
                i += 1
                while i < n and s[i] != '"':
                    if s[i] == "\\" and i + 1 < n:
                        i += 1
                    cur.append(s[i])
                    i += 1
                i += 1
                quoted = True
            elif c == "," and key:
                items.append(("".join(cur), quoted))
                cur, quoted = [], False
                i += 1
            else:
                cur.append(c)
                i += 1
        items.append(("".join(cur), quoted))
        out.append((neg, key, items))
    return out


def _num(text: str, code: str, label: str, key: str, rx: re.Pattern) -> re.Match:
    m = rx.match(text.lower().replace(" ", ""))
    if not m or not any(m.groups()):
        raise FilterError(code, f"{label}: “{text}” is not a value this field understands.", key)
    return m


def _check_scalar(f: Field, text: str) -> str:
    """Validate one end of a comparison for ``f``; returns its canonical text."""
    t = text.strip().lower()
    if f.kind == NUMBER:
        if not re.fullmatch(r"\d{1,9}", t):
            raise FilterError("bad_number", f"{f.label}: “{text}” is not a whole number.", f.key)
    elif f.kind == RATING:
        if not _RATING_RE.match(t) or float(t) > 5:
            raise FilterError(
                "bad_rating", f"{f.label}: “{text}” is not 0 to 5 stars in halves (4.5).", f.key
            )
    elif f.kind == SIZE:
        _num(t, "bad_size", f.label, f.key, _SIZE_RE)
    elif f.kind == DURATION:
        if not re.fullmatch(r"\d+(?:\.\d+)?", t):
            _num(t, "bad_duration", f.label, f.key, _DUR_RE)
    elif f.kind == PERCENT:
        m = _num(t, "bad_percent", f.label, f.key, _PCT_RE)
        if float(m.group(1)) > 100:
            raise FilterError("bad_percent", f"{f.label}: at most 100%.", f.key)
    elif f.kind == DATE:
        m = _DATE_RE.match(t)
        if m:
            try:
                datetime(int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1))
            except ValueError:
                m = None
        if not m and not _AGE_RE.match(t):
            raise FilterError(
                "bad_date",
                f"{f.label}: “{text}” is not a date (2026, 2026-03, 2026-03-15) or an age "
                "(30d, 2w, 6m, 1y).",
                f.key,
            )
    return t


def _value(f: Field, text: str, quoted: bool) -> Value:
    if f.kind in COMPARABLE and not quoted:
        t = text.strip()
        m = _OP_RE.match(t)
        if m and m.group(1) != "=":
            return Value(m.group(1), _check_scalar(f, t[m.end() :]))
        t = t[m.end() :] if m else t
        if ".." in t:
            a, b = t.split("..", 1)
            if not a.strip() and not b.strip():
                raise FilterError(
                    "empty_value", f"{f.label}: a range needs at least one end.", f.key
                )
            if f.kind == DATE and a.strip() and b.strip():
                if bool(_AGE_RE.match(a.strip().lower())) != bool(_AGE_RE.match(b.strip().lower())):
                    raise FilterError(
                        "bad_date", f"{f.label}: a range is two dates or two ages.", f.key
                    )
            return Value(
                "..",
                _check_scalar(f, a) if a.strip() else "",
                _check_scalar(f, b) if b.strip() else "",
            )
        return Value("=", _check_scalar(f, t))
    if f.kind in COMPARABLE:
        return Value("=", _check_scalar(f, text))
    t = text.strip()
    if f.kind == ENUM:
        low = t.lower()
        for canon, (_label, aliases) in f.options.items():
            if low == canon or low in aliases:
                return Value("=", canon)
        raise FilterError(
            "unknown_value",
            f"{f.label}: “{t}” is not one of {', '.join(f.options)}.",
            f.key,
        )
    if len(t) > MAX_VALUE_CHARS:
        raise FilterError(
            "too_long", f"{f.label}: a value is at most {MAX_VALUE_CHARS} characters.", f.key
        )
    return Value("=", t)


def parse(text: str) -> Query:
    """Read the filter syntax. Raises :class:`FilterError` for a value its field cannot take,
    or for a filter over the size limits."""
    if len(text) > MAX_CHARS:
        raise FilterError("too_long", f"A filter is at most {MAX_CHARS} characters.")
    q = Query()
    for neg, key, items in _scan(text):
        if key is None:
            word, quoted = items[0]
            if word.strip() or quoted:
                if not word.strip():
                    continue
                q.words.append(Word(word.strip(), neg))
            continue
        f = FIELDS[key]
        values = [i for i in items if i[0].strip() or i[1]]
        if not values:
            raise FilterError("empty_value", f"{f.label}: add a value after “{key}:”.", key)
        if len(values) > MAX_VALUES:
            raise FilterError("too_many", f"{f.label}: at most {MAX_VALUES} values.", key)
        vals = tuple(dict.fromkeys(_value(f, t, quoted) for t, quoted in values))
        q.clauses.append(Clause(key, vals, neg))
    if len(q.clauses) > MAX_CLAUSES:
        raise FilterError("too_many", f"A filter has at most {MAX_CLAUSES} conditions.")
    if len(q.words) > MAX_WORDS:
        raise FilterError("too_many", f"A filter has at most {MAX_WORDS} words.")
    return q


def _quote(t: str) -> str:
    return '"' + t.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _fmt_value(f: Field, v: Value) -> str:
    if v.op == "..":
        return f"{v.a}..{v.b}"
    if v.op != "=":
        return f"{v.op}{v.a}"
    t = v.a
    if f.kind in (ENUM, *COMPARABLE):
        return t
    if not t or re.search(r'[\s,"]', t) or _OP_RE.match(t) or ".." in t:
        return _quote(t)
    return t


def format_clause(c: Clause) -> str:
    f = FIELDS[c.key]
    return ("-" if c.negate else "") + f"{c.key}:" + ",".join(_fmt_value(f, v) for v in c.values)


def format_word(w: Word) -> str:
    t = w.text
    if re.search(r'[\s",:]', t) or t[0] in "-!":
        t = _quote(t)
    return ("-" if w.negate else "") + t


def format_query(q: Query) -> str:
    """The canonical text of a query: what is stored for a smart shelf and shown in URLs."""
    return " ".join([format_clause(c) for c in q.clauses] + [format_word(w) for w in q.words])


# --- values -----------------------------------------------------------------------------------

_SIZE_UNIT = {
    None: 1024**2, "b": 1, "k": 1024, "kb": 1024, "kib": 1024, "m": 1024**2, "mb": 1024**2,
    "mib": 1024**2, "g": 1024**3, "gb": 1024**3, "gib": 1024**3, "t": 1024**4, "tb": 1024**4,
    "tib": 1024**4,
}  # fmt: skip


def size_of(t: str) -> tuple[float, float]:
    """(bytes, unit): ``100MB`` is (104857600, 1048576). A bare number is megabytes."""
    m = _SIZE_RE.match(t.replace(" ", ""))
    assert m
    unit = _SIZE_UNIT[m.group(2)]
    return float(m.group(1)) * unit, unit


def seconds_of(t: str) -> tuple[float, float]:
    """(seconds, unit). A bare number is hours; ``1h30m`` is 5400 seconds in minutes."""
    if re.fullmatch(r"\d+(?:\.\d+)?", t):
        return float(t) * 3600, 3600
    m = _DUR_RE.match(t)
    assert m
    h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    unit = 1 if m.group(3) else 60 if m.group(2) else 3600
    return h * 3600 + mi * 60 + s, unit


def period_of(t: str, now: datetime) -> tuple[datetime, datetime, bool]:
    """(start, end, is_age) of a date value. For an age, start is ``now`` minus the age and
    end is ``now``: ``30d`` is the last 30 days."""
    m = _AGE_RE.match(t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = {"d": 1, "w": 7, "m": 30.4375, "y": 365.25}[unit] * n
        return now - timedelta(days=days), now, True
    m = _DATE_RE.match(t)
    assert m
    y, mo, d = int(m.group(1)), m.group(2), m.group(3)
    start = datetime(y, int(mo or 1), int(d or 1), tzinfo=UTC)
    if d:
        end = start + timedelta(days=1)
    elif mo:
        end = datetime(y + (start.month == 12), start.month % 12 + 1, 1, tzinfo=UTC)
    else:
        end = datetime(y + 1, 1, 1, tzinfo=UTC)
    return start, end, False


def _compare(x, f: Field, v: Value, now: datetime):
    """SQL for one comparable value against the expression ``x`` (NULL never matches)."""
    if f.kind == DATE:
        if v.op == "..":
            parts = []
            if v.a:
                a0, a1, age = period_of(v.a, now)
                # Ages count backwards: "7d..30d" is between 30 and 7 days ago.
                parts.append(x <= a0 if age else x >= a0)
            if v.b:
                b0, b1, age = period_of(v.b, now)
                parts.append(x >= b0 if age else x < b1)
            return and_(*parts)
        start, end, age = period_of(v.a, now)
        if age:
            # An age compares how long ago: "<30d" (or "30d") is more recent than 30 days ago.
            return {"=": x >= start, "<": x > start, "<=": x >= start,
                    ">": x < start, ">=": x <= start}[v.op]  # fmt: skip
        return {"=": and_(x >= start, x < end), "<": x < start, "<=": x < end,
                ">": x >= end, ">=": x >= start}[v.op]  # fmt: skip

    def num(t: str) -> tuple[float, float]:
        if f.kind == SIZE:
            return size_of(t)
        if f.kind == DURATION:
            return seconds_of(t)
        if f.kind == PERCENT:
            return float(_PCT_RE.match(t).group(1)) / 100, 0.01  # type: ignore[union-attr]
        if f.kind == RATING:
            return float(t), 0.5
        return float(int(t)), 1.0

    if v.op == "..":
        parts = []
        if v.a:
            parts.append(x >= num(v.a)[0])
        if v.b:
            parts.append(x <= num(v.b)[0])
        return and_(*parts)
    n, unit = num(v.a)
    if v.op == "=":
        if f.kind in (NUMBER, RATING):
            return x == n
        # "length:10h" or "size:100MB": equal when rounded to the unit written.
        return and_(x >= n - unit / 2, x < n + unit / 2)
    return {"<": x < n, "<=": x <= n, ">": x > n, ">=": x >= n}[v.op]


# --- SQL --------------------------------------------------------------------------------------


@dataclass
class Context:
    """Who is filtering, and what the filter may look at.

    ``strict``: a shelf, device, or person a value names that does not exist or this person
    cannot see is an error (a filter typed now). Off for stored smart-shelf rules, where such
    a value matches nothing for this viewer instead, exactly as if it did not exist.
    ``offline_ids``: the works this device holds offline (``is:offline``), sent by the web
    app; nowhere else knows."""

    db: Session
    user: User
    strict: bool = True
    offline_ids: frozenset[int] = frozenset()
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Inside a smart shelf's rule: a smart shelf named there matches nothing (no recursion).
    nested: bool = False
    # The person's private reviews (lectern.reviews): their ratings (``rating:``) and the read
    # dates they entered by hand (``started:``/``finished:``). Off for an AI key without the
    # reviews scope: no ``rating:``, and only the reading-state dates.
    private_reviews: bool = True

    def lenient(self) -> Context:
        return Context(
            self.db, self.user, False, self.offline_ids, self.now, nested=True,
            private_reviews=self.private_reviews,
        )  # fmt: skip


def _has_file(media_type: str):
    return exists().where(
        BookFile.work_id == Work.id,
        BookFile.kind == FileKind.EBOOK,
        BookFile.media_type == media_type,
    )


def _has_ebook():
    return exists().where(BookFile.work_id == Work.id, BookFile.kind == FileKind.EBOOK)


def _has_audio():
    return exists().where(AudioTrack.work_id == Work.id)


def _json_any(column, test: Callable) -> ColumnElement[bool]:
    each = func.json_each(column).table_valued("value").alias("each")
    return exists(select(1).select_from(each).where(test(each.c.value)))


def _contains(column, text: str):
    return func.lectern_fold(column).contains(fold(text), autoescape=True)


def total_size():
    files = select(func.coalesce(func.sum(BookFile.size), 0)).where(BookFile.work_id == Work.id)
    audio = select(func.coalesce(func.sum(AudioTrack.size), 0)).where(AudioTrack.work_id == Work.id)
    return files.scalar_subquery() + audio.scalar_subquery()


def audio_length():
    return (
        select(func.sum(AudioTrack.duration_s))
        .where(AudioTrack.work_id == Work.id)
        .scalar_subquery()
    )


def page_count():
    return select(func.max(BookFile.pages)).where(BookFile.work_id == Work.id).scalar_subquery()


def year_of():
    head = func.substr(Work.published, 1, 4)
    return case((head.op("GLOB")("[0-9][0-9][0-9][0-9]"), cast(head, Integer)), else_=None)


def _state(user: User, column):
    return (
        select(column)
        .where(ReadingState.user_id == user.id, ReadingState.work_id == Work.id)
        .scalar_subquery()
    )


def fraction_of(user: User):
    return func.coalesce(_state(user, ReadingState.fraction), 0.0)


def _not_found(ctx: Context, code: str, message: str, key: str):
    if ctx.strict:
        raise FilterError(code, message, key)
    return false()


def _shelf_pred(ctx: Context, values: tuple[Value, ...], key: str) -> ColumnElement[bool]:
    shelves, perms = shelf_perms(ctx.db, ctx.user)
    shelved = select(ShelfItem.work_id).where(ShelfItem.shelf_id.in_(visible_shelf_ids(ctx.user)))
    paths = {
        sid: fold("/".join(s.name for s in visible_path(shelves[sid], shelves, perms)))
        for sid in perms
    }
    children: dict[int, list[int]] = {}
    for sid in perms:
        p = shelves[sid].parent_id
        if p in perms:
            children.setdefault(p, []).append(sid)
    parts = []
    for v in values:
        t = v.a
        if fold(t) == "none":
            parts.append(Work.id.not_in(shelved))
            continue
        if fold(t) == "any":
            parts.append(Work.id.in_(shelved))
            continue
        if re.fullmatch(r"#\d+", t):
            found = [int(t[1:])] if int(t[1:]) in perms else []
        else:
            want = fold(t.strip("/"))
            found = [sid for sid, p in paths.items() if p == want] or [
                sid for sid in perms if fold(shelves[sid].name) == want
            ]
        if not found:
            parts.append(_not_found(ctx, "unknown_shelf", f"Shelf: no shelf “{t}”.", key))
            continue
        # A smart shelf named here is what it shows; one nested below a shelf is not part of
        # that shelf's books, and inside another smart shelf's rule it matches nothing.
        for sid in found:
            if shelves[sid].rule is not None:
                parts.append(
                    false() if ctx.nested else smart_predicate(ctx.lenient(), shelves[sid])
                )
        found = [sid for sid in found if shelves[sid].rule is None]
        if not found:
            continue
        subtree, todo = set(), list(found)
        while todo:
            sid = todo.pop()
            if sid not in subtree and shelves[sid].rule is None:
                subtree.add(sid)
                todo.extend(children.get(sid, []))
        parts.append(
            Work.id.in_(select(ShelfItem.work_id).where(ShelfItem.shelf_id.in_(sorted(subtree))))
        )
    return or_(*parts) if parts else false()


def _user_pred(ctx: Context, values: tuple[Value, ...], key: str) -> ColumnElement[bool]:
    if ctx.user.role == Role.READER:
        if ctx.strict:
            raise FilterError(
                "field_not_allowed",
                "Uploaded by: only owners and members can filter by uploader.",
                key,
            )
        # A smart shelf shared with a reader: who uploaded what is not theirs to learn.
        return false()
    users = {fold(u.username): u.id for u in ctx.db.scalars(select(User))}
    parts = []
    for v in values:
        uid = users.get(fold(v.a.lstrip("@")))
        if uid is None:
            parts.append(
                _not_found(ctx, "unknown_user", f"Uploaded by: no one is called “{v.a}”.", key)
            )
            continue
        parts.append(
            Work.id.in_(
                select(Import.work_id).where(
                    Import.user_id == uid, Import.status == ImportStatus.FILED
                )
            )
        )
    return or_(*parts)


def _device_pred(ctx: Context, values: tuple[Value, ...], key: str) -> ColumnElement[bool]:
    devices = list(
        ctx.db.scalars(
            select(AppPassword).where(
                AppPassword.user_id == ctx.user.id, AppPassword.revoked_at.is_(None)
            )
        )
    )
    ids: set[int] = set()
    parts = []
    for v in values:
        t = v.a
        if fold(t) == "any":
            ids |= {d.id for d in devices}
            continue
        hit = [d.id for d in devices if (t == f"#{d.id}" or fold(d.label) == fold(t))]
        if not hit:
            parts.append(_not_found(ctx, "unknown_device", f"Queue: no device “{t}”.", key))
        ids |= set(hit)
    if ids:
        parts.append(
            Work.id.in_(
                select(DeviceQueueItem.work_id).where(
                    DeviceQueueItem.user_id == ctx.user.id,
                    DeviceQueueItem.status == QueueStatus.PENDING,
                    DeviceQueueItem.app_password_id.in_(sorted(ids)),
                )
            )
        )
    return or_(*parts) if parts else false()


def duplicate_ids(db: Session, user: User) -> set[int]:
    """Works that look like another work this person can see: the same title and author
    (accents, case, and spacing ignored) unless someone said they are different books, or a
    possible duplicate (from ingest or the library scan) nobody has decided on yet. Only visible works are compared, so a hidden copy is never hinted at."""
    seen: dict[tuple[str, str], list[int]] = {}
    for wid, title, author in db.execute(
        select(Work.id, Work.title, Work.author).where(visible_works(user))
    ):
        k = (" ".join((fold(title) or "").split()), " ".join((fold(author) or "").split()))
        seen.setdefault(k, []).append(wid)
    open_pairs, dismissed = [], set()
    for a, b, resolution in db.execute(
        select(
            MatchSuggestion.work_id, MatchSuggestion.candidate_work_id, MatchSuggestion.resolution
        ).where(
            or_(MatchSuggestion.resolved_at.is_(None), MatchSuggestion.resolution == "dismissed")
        )
    ):
        if resolution == "dismissed":
            dismissed.add(frozenset((a, b)))
        else:
            open_pairs.append((a, b))
    # A pair someone marked as different books is not a duplicate any more.
    out = {
        w
        for ids in seen.values()
        if len(ids) > 1
        for w in ids
        if any(o != w and frozenset((o, w)) not in dismissed for o in ids)
    }
    visible = {w for ids in seen.values() for w in ids}
    for a, b in open_pairs:
        if a in visible and b in visible:
            out |= {a, b}
    return out


def _needs_ocr():
    ocr_done = exists().where(
        Job.work_id == Work.id, Job.kind == "ocr", Job.status == JobStatus.SUCCEEDED
    )
    scan = exists().where(
        PdfText.work_id == Work.id,
        PdfText.status == "needs_ocr",
        PdfText.file_id == BookFile.id,
        BookFile.sha256 == PdfText.pdf_sha256,
    )
    return and_(scan, not_(ocr_done))


def _enum_pred(ctx: Context, f: Field, v: str) -> ColumnElement[bool]:
    u = ctx.user
    if f.key == "format":
        return (
            _has_audio()
            if v == "audiobook"
            else _has_file({"epub": EPUB, "pdf": PDF, "cbz": CBZ}[v])
        )
    if f.key == "kind":
        return {"ebook": and_(_has_ebook(), not_(_has_audio())),
                "audio": and_(_has_audio(), not_(_has_ebook())),
                "both": and_(_has_ebook(), _has_audio())}[v]  # fmt: skip
    if f.key == "has":
        nonempty = lambda c: and_(c.is_not(None), func.trim(c) != "")  # noqa: E731
        return {
            "audio": _has_audio(),
            "cover": Work.has_cover.is_(True),
            "author": and_(nonempty(Work.author), Work.author != "Unknown Author"),
            "series": nonempty(Work.series),
            "isbn": nonempty(Work.isbn),
            "tags": func.coalesce(func.json_array_length(Work.tags), 0) > 0,
            "description": nonempty(Work.description),
        }[v]
    if f.key == "audio":
        generated = exists().where(GeneratedAudiobook.work_id == Work.id)
        return and_(_has_audio(), generated if v == "generated" else not_(generated))
    if f.key == "align":
        st = {"aligned": [AlignmentStatus.SUCCEEDED],
              "running": [AlignmentStatus.PENDING, AlignmentStatus.RUNNING],
              "failed": [AlignmentStatus.FAILED]}  # fmt: skip
        if v == "none":
            current = [s for ss in st.values() for s in ss]
            return and_(
                _has_audio(),
                not_(exists().where(Alignment.work_id == Work.id, Alignment.status.in_(current))),
            )
        return exists().where(Alignment.work_id == Work.id, Alignment.status.in_(st[v]))
    if f.key == "status":
        x = fraction_of(u)
        return {"unread": x <= 0, "reading": and_(x > 0, x < FINISHED), "finished": x >= FINISHED}[
            v
        ]
    if f.key == "is":
        if v == "offline":
            return Work.id.in_(sorted(ctx.offline_ids)) if ctx.offline_ids else false()
        ids = duplicate_ids(ctx.db, u)
        return Work.id.in_(sorted(ids)) if ids else false()
    if f.key == "needs":
        return _needs_ocr()
    raise AssertionError(f.key)


def _text_pred(f: Field, t: str) -> ColumnElement[bool]:
    if f.key == "title":
        return _contains(Work.title, t)
    if f.key == "author":
        return or_(
            _contains(Work.author, t),
            _json_any(Work.authors, lambda v: _contains(func.json_extract(v, "$.name"), t)),
        )
    if f.key == "series":
        return _contains(Work.series, t)
    if f.key == "publisher":
        return _contains(Work.publisher, t)
    if f.key == "narrator":
        return _json_any(Work.narrators, lambda v: _contains(v, t))
    if f.key == "tag":
        return _json_any(Work.tags, lambda v: func.lectern_fold(v) == fold(t))
    if f.key == "lang":
        lang = func.lectern_fold(Work.language)
        return or_(lang == fold(t), lang.like(fold(t).replace("%", "").replace("_", "") + "-%"))
    raise AssertionError(f.key)


def _scalar(f: Field, u: User, private_reviews: bool = True):
    if f.key in ("started", "finished") and not private_reviews:
        return _state(
            u, ReadingState.started_at if f.key == "started" else ReadingState.finished_at
        )
    return {
        "size": total_size,
        "length": audio_length,
        "pages": page_count,
        "year": year_of,
        "progress": lambda: fraction_of(u),
        "read": lambda: _state(u, ReadingState.last_read_at),
        "started": lambda: reviews.latest_date(u.id, "started"),
        "finished": lambda: reviews.latest_date(u.id, "finished"),
        "rating": lambda: reviews.rating_of(u.id),
        "added": lambda: Work.created_at,
    }[f.key]()


def clause_pred(ctx: Context, c: Clause) -> ColumnElement[bool]:
    f = FIELDS[c.key]
    if f.kind == SHELF:
        pred = _shelf_pred(ctx, c.values, c.key)
    elif f.kind == USER:
        pred = _user_pred(ctx, c.values, c.key)
    elif f.kind == DEVICE:
        pred = _device_pred(ctx, c.values, c.key)
    elif f.kind == ENUM:
        pred = or_(*(_enum_pred(ctx, f, v.a) for v in c.values))
    elif f.kind == TEXT:
        pred = or_(*(_text_pred(f, v.a) for v in c.values))
    elif f.key in ("started", "finished") and ctx.private_reviews:
        # Any read-through's date; the reading state's for a work with none saved.
        pred = reviews.date_pred(
            ctx.user.id, f.key, lambda x: or_(*(_compare(x, f, v, ctx.now) for v in c.values))
        )
    elif f.kind == RATING and not ctx.private_reviews:
        if ctx.strict:
            raise FilterError(
                "field_not_allowed",
                "Your rating: this AI key needs the reviews scope to filter by rating.",
                c.key,
            )
        pred = false()
    else:
        x = _scalar(f, ctx.user, ctx.private_reviews)
        pred = or_(*(_compare(x, f, v, ctx.now) for v in c.values))
    if c.negate:
        # "Is not": everything else, works with no value for the field included.
        return not_(func.coalesce(cast(pred, Boolean), false()))
    return pred


def word_pred(w: Word) -> ColumnElement[bool]:
    hit = or_(
        _contains(Work.title, w.text),
        _contains(Work.author, w.text),
        _contains(Work.series, w.text),
        _contains(Work.isbn, w.text),
    )
    return not_(func.coalesce(cast(hit, Boolean), false())) if w.negate else hit


def predicate(ctx: Context, q: Query) -> ColumnElement[bool]:
    """SQL on ``Work`` for ``q`` as ``ctx.user`` sees it, with :func:`visible_works`."""
    parts = [visible_works(ctx.user)]
    parts += [clause_pred(ctx, c) for c in q.clauses]
    parts += [word_pred(w) for w in q.words]
    return and_(*parts)


def check(ctx: Context, q: Query) -> None:
    """Raise what :func:`predicate` would for ``q`` (unknown shelves, devices, people)."""
    for c in q.clauses:
        clause_pred(ctx, c)


# --- sorting ----------------------------------------------------------------------------------

# key -> (label, newest/largest first by default)
SORTS: dict[str, tuple[str, bool]] = {
    "title": ("Title", False),
    "author": ("Author", False),
    "added": ("Newest", True),
    "series": ("Series", False),
    "year": ("Year", False),
    "size": ("File size", True),
    "length": ("Audio length", True),
    "pages": ("Page count", True),
    "progress": ("Progress", True),
    "read": ("Last read", True),
    "started": ("Started", True),
    "finished": ("Finished", True),
    "rating": ("Your rating", True),
    "lang": ("Language", False),
    "publisher": ("Publisher", False),
}
SORT_PATTERN = "^(" + "|".join(SORTS) + ")$"


def order_by(
    sort: str, user: User, descending: bool | None = None, private_reviews: bool = True
) -> list:
    """ORDER BY for ``sort`` (a :data:`SORTS` key): works without a value last, then title.
    ``private_reviews`` off (an AI key without the reviews scope): ``started``/``finished``
    use only the reading-state dates; ``rating`` is refused by the caller."""
    desc = SORTS[sort][1] if descending is None else descending
    if sort == "title":
        cols = [Work.sort_title]
    elif sort == "author":
        cols = [Work.sort_author]
    elif sort == "series":
        cols = [Work.series, Work.series_index]
    elif sort == "lang":
        cols = [Work.language]
    elif sort == "publisher":
        cols = [Work.publisher]
    elif sort == "added":
        cols = [Work.created_at]
    else:
        x = _scalar(FIELDS[sort], user, private_reviews)
        cols = [_state(user, ReadingState.fraction) if sort == "progress" else x]
    out = [(c.desc() if desc else c.asc()).nulls_last() for c in cols]
    if sort == "added":
        return [*out, Work.id.desc() if desc else Work.id.asc()]
    if sort != "title":
        out.append(Work.sort_title.asc())
    return [*out, Work.id.asc()]


def describe() -> list[dict]:
    """The fields, for the web app's filter menu and the MCP tool description."""
    return [
        {
            "key": f.key,
            "kind": f.kind,
            "label": f.label,
            "group": f.group,
            "aliases": list(f.aliases),
            "options": [{"value": k, "label": lab} for k, (lab, _a) in f.options.items()],
            "members_only": f.members_only,
        }
        for f in FIELDS.values()
    ]


# --- smart shelves ----------------------------------------------------------------------------
#
# A smart shelf is a shelf whose ``rule`` is ``{"v": 1, "filter": <canonical filter>,
# "sort": <SORTS key>, "desc": bool | None}``; its books are the filter's matches, live, for
# whoever is looking: their visibility, their reading progress, their devices. It holds no
# items and no sub-shelves, and a grant on it lets someone see the shelf, never a book they
# could not already see.


def make_rule(text: str, sort: str | None = None, desc: bool | None = None) -> dict:
    q = parse(text)
    if not q:
        raise FilterError("empty_filter", "A smart shelf needs at least one condition.")
    return {"v": 1, "filter": format_query(q), "sort": sort or "title", "desc": desc}


def rule_query(shelf) -> Query | None:
    """The stored rule's query, or ``None`` for a rule this version cannot read."""
    try:
        return parse((shelf.rule or {}).get("filter") or "") or None
    except FilterError:
        return None


def smart_predicate(ctx: Context, shelf) -> ColumnElement[bool]:
    """What ``shelf`` shows ``ctx.user`` (an unreadable rule: nothing). Values naming a
    shelf, device, or person this viewer cannot see match nothing (``ctx`` is lenient)."""
    q = rule_query(shelf)
    return predicate(ctx, q) if q is not None else false()


def smart_order(shelf, user: User) -> list:
    rule = shelf.rule or {}
    sort = rule.get("sort") if rule.get("sort") in SORTS else "title"
    return order_by(sort, user, rule.get("desc"))
