"""Shelves and reader grants (docs/contracts.md "Shelves").

Owners and members see the whole library; readers see the works on shelves granted to them.
Shelf management (rename, delete, grants) belongs to the shelf's owner and the owner role; an
``edit`` grant adds the right to add and remove works.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from lectern.access import (
    can_create_shelf,
    can_edit_shelf,
    can_manage_shelf,
    can_see_work,
    visible_works,
)
from lectern.models import Role, Shelf, ShelfGrant, ShelfItem, ShelfPermission, User, Work
from lectern.routes.api.serializers import iso, work_json
from lectern.security import audit
from lectern.security.deps import Auth, AuthCsrf, DbDep
from lectern.security.sessions import client_ip

router = APIRouter(prefix="/shelves")


def _permission(db, user: User, shelf: Shelf) -> str | None:
    if can_manage_shelf(user, shelf):
        return "owner"
    grant = db.get(ShelfGrant, (shelf.id, user.id))
    return grant.permission.value if grant else None


def _shelf_or_404(db, user: User, shelf_id: int) -> tuple[Shelf, str]:
    shelf = db.get(Shelf, shelf_id)
    perm = _permission(db, user, shelf) if shelf else None
    if shelf is None or perm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return shelf, perm


def _shelf_json(db, shelf: Shelf, perm: str, user: User, owners: dict[int, str]) -> dict:
    count = db.scalar(
        select(func.count())
        .select_from(ShelfItem)
        .join(Work, Work.id == ShelfItem.work_id)
        .where(ShelfItem.shelf_id == shelf.id, visible_works(user))
    )
    grants = db.scalar(
        select(func.count()).select_from(ShelfGrant).where(ShelfGrant.shelf_id == shelf.id)
    )
    return {
        "id": shelf.id,
        "name": shelf.name,
        "owner": owners.get(shelf.owner_id),
        "owner_id": shelf.owner_id,
        "permission": perm,
        "work_count": count or 0,
        "grant_count": grants or 0,
        "smart": shelf.rule is not None,
        "created_at": iso(shelf.created_at),
        "updated_at": iso(shelf.updated_at),
    }


@router.get("")
def list_shelves(auth: Auth, db: DbDep) -> dict:
    user = auth.user
    granted = select(ShelfGrant.shelf_id).where(ShelfGrant.user_id == user.id)
    stmt = select(Shelf).order_by(func.lower(Shelf.name))
    if user.role == Role.MEMBER:
        stmt = stmt.where(or_(Shelf.owner_id == user.id, Shelf.id.in_(granted)))
    elif user.role == Role.READER:
        stmt = stmt.where(Shelf.id.in_(granted))
    owners = {u.id: u.username for u in db.scalars(select(User))}
    out = []
    for shelf in db.scalars(stmt):
        perm = _permission(db, user, shelf) or ("view" if user.role == Role.OWNER else None)
        if perm:
            out.append(_shelf_json(db, shelf, perm, user, owners))
    return {"shelves": out, "can_create": can_create_shelf(user)}


class NameIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)


def _audit(db, request: Request, auth, action: str, target: str, detail: str | None = None):
    audit.record(
        db,
        action,
        actor_user_id=auth.user.id,
        target=target[:256],
        ip=client_ip(request),
        detail=detail,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_shelf(request: Request, auth: AuthCsrf, db: DbDep, body: NameIn) -> dict:
    if not can_create_shelf(auth.user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "name_required")
    if db.scalar(select(Shelf).where(Shelf.owner_id == auth.user.id, Shelf.name == name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "duplicate_name")
    shelf = Shelf(owner_id=auth.user.id, name=name)
    db.add(shelf)
    db.flush()
    _audit(db, request, auth, "shelf.create", name)
    db.commit()
    return _shelf_json(db, shelf, "owner", auth.user, {auth.user.id: auth.user.username})


@router.get("/{shelf_id}")
def get_shelf(auth: Auth, db: DbDep, shelf_id: int) -> dict:
    shelf, perm = _shelf_or_404(db, auth.user, shelf_id)
    owners = {u.id: u.username for u in db.scalars(select(User))}
    rows = db.execute(
        select(ShelfItem, Work)
        .join(Work, Work.id == ShelfItem.work_id)
        .options(selectinload(Work.files))
        .where(ShelfItem.shelf_id == shelf.id, visible_works(auth.user))
        .order_by(ShelfItem.position, ShelfItem.added_at)
    ).all()
    return {
        **_shelf_json(db, shelf, perm, auth.user, owners),
        "works": [work_json(w) for _, w in rows],
    }


@router.patch("/{shelf_id}")
def rename_shelf(request: Request, auth: AuthCsrf, db: DbDep, shelf_id: int, body: NameIn) -> dict:
    shelf, perm = _shelf_or_404(db, auth.user, shelf_id)
    if perm != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the shelf's owner can rename it")
    name = body.name.strip()
    clash = db.scalar(
        select(Shelf).where(
            Shelf.owner_id == shelf.owner_id, Shelf.name == name, Shelf.id != shelf.id
        )
    )
    if clash is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "duplicate_name")
    old, shelf.name = shelf.name, name
    _audit(db, request, auth, "shelf.rename", name, old)
    db.commit()
    return _shelf_json(db, shelf, perm, auth.user, {})


@router.delete("/{shelf_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shelf(request: Request, auth: AuthCsrf, db: DbDep, shelf_id: int) -> Response:
    shelf, perm = _shelf_or_404(db, auth.user, shelf_id)
    if perm != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the shelf's owner can delete it")
    _audit(db, request, auth, "shelf.delete", shelf.name)
    db.delete(shelf)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _editable(db, auth, shelf_id: int) -> Shelf:
    shelf, _ = _shelf_or_404(db, auth.user, shelf_id)
    if not can_edit_shelf(db, auth.user, shelf):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "View-only shelf")
    return shelf


def _check_work(db, user: User, work_id: int) -> None:
    # Members may add any work; a reader with edit only works they can already see.
    if db.get(Work, work_id) is None or not can_see_work(db, user, work_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Work not found")


class ItemsIn(BaseModel):
    work_ids: list[int] = Field(max_length=5000)


@router.put("/{shelf_id}/items")
def replace_items(
    request: Request, auth: AuthCsrf, db: DbDep, shelf_id: int, body: ItemsIn
) -> dict:
    shelf = _editable(db, auth, shelf_id)
    ids = list(dict.fromkeys(body.work_ids))
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


@router.get("/{shelf_id}/grants")
def list_grants(auth: Auth, db: DbDep, shelf_id: int) -> dict:
    shelf = _managed(db, auth, shelf_id)
    users = {u.id: u for u in db.scalars(select(User))}
    return {
        "grants": [
            {
                "user": {
                    "id": g.user_id,
                    "username": users[g.user_id].username,
                    "display_name": users[g.user_id].display_name,
                    "role": users[g.user_id].role.value,
                },
                "permission": g.permission.value,
                "granted_by": users[g.granted_by].username if g.granted_by in users else None,
                "created_at": iso(g.created_at),
            }  # fmt: skip
            for g in shelf.grants
        ]
    }


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
