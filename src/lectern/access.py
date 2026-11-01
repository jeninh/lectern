"""Who may see which works and shelves.

Rule: owners and members see the whole library. Readers see only works that sit on a shelf
they hold a grant for, or on any shelf nested inside one: a grant covers the shelf's whole
subtree. Shelf *management* (rename, move, delete, grant) belongs to the owner role and to
whoever owns the shelf or any shelf above it; a grant with ``EDIT`` permission (on the shelf
or above it) adds the right to add and remove works and to make sub-shelves, but not to
manage the shelf itself.

A shelf is visible to someone with any permission on it. Visibility is closed downwards
(everything under a visible shelf is visible), so the visible part of a shelf's ancestry is
one unbroken chain: :func:`visible_parent` is the nearest visible ancestor, and a shelf whose
parent is hidden appears at that person's top level, without the hidden name.

:func:`visible_works` is applied to ``/api/library``, ``/api/works/{id}``, ``/api/continue``,
covers, downloads, OPDS, the audio/sentence routes, and the MCP tools.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import ColumnElement, Select, and_, exists, or_, select
from sqlalchemy.orm import Session

from lectern.models import (
    Role,
    Shelf,
    ShelfAutoChild,
    ShelfGrant,
    ShelfItem,
    ShelfPermission,
    User,
    Work,
)

# How deep shelves may nest: a top-level shelf is depth 1.
MAX_DEPTH = 8

Perm = Literal["owner", "edit", "view"]
_RANK = {"view": 1, "edit": 2, "owner": 3}


def sees_whole_library(user: User) -> bool:
    # A guest is held to their grants whatever role the row says (deny by default).
    return user.guest is None and user.role in (Role.OWNER, Role.MEMBER)


def _subtree_ids(roots: Select) -> Select:
    """Ids of the shelves in ``roots`` (a one-column select of shelf ids) and every shelf
    below them. ``UNION`` (not ``UNION ALL``) also ends on a cycle the API never allows."""
    tree = roots.cte(recursive=True)
    tree = tree.union(select(Shelf.id).join(tree, Shelf.parent_id == tree.c[0]))
    return select(tree.c[0])


def granted_shelf_ids(user: User) -> Select:
    """Shelves ``user`` holds a grant on, and everything nested inside them."""
    return _subtree_ids(select(ShelfGrant.shelf_id).where(ShelfGrant.user_id == user.id))


def own_shelf_ids(user: User) -> Select:
    """Shelves ``user`` owns or holds a grant on, and everything nested inside them: "their"
    shelves (Up next), as opposed to every shelf the owner role can see."""
    if user.guest is not None:  # a guest owns no shelf
        return granted_shelf_ids(user)
    granted = select(ShelfGrant.shelf_id).where(ShelfGrant.user_id == user.id)
    return _subtree_ids(
        select(Shelf.id).where(or_(Shelf.owner_id == user.id, Shelf.id.in_(granted)))
    )


def visible_shelf_ids(user: User) -> Select:
    """Every shelf ``user`` may see (SQL); :func:`shelf_perms` is the same in Python."""
    if user.role == Role.OWNER and user.guest is None:
        return select(Shelf.id)
    return own_shelf_ids(user)


def visible_works(user: User) -> ColumnElement[bool]:
    """SQL predicate on ``Work`` rows for what ``user`` may see; ``AND`` it into any query.

    Works in the trash are invisible to everyone, owner included; only the trash routes
    (``lectern.trash``) look at them.
    """
    live = Work.trashed_at.is_(None)
    if sees_whole_library(user):
        return live
    on_granted = select(ShelfItem.work_id).where(ShelfItem.shelf_id.in_(granted_shelf_ids(user)))
    return and_(live, Work.id.in_(on_granted))


def can_see_work(db: Session, user: User, work_id: int) -> bool:
    return bool(db.scalar(select(exists().where(Work.id == work_id, visible_works(user)))))


def can_edit_library(user: User) -> bool:
    """Adding, editing, and trashing works: owners and members, never readers or guests."""
    return user.guest is None and user.role in (Role.OWNER, Role.MEMBER)


def lineage(shelf: Shelf) -> list[Shelf]:
    """``shelf`` and its ancestors, nearest first (bounded, in case of a corrupt cycle)."""
    out: list[Shelf] = []
    node: Shelf | None = shelf
    while node is not None and len(out) <= MAX_DEPTH + 1 and node not in out:
        out.append(node)
        node = node.parent
    return out


def can_manage_shelf(user: User, shelf: Shelf) -> bool:
    """Rename, move, delete, and share: the owner role, or the owner of this shelf or of any
    shelf above it."""
    if user.guest is not None:
        return False
    return user.role == Role.OWNER or any(s.owner_id == user.id for s in lineage(shelf))


def shelf_permission(db: Session, user: User, shelf: Shelf) -> Perm | None:
    """``owner`` (manage), ``edit`` (add and remove works, make sub-shelves), ``view``, or
    ``None`` (hidden). Grants on any ancestor count. A generated auto-shelf this person must
    not see (lectern.auto_shelves.view) is ``None`` for everyone, its manager included."""
    if shelf.auto_child is not None:
        from lectern import auto_shelves  # it imports this module

        if not auto_shelves.view(db, user).shows(shelf.id):
            return None
    if can_manage_shelf(user, shelf):
        return "owner"
    ids = [s.id for s in lineage(shelf)]
    perms = set(
        db.scalars(
            select(ShelfGrant.permission).where(
                ShelfGrant.user_id == user.id, ShelfGrant.shelf_id.in_(ids)
            )
        )
    )
    if ShelfPermission.EDIT in perms and user.guest is None:  # a guest only ever views
        return "edit"
    return "view" if perms else None


def can_edit_shelf(db: Session, user: User, shelf: Shelf) -> bool:
    return shelf_permission(db, user, shelf) in ("owner", "edit")


def can_create_shelf(user: User) -> bool:
    """Top-level shelves; sub-shelves also need edit on the parent (:func:`can_edit_shelf`)."""
    return user.guest is None and user.role in (Role.OWNER, Role.MEMBER)


def shelf_perms(db: Session, user: User) -> tuple[dict[int, Shelf], dict[int, Perm]]:
    """Every shelf, and ``user``'s permission on each one they may see (one query each).
    Generated auto-shelves hidden from them (lectern.auto_shelves.view) are left out."""
    from lectern import auto_shelves  # it imports this module

    view = auto_shelves.view(db, user)
    # Each shelf with its generated kind in one statement: a generated shelf the view does not
    # positively show (one made since, by another request) is hidden.
    rows = db.execute(
        select(Shelf, ShelfAutoChild.kind).outerjoin(
            ShelfAutoChild, ShelfAutoChild.shelf_id == Shelf.id
        )
    ).all()
    shelves = {s.id: s for s, _kind in rows}
    hidden = {s.id for s, kind in rows if kind is not None and not view.shows(s.id)}
    grants = {
        g.shelf_id: g.permission
        for g in db.scalars(select(ShelfGrant).where(ShelfGrant.user_id == user.id))
    }
    memo: dict[int, Perm | None] = {}

    def perm(sid: int, depth: int = 0) -> Perm | None:
        if sid in memo:
            return memo[sid]
        s = shelves.get(sid)
        if s is None or depth > MAX_DEPTH + 1:
            return None
        if user.guest is not None:  # a guest owns nothing and only views what is granted
            own: Perm | None = "view" if sid in grants else None
        elif user.role == Role.OWNER or s.owner_id == user.id:
            own = "owner"
        elif sid in grants:
            own = "edit" if grants[sid] == ShelfPermission.EDIT else "view"
        else:
            own = None
        above = perm(s.parent_id, depth + 1) if s.parent_id is not None else None
        best = max((own, above), key=lambda p: _RANK.get(p or "", 0))
        memo[sid] = best
        return best

    return shelves, {
        sid: p for sid in shelves if sid not in hidden and (p := perm(sid)) is not None
    }


def takes_items(shelf: Shelf) -> bool:
    """Whether works can be put on ``shelf`` by hand: not a smart shelf (generated auto-shelf
    children are smart shelves) and not an auto-shelves group or bucket."""
    return shelf.rule is None and shelf.auto_kind not in ("group", "bucket")


def visible_parent(shelf: Shelf, perms: dict[int, Perm]) -> int | None:
    """The parent's id if ``user`` may see it (``perms`` from :func:`shelf_perms`), else
    ``None``: the shelf is at their top level and the hidden parent is never named."""
    return shelf.parent_id if shelf.parent_id in perms else None


def visible_path(shelf: Shelf, shelves: dict[int, Shelf], perms: dict[int, Perm]) -> list[Shelf]:
    """The visible chain from this person's top level down to ``shelf``."""
    chain = [shelf]
    while (pid := visible_parent(chain[-1], perms)) is not None and len(chain) <= MAX_DEPTH:
        chain.append(shelves[pid])
    return chain[::-1]


def depth_below(shelf_id: int, shelves: dict[int, Shelf]) -> int:
    """Levels in the subtree rooted at ``shelf_id``, the shelf itself included (1 = a leaf)."""
    children: dict[int, list[int]] = {}
    for s in shelves.values():
        if s.parent_id is not None:
            children.setdefault(s.parent_id, []).append(s.id)

    def height(sid: int, seen: frozenset[int]) -> int:
        kids = [c for c in children.get(sid, []) if c not in seen]
        return 1 + max((height(c, seen | {c}) for c in kids), default=0)

    return height(shelf_id, frozenset({shelf_id}))


def depth_of(shelf: Shelf) -> int:
    """1 for a top-level shelf."""
    return len(lineage(shelf))
