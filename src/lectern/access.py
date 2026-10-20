"""Who may see which works.

Rule: owners and members see the whole library. Readers see only works that sit on a shelf
they hold a grant for. Shelf *management* (rename, delete, grant) belongs to the shelf's
owner and to the owner role; a grant with ``EDIT`` permission adds the right to add and
remove works but not to manage the shelf itself.

Step 0 ships the predicates; milestone 2 applies :func:`visible_works` to ``/api/library``,
``/api/works/{id}``, ``/api/continue``, covers, downloads, OPDS, and the audio/sentence routes.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, exists, select, true
from sqlalchemy.orm import Session

from lectern.models import Role, Shelf, ShelfGrant, ShelfItem, ShelfPermission, User, Work


def sees_whole_library(user: User) -> bool:
    return user.role in (Role.OWNER, Role.MEMBER)


def visible_works(user: User) -> ColumnElement[bool]:
    """SQL predicate on ``Work`` rows for what ``user`` may see; ``AND`` it into any query."""
    if sees_whole_library(user):
        return true()
    granted = (
        select(ShelfItem.work_id)
        .join(ShelfGrant, ShelfGrant.shelf_id == ShelfItem.shelf_id)
        .where(ShelfGrant.user_id == user.id)
    )
    return Work.id.in_(granted)


def can_see_work(db: Session, user: User, work_id: int) -> bool:
    if sees_whole_library(user):
        return True
    return bool(db.scalar(select(exists().where(Work.id == work_id, visible_works(user)))))


def can_manage_shelf(user: User, shelf: Shelf) -> bool:
    return user.role == Role.OWNER or shelf.owner_id == user.id


def can_edit_shelf(db: Session, user: User, shelf: Shelf) -> bool:
    if can_manage_shelf(user, shelf):
        return True
    grant = db.get(ShelfGrant, (shelf.id, user.id))
    return grant is not None and grant.permission == ShelfPermission.EDIT


def can_create_shelf(user: User) -> bool:
    return user.role in (Role.OWNER, Role.MEMBER)
