"""Shelves: the Library's nested folders, and reader grants (docs/contracts.md "Shelves").

Owners and members see the whole library; readers see the works on shelves granted to them,
including every shelf nested inside a granted one. Shelf management (rename, move, delete,
grants) belongs to the owner role and to whoever owns the shelf or a shelf above it; an
``edit`` grant adds the right to add and remove works and to make sub-shelves.

What someone cannot see is never named or counted: a shelf whose parent is hidden from them
has ``parent_id: null`` and a ``path`` that starts at itself, and counts include only what
they may see. Hidden shelves are ``404``, exactly like shelves that do not exist.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from lectern.access import (
    MAX_DEPTH,
    can_create_shelf,
    can_edit_library,
    can_see_work,
    depth_below,
    depth_of,
    lineage,
    shelf_perms,
    visible_parent,
    visible_path,
    visible_works,
)
from lectern.access import (
    shelf_permission as _shelf_permission,
)
from lectern.models import Role, Shelf, ShelfGrant, ShelfItem, ShelfPermission, User, Work
from lectern.routes.api.serializers import iso, work_json
from lectern.security import audit
from lectern.security.deps import Auth, AuthCsrf, DbDep, SettingsDep
from lectern.security.sessions import client_ip

router = APIRouter(prefix="/shelves")


def _permission(db, user: User, shelf: Shelf) -> str | None:
    return _shelf_permission(db, user, shelf)


def _shelf_or_404(db, user: User, shelf_id: int) -> tuple[Shelf, str]:
    shelf = db.get(Shelf, shelf_id)
    perm = _permission(db, user, shelf) if shelf else None
    if shelf is None or perm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return shelf, perm


class Tree:
    """One person's view of the shelves: which they see, with what permission, and the
    counts of what they may see in each. Loaded once per request."""

    def __init__(self, db: Session, user: User) -> None:
        self.db, self.user = db, user
        self.shelves, self.perms = shelf_perms(db, user)
        self.children: dict[int | None, list[Shelf]] = {}
        for sid in self.perms:
            s = self.shelves[sid]
            self.children.setdefault(visible_parent(s, self.perms), []).append(s)
        for kids in self.children.values():
            kids.sort(key=lambda s: (s.name.casefold(), s.id))
        self._counts: dict[int, int] | None = None

    def visible(self, shelf_id: int) -> Shelf:
        if shelf_id not in self.perms:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        return self.shelves[shelf_id]

    def path(self, shelf: Shelf) -> list[Shelf]:
        return visible_path(shelf, self.shelves, self.perms)

    def counts(self) -> dict[int, int]:
        if self._counts is None:
            self._counts = dict(
                self.db.execute(
                    select(ShelfItem.shelf_id, func.count())
                    .join(Work, Work.id == ShelfItem.work_id)
                    .where(ShelfItem.shelf_id.in_(list(self.perms)), visible_works(self.user))
                    .group_by(ShelfItem.shelf_id)
                ).all()
            )
        return self._counts

    def json(self, shelf: Shelf, owners: dict[int, str]) -> dict:
        grants = self.db.scalar(
            select(func.count()).select_from(ShelfGrant).where(ShelfGrant.shelf_id == shelf.id)
        )
        return {
            "id": shelf.id,
            "name": shelf.name,
            "parent_id": visible_parent(shelf, self.perms),
            "path": [s.name for s in self.path(shelf)],
            "owner": owners.get(shelf.owner_id),
            "owner_id": shelf.owner_id,
            "permission": self.perms[shelf.id],
            "work_count": self.counts().get(shelf.id, 0),
            "shelf_count": len(self.children.get(shelf.id, [])),
            "grant_count": grants or 0,
            "smart": shelf.rule is not None,
            "created_at": iso(shelf.created_at),
            "updated_at": iso(shelf.updated_at),
        }


def _owners(db) -> dict[int, str]:
    return {u.id: u.username for u in db.scalars(select(User))}


def _shelf_json(db, shelf: Shelf, user: User) -> dict:
    return Tree(db, user).json(shelf, _owners(db))


@router.get("")
def list_shelves(auth: Auth, db: DbDep) -> dict:
    """Every shelf this person may see, top-level first and then depth-first, each child
    list in name order (a tree walk of ``parent_id``)."""
    tree = Tree(db, auth.user)
    owners = _owners(db)
    out: list[dict] = []

    def walk(parent: int | None) -> None:
        for s in tree.children.get(parent, []):
            out.append(tree.json(s, owners))
            walk(s.id)

    walk(None)
    return {"shelves": out, "can_create": can_create_shelf(auth.user)}


def _clean_name(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("name_required")
    if "/" in v:
        # "/" separates shelves in a path (Cmd/Ctrl+K, MCP tools).
        raise ValueError("name_has_slash")
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise ValueError("control_characters")
    return v


class CreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    parent_id: int | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _clean_name(v)


class PatchIn(BaseModel):
    """Rename (``name``), move (``parent_id``; ``null`` = top level), or both."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    parent_id: int | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        return None if v is None else _clean_name(v)


def _audit(db, request: Request, auth, action: str, target: str, detail: str | None = None):
    audit.record(
        db,
        action,
        actor_user_id=auth.user.id,
        target=target[:256],
        ip=client_ip(request),
        detail=detail,
    )


def _clash(db, owner_id: int, parent_id: int | None, name: str, exclude: int | None = None):
    stmt = select(Shelf.id).where(
        Shelf.owner_id == owner_id,
        Shelf.parent_id.is_(None) if parent_id is None else Shelf.parent_id == parent_id,
        Shelf.name == name,
    )
    if exclude is not None:
        stmt = stmt.where(Shelf.id != exclude)
    return db.scalar(stmt.limit(1))


def _free_name(db, owner_id: int, parent_id: int | None, name: str, exclude: int) -> str:
    """``name``, or ``name (2)``, ``(3)``… if a sibling of the same owner already has it."""
    candidate, n = name, 1
    while _clash(db, owner_id, parent_id, candidate, exclude) is not None:
        n += 1
        suffix = f" ({n})"
        candidate = name[: 128 - len(suffix)] + suffix
    return candidate


def _target_parent(db, user: User, parent_id: int | None) -> Shelf | None:
    """The folder something is being created in or moved into, if this person may put
    shelves there: the top level for owners and members, or a visible shelf they can edit."""
    if user.role == Role.READER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
    if parent_id is None:
        if not can_create_shelf(user):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return None
    parent, perm = _shelf_or_404(db, user, parent_id)
    if perm not in ("owner", "edit"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "View-only shelf")
    if parent.rule is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "smart_shelf")
    return parent


def _too_deep() -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        {"detail": "too_deep", "max_depth": MAX_DEPTH,
         "message": f"Shelves can be nested at most {MAX_DEPTH} deep."},
    )  # fmt: skip


@router.post("", status_code=status.HTTP_201_CREATED)
def create_shelf(request: Request, auth: AuthCsrf, db: DbDep, body: CreateIn) -> dict:
    """A new shelf in ``parent_id`` (``null``: top level), owned by its creator."""
    parent = _target_parent(db, auth.user, body.parent_id)
    if parent is not None and depth_of(parent) + 1 > MAX_DEPTH:
        raise _too_deep()
    if _clash(db, auth.user.id, body.parent_id, body.name) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "duplicate_name")
    shelf = Shelf(owner_id=auth.user.id, name=body.name, parent=parent)
    db.add(shelf)
    db.flush()
    where = f"in shelf:{parent.id}" if parent else None
    _audit(db, request, auth, "shelf.create", body.name, where)
    db.commit()
    return _shelf_json(db, shelf, auth.user)


@router.get("/{shelf_id}")
def get_shelf(auth: Auth, db: DbDep, shelf_id: int) -> dict:
    """The shelf, its visible ancestors (``ancestors``, top first: breadcrumbs), its
    sub-shelves, and the works directly on it in shelf order."""
    tree = Tree(db, auth.user)
    shelf = tree.visible(shelf_id)
    owners = _owners(db)
    rows = db.execute(
        select(ShelfItem, Work)
        .join(Work, Work.id == ShelfItem.work_id)
        .options(selectinload(Work.files))
        .where(ShelfItem.shelf_id == shelf.id, visible_works(auth.user))
        .order_by(ShelfItem.position, ShelfItem.added_at)
    ).all()
    return {
        **tree.json(shelf, owners),
        "ancestors": [{"id": s.id, "name": s.name} for s in tree.path(shelf)[:-1]],
        "shelves": [tree.json(s, owners) for s in tree.children.get(shelf.id, [])],
        "works": [work_json(w) for _, w in rows],
    }


@router.patch("/{shelf_id}")
def update_shelf(request: Request, auth: AuthCsrf, db: DbDep, shelf_id: int, body: PatchIn) -> dict:
    """Rename and/or move a shelf with everything in it. Moving needs the right to manage
    the shelf and to put shelves in the destination; a shelf never moves into itself or
    below itself (``409 shelf_cycle``) or deeper than ``MAX_DEPTH`` (``409 too_deep``)."""
    shelf, perm = _shelf_or_404(db, auth.user, shelf_id)
    if perm != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the shelf's owner can change it")
    moving = "parent_id" in body.model_fields_set and body.parent_id != shelf.parent_id
    name = body.name if body.name is not None else shelf.name
    parent_id = body.parent_id if moving else shelf.parent_id
    if moving:
        parent = _target_parent(db, auth.user, body.parent_id)
        if parent is not None:
            if shelf in lineage(parent):
                raise HTTPException(status.HTTP_409_CONFLICT, "shelf_cycle")
            shelves = {s.id: s for s in db.scalars(select(Shelf))}
            if depth_of(parent) + depth_below(shelf.id, shelves) > MAX_DEPTH:
                raise _too_deep()
    if _clash(db, shelf.owner_id, parent_id, name, shelf.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "duplicate_name")
    if name != shelf.name:
        _audit(db, request, auth, "shelf.rename", name, shelf.name)
        shelf.name = name
    if moving:
        _audit(
            db, request, auth, "shelf.move", shelf.name,
            f"shelf:{shelf.parent_id or 'top'} -> shelf:{body.parent_id or 'top'}",
        )  # fmt: skip
        shelf.parent = parent
    db.commit()
    db.expire_all()
    return _shelf_json(db, db.get(Shelf, shelf.id), auth.user)


def _subtree(shelf: Shelf) -> list[Shelf]:
    out, todo = [], [shelf]
    while todo:
        s = todo.pop()
        if s in out:
            continue
        out.append(s)
        todo.extend(s.children)
    return out


@router.delete("/{shelf_id}", response_model=None)
def delete_shelf(
    request: Request,
    auth: AuthCsrf,
    db: DbDep,
    settings: SettingsDep,
    shelf_id: int,
    contents: Literal["move_up", "trash"] | None = Query(default=None),
) -> Response | dict:
    """Delete a shelf. Works are never deleted with it. A shelf with sub-shelves or works in
    it needs ``contents``:

    - ``move_up``: its sub-shelves and works go to its parent (top level: the sub-shelves
      become top-level shelves and the works leave this shelf). ``204``.
    - ``trash``: every work in it or below it goes to the Trash (restorable, one Undo batch),
      then it and its sub-shelves are deleted. ``200 {batch_id, results}``; if any work
      could not be trashed, nothing is deleted (``deleted: false``).

    Without ``contents`` a non-empty shelf is ``409 shelf_not_empty`` with the counts.
    """
    shelf, perm = _shelf_or_404(db, auth.user, shelf_id)
    if perm != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the shelf's owner can delete it")
    subtree = _subtree(shelf)
    ids = [s.id for s in subtree]
    work_ids = list(
        db.scalars(
            select(ShelfItem.work_id)
            .join(Work, Work.id == ShelfItem.work_id)
            .where(ShelfItem.shelf_id.in_(ids), visible_works(auth.user))
            .distinct()
        )
    )
    direct_works = db.scalar(
        select(func.count())
        .select_from(ShelfItem)
        .join(Work, Work.id == ShelfItem.work_id)
        .where(ShelfItem.shelf_id == shelf.id, visible_works(auth.user))
    )
    if (shelf.children or direct_works) and contents is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"detail": "shelf_not_empty", "shelves": len(subtree) - 1, "works": len(work_ids),
             "direct_works": direct_works or 0, "sub_shelves": len(shelf.children)},
        )  # fmt: skip

    if contents == "trash":
        from lectern.routes.api import editor

        if not can_edit_library(auth.user):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        if len(work_ids) > editor.BULK_MAX:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"detail": "selection_too_large", "limit": editor.BULK_MAX},
            )
        name = shelf.name
        out: dict = {"batch_id": None, "results": [], "deleted": True}
        if work_ids:
            out = editor.bulk_edit(
                request, auth, db, settings,
                editor.BulkIn(work_ids=work_ids, action="trash"),
            )  # fmt: skip
            out["deleted"] = all(r["ok"] for r in out["results"])
        if out["deleted"]:
            shelf = db.get(Shelf, shelf_id)
            _audit(db, request, auth, "shelf.delete", name,
                   f"with {len(ids) - 1} sub-shelves; {len(work_ids)} works trashed")  # fmt: skip
            db.delete(shelf)
            db.commit()
        return out

    if contents == "move_up" and (shelf.children or direct_works):
        parent = shelf.parent
        if parent is None:
            if shelf.children and not can_create_shelf(auth.user):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        elif _permission(db, auth.user, parent) not in ("owner", "edit"):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "parent_not_editable")
        for child in list(shelf.children):
            child.name = _free_name(db, child.owner_id, shelf.parent_id, child.name, child.id)
            child.parent = parent
            db.flush()
        if parent is not None:
            on_parent = set(
                db.scalars(select(ShelfItem.work_id).where(ShelfItem.shelf_id == parent.id))
            )
            last = db.scalar(
                select(func.max(ShelfItem.position)).where(ShelfItem.shelf_id == parent.id)
            )
            for item in shelf.items:  # trashed works too: a restore finds them there
                if item.work_id not in on_parent:
                    last = (last or 0) + 1
                    db.add(ShelfItem(shelf_id=parent.id, work_id=item.work_id, position=last))
        db.flush()
        db.expire(shelf)
    _audit(db, request, auth, "shelf.delete", shelf.name,
           "contents moved up" if contents == "move_up" else None)  # fmt: skip
    db.delete(shelf)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _editable(db, auth, shelf_id: int) -> Shelf:
    shelf, perm = _shelf_or_404(db, auth.user, shelf_id)
    if perm not in ("owner", "edit"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "View-only shelf")
    return shelf


def _check_work(db, user: User, work_id: int) -> None:
    # Members may add any work; a reader with edit only works they can already see.
    if db.get(Work, work_id) is None or not can_see_work(db, user, work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Work not found")


def readers_who_see(db, shelf: Shelf, other_than: int) -> int:
    """How many readers besides ``other_than`` see this shelf (a grant on it or above it)."""
    return (
        db.scalar(
            select(func.count(func.distinct(ShelfGrant.user_id)))
            .join(User, User.id == ShelfGrant.user_id)
            .where(
                ShelfGrant.shelf_id.in_([s.id for s in lineage(shelf)]),
                User.id != other_than,
                User.role == Role.READER,
            )
        )
        or 0
    )


def _check_reader_adding(db, user: User, shelf: Shelf) -> None:
    """A reader with an edit grant may not add works to a shelf another reader can see: that
    would widen the other reader's library with books only the owner should grant."""
    if user.role == Role.READER and readers_who_see(db, shelf, user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "shelf_shared_with_readers")


class ItemsIn(BaseModel):
    work_ids: list[int] = Field(max_length=5000)


@router.put("/{shelf_id}/items")
def replace_items(
    request: Request, auth: AuthCsrf, db: DbDep, shelf_id: int, body: ItemsIn
) -> dict:
    shelf = _editable(db, auth, shelf_id)
    ids = list(dict.fromkeys(body.work_ids))
    if set(ids) - {i.work_id for i in shelf.items}:
        _check_reader_adding(db, auth.user, shelf)
    for wid in ids:
        _check_work(db, auth.user, wid)
    shelf.items.clear()
    db.flush()
    for pos, wid in enumerate(ids):
        shelf.items.append(ShelfItem(work_id=wid, position=pos))
    _audit(db, request, auth, "shelf.items", shelf.name, f"{len(ids)} works")
    db.commit()
    return {"work_ids": ids}


class ItemIn(BaseModel):
    work_id: int


@router.post("/{shelf_id}/items", status_code=status.HTTP_201_CREATED)
def add_item(request: Request, auth: AuthCsrf, db: DbDep, shelf_id: int, body: ItemIn) -> dict:
    shelf = _editable(db, auth, shelf_id)
    _check_work(db, auth.user, body.work_id)
    if db.get(ShelfItem, (shelf.id, body.work_id)) is None:
        _check_reader_adding(db, auth.user, shelf)
        last = db.scalar(select(func.max(ShelfItem.position)).where(ShelfItem.shelf_id == shelf.id))
        db.add(ShelfItem(shelf_id=shelf.id, work_id=body.work_id, position=(last or 0) + 1))
        _audit(db, request, auth, "shelf.add", shelf.name, f"work:{body.work_id}")
        db.commit()
    return {"shelf_id": shelf.id, "work_id": body.work_id}


@router.delete("/{shelf_id}/items/{work_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_item(
    request: Request, auth: AuthCsrf, db: DbDep, shelf_id: int, work_id: int
) -> Response:
    shelf = _editable(db, auth, shelf_id)
    _check_work(db, auth.user, work_id)
    item = db.get(ShelfItem, (shelf.id, work_id))
    if item is not None:
        db.delete(item)
        _audit(db, request, auth, "shelf.remove", shelf.name, f"work:{work_id}")
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _managed(db, auth, shelf_id: int) -> Shelf:
    shelf, perm = _shelf_or_404(db, auth.user, shelf_id)
    if perm != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the shelf's owner manages access")
    return shelf


def _grant_json(g: ShelfGrant, users: dict[int, User]) -> dict:
    return {
        "user": {
            "id": g.user_id,
            "username": users[g.user_id].username,
            "display_name": users[g.user_id].display_name,
            "role": users[g.user_id].role.value,
        },
        "permission": g.permission.value,
        "granted_by": users[g.granted_by].username if g.granted_by in users else None,
        "created_at": iso(g.created_at),
    }


@router.get("/{shelf_id}/grants")
def list_grants(auth: Auth, db: DbDep, shelf_id: int) -> dict:
    """The shelf's own grants, and (``inherited``) the grants on shelves above it that also
    reach it, from the ancestors this person manages too: a grant covers the whole subtree."""
    shelf = _managed(db, auth, shelf_id)
    users = {u.id: u for u in db.scalars(select(User))}
    inherited = []
    for above in lineage(shelf)[1:]:
        if _permission(db, auth.user, above) != "owner":
            break
        inherited += [
            {**_grant_json(g, users), "shelf": {"id": above.id, "name": above.name}}
            for g in above.grants
        ]
    return {"grants": [_grant_json(g, users) for g in shelf.grants], "inherited": inherited}


class GrantIn(BaseModel):
    permission: Literal["view", "edit"]


@router.put("/{shelf_id}/grants/{user_id}")
def put_grant(
    request: Request, auth: AuthCsrf, db: DbDep, shelf_id: int, user_id: int, body: GrantIn
) -> dict:
    shelf = _managed(db, auth, shelf_id)
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.role == Role.READER and auth.user.role != Role.OWNER:
        # What a reader may see is the owner's decision alone.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only_owner_grants_readers")
    grant = db.get(ShelfGrant, (shelf.id, user_id))
    if grant is None:
        grant = ShelfGrant(shelf_id=shelf.id, user_id=user_id, granted_by=auth.user.id,
                           permission=ShelfPermission(body.permission))  # fmt: skip
        db.add(grant)
    else:
        grant.permission = ShelfPermission(body.permission)
    _audit(db, request, auth, "shelf.grant", shelf.name, f"{user.username}:{body.permission}")
    db.commit()
    return {"user_id": user_id, "permission": body.permission}


@router.delete("/{shelf_id}/grants/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_grant(
    request: Request, auth: AuthCsrf, db: DbDep, shelf_id: int, user_id: int
) -> Response:
    shelf = _managed(db, auth, shelf_id)
    grant = db.get(ShelfGrant, (shelf.id, user_id))
    if grant is not None:
        db.delete(grant)
        user = db.get(User, user_id)
        _audit(
            db, request, auth, "shelf.revoke", shelf.name, user.username if user else str(user_id)
        )
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
