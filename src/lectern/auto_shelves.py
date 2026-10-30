"""Auto-shelves (issue #73): shelves the library keeps for itself, one per author, tag, genre,
language, or narrator.

A *group* is an ordinary shelf with a :class:`~lectern.models.ShelfAutoGroup` row naming a
field. Under it, :func:`reconcile` keeps one *child* per value of that field: a real shelf
whose rule is the exact filter for the value (``author:="Leo Tolstoy"``, ``tag:fantasy``,
``lang:en``), so OPDS, follows, ``shelf:`` filters, MCP, and Cmd+K treat it like any smart
shelf. With ``buckets`` on, children sit in A–Z (and ``#``) *buckets* between them and the
group.

Privacy is :func:`view`'s job, not reconcile's. Children are made from the whole library (owners
and members see all of it), so a child's name alone could tell a reader about books they cannot
see. :func:`view` counts, for one viewer, the books they may see under each child; a child below
its group's minimum for them, and a bucket with no child they may see, is hidden: dropped by
:func:`lectern.access.shelf_perms` and ``None`` from :func:`lectern.access.shelf_permission`,
exactly like a shelf that does not exist.

A child is never deleted because it fell below the minimum: it goes dormant (hidden from
everyone) and keeps its id, so trash-and-restore brings back the same shelf and a device
following it keeps working. Only a child with no books at all and no follower is removed.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import ColumnElement, Select, case, delete, exists, func, select, true, union_all
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from lectern import filters, instance
from lectern.access import MAX_DEPTH, depth_of, sees_whole_library, visible_works
from lectern.models import (
    DeviceFollow,
    InstanceSetting,
    Shelf,
    ShelfAutoChild,
    ShelfAutoGroup,
    ShelfGrant,
    ShelfItem,
    User,
    Work,
)

log = logging.getLogger(__name__)

MIN_BOOKS_MAX = 50
CHILD_ORDERS = ("name", "count")
VIEW_TTL_S = 60.0
_VIEW_CACHE_SIZE = 256


# --- fields -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoField:
    """One kind of group. ``source(pred, genres)`` is a ``SELECT work_id, key, raw``: a row per
    value of a work matching ``pred``, ``key`` its folded value (what a child is keyed by and
    what the child's filter compares) and ``raw`` the spelling as stored. ``rule_text(display,
    key, genres)`` is the child's filter and the folded values it must parse back to, or
    ``None`` when the value cannot be written as one."""

    key: str
    source: Callable[[ColumnElement[bool], dict], Select]
    rule_text: Callable[[str, str, dict], tuple[str, set[str]] | None]
    min_books: int = 1
    buckets: bool = False


def _each(column, name: str):
    return func.json_each(column).table_valued("value").alias(name)


def _json_list(column) -> Callable[[ColumnElement[bool], dict], Select]:
    def source(pred: ColumnElement[bool], _genres: dict) -> Select:
        each = _each(column, "each")
        return (
            select(Work.id.label("work_id"), func.lectern_fold(each.c.value).label("key"),
                   each.c.value.label("raw"))
            .select_from(Work).join(each, true()).where(pred)
        )  # fmt: skip

    return source


def _authors(pred: ColumnElement[bool], _genres: dict) -> Select:
    # As the author:= filter matches: every listed author, or the display author of a work
    # that lists none.
    each = _each(Work.authors, "each")
    name = func.json_extract(each.c.value, "$.name")
    listed = select(Work.id.label("work_id"), func.lectern_fold(name).label("key"),
                    name.label("raw")).select_from(Work).join(each, true())  # fmt: skip
    no_authors = (Work.authors.is_(None)) | (func.json_array_length(Work.authors) == 0)
    plain = select(Work.id, func.lectern_fold(Work.author), Work.author).where(no_authors)
    return union_all(listed.where(pred), plain.where(pred))


def _primary(x):
    """``en-GB`` → ``en``: lang:en matches ``en`` and every ``en-…``."""
    return case((func.instr(x, "-") > 0, func.substr(x, 1, func.instr(x, "-") - 1)), else_=x)


def _langs(pred: ColumnElement[bool], _genres: dict) -> Select:
    return select(
        Work.id.label("work_id"),
        _primary(func.lectern_fold(Work.language)).label("key"),
        _primary(Work.language).label("raw"),
    ).where(pred, Work.language.is_not(None))


def _genre_rows(pred: ColumnElement[bool], genres: dict) -> Select:
    pairs = json.dumps([[t, key, name] for key, (name, tags) in genres.items() for t in tags])
    tag, gm = _each(Work.tags, "each"), _each(pairs, "gm")
    return (
        select(Work.id.label("work_id"), func.json_extract(gm.c.value, "$[1]").label("key"),
               func.json_extract(gm.c.value, "$[2]").label("raw"))
        .select_from(Work).join(tag, true())
        .join(gm, func.json_extract(gm.c.value, "$[0]") == func.lectern_fold(tag.c.value))
        .where(pred)
    )  # fmt: skip


def _q(t: str) -> str:
    return filters._quote(t)


def _exact(key: str) -> Callable[[str, str, dict], tuple[str, set[str]]]:
    return lambda display, k, _g: (f"{key}:={_q(display)}", {k})


def _lang_rule(_display: str, key: str, _g: dict) -> tuple[str, set[str]] | None:
    # lang: is a prefix match on "-" with % and _ stripped; only a plain code says the same.
    return (f"lang:{key}", {key}) if re.fullmatch(r"[0-9a-z]+", key) else None


def _genre_rule(_display: str, key: str, genres: dict) -> tuple[str, set[str]] | None:
    if key not in genres:
        return None
    tags = genres[key][1]
    return "tag:" + ",".join(_q(t) for t in tags), set(tags)


# key -> field. Series joins once #70's series_key lands (its source keys by that instead).
FIELDS: dict[str, AutoField] = {
    "author": AutoField("author", _authors, _exact("author"), min_books=2, buckets=True),
    "tag": AutoField("tag", _json_list(Work.tags),
                     lambda d, k, _g: (f"tag:{_q(d)}", {k}), min_books=2, buckets=True),
    "genre": AutoField("genre", _genre_rows, _genre_rule),
    "lang": AutoField("lang", _langs, _lang_rule),
    "narrator": AutoField("narrator", _json_list(Work.narrators), _exact("narrator")),
}  # fmt: skip


def _genres(db: Session) -> dict[str, tuple[str, list[str]]]:
    """The genre map, keyed by folded genre: (name, folded tags). Genres that fold alike are
    one genre with both tag lists."""
    out: dict[str, tuple[str, list[str]]] = {}
    for name, tags in instance.genre_map(db).items():
        key = filters.fold(name.strip()) or ""
        if key:
            got = out.setdefault(key, (name.strip(), []))[1]
            for t in tags:
                ft = filters.fold(t.strip()) or ""
                if ft and ft not in got and len(got) < filters.MAX_VALUES:
                    got.append(ft)
    return {k: v for k, v in out.items() if v[1]}


def _counts(db: Session, fld: AutoField, pred: ColumnElement[bool], genres: dict) -> dict:
    """Books matching ``pred`` per key, each book once per key."""
    src = fld.source(pred, genres).subquery()
    return dict(
        db.execute(
            select(src.c.key, func.count(func.distinct(src.c.work_id)))
            .where(src.c.key.is_not(None), src.c.key != "")
            .group_by(src.c.key)
        ).all()
    )


def _names(db: Session, fld: AutoField, genres: dict) -> dict[str, str]:
    """The most common spelling of each key in live books (a tie: the first in code order)."""
    src = fld.source(Work.trashed_at.is_(None), genres).subquery()
    best: dict[str, tuple[int, str]] = {}
    for key, raw, n in db.execute(
        select(src.c.key, src.c.raw, func.count())
        .where(src.c.key.is_not(None), src.c.key != "", src.c.raw.is_not(None))
        .group_by(src.c.key, src.c.raw)
    ):
        if key not in best or n > best[key][0] or (n == best[key][0] and raw < best[key][1]):
            best[key] = (n, raw)
    return {k: raw for k, (_n, raw) in best.items()}


# --- groups -----------------------------------------------------------------------------------


def fits_depth(group_depth: int, buckets: bool) -> bool:
    """Whether a group at ``group_depth`` (1 = top level) has room for its children."""
    return group_depth + (2 if buckets else 1) <= MAX_DEPTH


def create_group(
    db: Session,
    owner: User,
    name: str,
    field: str,
    parent: Shelf | None = None,
    *,
    buckets: bool | None = None,
    min_books: int | None = None,
    child_order: str = "name",
    sort: str = "title",
    descending: bool | None = None,
) -> Shelf:
    """A new group shelf. ``ValueError`` (its message a code) for a setting out of range or
    ``too_deep`` when its children would nest deeper than ``MAX_DEPTH``. The caller checks
    that ``owner`` may create a shelf in ``parent`` and commits."""
    fld = FIELDS.get(field)
    if fld is None:
        raise ValueError("unknown_field")
    buckets = fld.buckets if buckets is None else buckets
    min_books = fld.min_books if min_books is None else min_books
    if not 1 <= min_books <= MIN_BOOKS_MAX:
        raise ValueError("min_books")
    if child_order not in CHILD_ORDERS:
        raise ValueError("child_order")
    if sort not in filters.SORTS:
        raise ValueError("sort")
    if not fits_depth((depth_of(parent) if parent is not None else 0) + 1, buckets):
        raise ValueError("too_deep")
    shelf = Shelf(owner_id=owner.id, name=name, parent=parent)
    db.add(shelf)
    db.flush()
    db.add(ShelfAutoGroup(shelf_id=shelf.id, field=field, min_books=min_books,
                          child_order=child_order, buckets=buckets, sort=sort,
                          descending=descending))  # fmt: skip
    db.flush()
    return shelf


# --- reconcile --------------------------------------------------------------------------------

_lock = threading.Lock()
# Per database (its URL): the stamp last reconciled, and how many times reconcile changed it.
_done: dict[str, tuple] = {}
_generation: dict[str, int] = {}


def _url(db: Session) -> str:
    return str(db.get_bind().url)


def _stamp(db: Session, *, view: bool = False, reader: bool = False) -> tuple:
    """What reconcile depends on: every work row (trash and purge change it too), the groups,
    and the genre map. For :func:`view` (``view``), also the generated shelves, and for a
    reader (``reader``) the shelves, items, and grants that decide what they see. One query."""
    cols = [func.count(Work.id), func.max(Work.id), func.max(Work.updated_at),
            func.count(ShelfAutoGroup.shelf_id), func.max(ShelfAutoGroup.updated_at)]  # fmt: skip
    if view:
        cols += [func.count(ShelfAutoChild.shelf_id), func.max(ShelfAutoChild.shelf_id)]
    if reader:
        cols += [func.count(Shelf.id), func.max(Shelf.updated_at),
                 func.count(ShelfItem.work_id), func.max(ShelfItem.added_at),
                 func.count(ShelfGrant.shelf_id), func.max(ShelfGrant.created_at)]  # fmt: skip
    genre_map = select(InstanceSetting.updated_at).where(InstanceSetting.key == instance.GENRE_MAP)
    parts = [select(c).scalar_subquery() for c in cols] + [genre_map.scalar_subquery()]
    return tuple(db.execute(select(*parts)).one())


def _writing(db: Session) -> bool:
    """Whether ``db`` holds SQLite's write lock (it wrote and has not committed): another
    connection's write would then wait on it, from the same thread."""
    return bool(db.connection().connection.dbapi_connection.in_transaction)


def reconcile(db: Session) -> None:
    """Bring every group's children in step with the library, if it changed since last time.

    Runs in its own session and commits, so a read-only request keeps what it made. When
    ``db`` itself has written, it is skipped (the next request does it): only structure waits,
    never privacy, which :func:`view` decides from what is there."""
    url = _url(db)
    if _done.get(url) == _stamp(db) or _writing(db):
        return
    with _lock, Session(db.get_bind()) as s:
        stamp = _stamp(s)
        if _done.get(url) == stamp:
            return
        try:
            _sync(s)
            s.commit()
        except OperationalError:  # another writer held the lock past busy_timeout
            log.warning("auto-shelves: reconcile skipped, the database was busy", exc_info=True)
            s.rollback()
            return
        _done[url] = stamp
        _generation[url] = _generation.get(url, 0) + 1


def _sync(db: Session) -> None:
    genres = _genres(db)
    followed = set(db.scalars(select(DeviceFollow.shelf_id)))
    found: dict[str, tuple[dict, dict]] = {}
    for group in db.scalars(select(ShelfAutoGroup)).all():
        fld = FIELDS.get(group.field)
        if fld is None:
            continue
        if group.field not in found:
            live = Work.trashed_at.is_(None)
            found[group.field] = (_counts(db, fld, live, genres), _names(db, fld, genres))
        _sync_group(db, group, fld, *found[group.field], genres, followed)


def _clean_name(raw: str) -> str:
    """A value as a shelf name: no "/" (it separates a path), no control characters."""
    name = "".join(c for c in raw if ord(c) >= 32 and ord(c) != 127).replace("/", "\u2215")
    return name.strip()[:128].strip()


def _letter(key: str) -> str:
    c = key[:1]
    return c.upper() if "a" <= c <= "z" else "#"


def _rule(fld: AutoField, group: ShelfAutoGroup, display: str, key: str, genres) -> dict | None:
    """The child's rule, or ``None`` unless it parses back to exactly ``key``'s values (so the
    child shows the books it was counted from)."""
    built = fld.rule_text(display, key, genres)
    if built is None:
        return None
    text, want = built
    try:
        rule = filters.make_rule(text, group.sort, group.descending)
        q = filters.parse(rule["filter"])
    except filters.FilterError:
        return None
    got = {filters.fold(v.a) for c in q.clauses for v in c.values}
    return rule if got == want and len(q.clauses) == 1 else None


def _sync_group(db, group, fld, counts, names, genres, followed) -> None:
    from lectern.routes.api.shelves import _free_name

    gid = group.shelf_id
    owner = db.get(Shelf, gid).owner_id
    rows = list(db.scalars(select(ShelfAutoChild).where(ShelfAutoChild.group_id == gid)))
    shelves = {s.id: s for s in db.scalars(select(Shelf).where(
        Shelf.id.in_([r.shelf_id for r in rows])))}  # fmt: skip
    buckets = {r.key: r.shelf_id for r in rows if r.kind == "bucket"}
    values = {r.key: shelves[r.shelf_id] for r in rows if r.kind == "value"}

    gone = [s.id for k, s in values.items() if counts.get(k, 0) == 0 and s.id not in followed]
    if gone:
        db.execute(delete(Shelf).where(Shelf.id.in_(gone)))
        values = {k: s for k, s in values.items() if s.id not in gone}

    def parent_for(key: str) -> int:
        if not group.buckets:
            return gid
        letter = _letter(key)
        if letter not in buckets:
            b = Shelf(owner_id=owner, parent_id=gid, name=_free_name(db, owner, gid, letter, 0))
            db.add(b)
            db.flush()
            db.add(ShelfAutoChild(shelf_id=b.id, group_id=gid, kind="bucket", key=letter))
            buckets[letter] = b.id
        return buckets[letter]

    for key, s in values.items():
        rule = {**(s.rule or {}), "sort": group.sort, "desc": group.descending}
        if group.field == "genre" and (fresh := _rule(fld, group, "", key, genres)) is not None:
            rule["filter"] = fresh["filter"]
        if rule != s.rule:
            s.rule = rule
        want = parent_for(key)
        if s.parent_id != want:
            s.name, s.parent_id = _free_name(db, owner, want, s.name, s.id), want

    for key, n in sorted(counts.items()):
        if n < group.min_books or key in values or len(key) > filters.MAX_VALUE_CHARS:
            continue
        display = names.get(key) or ""
        name = _clean_name(display)
        if not name or len(display) > filters.MAX_VALUE_CHARS or key != key.strip():
            continue
        rule = _rule(fld, group, display, key, genres)
        if rule is None:
            continue
        pid = parent_for(key)
        s = Shelf(owner_id=owner, parent_id=pid, name=_free_name(db, owner, pid, name, 0),
                  rule=rule)  # fmt: skip
        db.add(s)
        db.flush()
        db.add(ShelfAutoChild(shelf_id=s.id, group_id=gid, kind="value", key=key))
        values[key] = s

    db.flush()
    used = {s.parent_id for s in values.values()}
    unused = [sid for sid in buckets.values() if sid not in used]
    if unused:
        db.execute(delete(Shelf).where(Shelf.id.in_(unused)))


# --- what one person sees ---------------------------------------------------------------------


@dataclass(frozen=True)
class View:
    """One viewer's generated shelves: ``counts`` the children they may see, with how many of
    their books each holds; ``buckets`` the buckets they may see; ``hidden`` every other
    generated shelf."""

    counts: dict[int, int]
    buckets: frozenset[int]
    hidden: frozenset[int]

    def shows(self, shelf_id: int) -> bool:
        return shelf_id in self.counts or shelf_id in self.buckets


EMPTY = View({}, frozenset(), frozenset())
_cache: OrderedDict[tuple, tuple[float, View]] = OrderedDict()
_cache_lock = threading.Lock()


def view(db: Session, user: User) -> View:
    """What ``user`` sees of the generated shelves (reconciling first). Cached per person and
    library state; a reader's state includes the shelves, items, and grants that decide what
    they see, so a change to any of them shows on their next look."""
    if not db.scalar(select(exists().select_from(ShelfAutoGroup))):
        return EMPTY
    reconcile(db)
    url = _url(db)
    stamp = _stamp(db, view=True, reader=not sees_whole_library(user))
    key = (url, user.id, user.role, stamp, _generation.get(url, 0))
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < VIEW_TTL_S:
            _cache.move_to_end(key)
            return hit[1]
    v = _compute(db, user)
    with _cache_lock:
        _cache[key] = (now, v)
        _cache.move_to_end(key)
        while len(_cache) > _VIEW_CACHE_SIZE:
            _cache.popitem(last=False)
    return v


def _compute(db: Session, user: User) -> View:
    groups = {g.shelf_id: g for g in db.scalars(select(ShelfAutoGroup))}
    kids = db.execute(
        select(ShelfAutoChild.shelf_id, ShelfAutoChild.group_id, ShelfAutoChild.kind,
               ShelfAutoChild.key, Shelf.parent_id)
        .join(Shelf, Shelf.id == ShelfAutoChild.shelf_id)
    ).all()  # fmt: skip
    genres = _genres(db)
    pred = visible_works(user)
    # One grouped query per field in use, however many children there are.
    per_field = {
        f: _counts(db, FIELDS[f], pred, genres)
        for f in {g.field for g in groups.values()}
        if f in FIELDS
    }
    counts: dict[int, int] = {}
    for sid, gid, kind, key, _parent in kids:
        g = groups[gid]
        n = per_field.get(g.field, {}).get(key, 0)
        if kind == "value" and n >= max(g.min_books, 1):
            counts[sid] = n
    shown_parents = {parent for sid, *_rest, parent in kids if sid in counts}
    buckets = frozenset(sid for sid, _g, kind, *_r in kids
                        if kind == "bucket" and sid in shown_parents)  # fmt: skip
    hidden = frozenset(sid for sid, *_r in kids if sid not in counts and sid not in buckets)
    return View(counts, buckets, hidden)
