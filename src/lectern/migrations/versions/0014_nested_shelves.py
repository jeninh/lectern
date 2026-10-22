"""Nested shelves: shelves.parent_id; names unique among one owner's siblings

Every existing shelf becomes a top-level folder (parent_id NULL). Shelf names were unique per
owner; they are now unique per owner among siblings, which every existing row satisfies.

Revision ID: 0014_nested_shelves
Revises: 0013
Create Date: 2026-10-22

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_nested_shelves"
down_revision = "0013"
branch_labels = None
depends_on = None


# Changing shelves' constraints makes SQLite rebuild the table (copy, DROP, rename). With
# foreign_keys=ON that DROP cascades into every row referencing a shelf, so those rows are
# copied aside first and put back after the rebuild. Clearing before restoring makes this
# correct whether or not the connection enforces foreign keys.
_SAVE = (
    "CREATE TEMP TABLE _keep_items AS SELECT * FROM shelf_items",
    "CREATE TEMP TABLE _keep_grants AS SELECT * FROM shelf_grants",
)
_RESTORE = (
    "DELETE FROM shelf_items",
    "INSERT INTO shelf_items SELECT * FROM temp._keep_items",
    "DROP TABLE temp._keep_items",
    "DELETE FROM shelf_grants",
    "INSERT INTO shelf_grants SELECT * FROM temp._keep_grants",
    "DROP TABLE temp._keep_grants",
)


def _save_children() -> None:
    for sql in _SAVE:
        op.execute(sql)


def _restore_children() -> None:
    for sql in _RESTORE:
        op.execute(sql)


def upgrade() -> None:
    _save_children()
    with op.batch_alter_table("shelves") as batch:
        batch.add_column(sa.Column("parent_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_shelves_parent_id", "shelves", ["parent_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_index("ix_shelves_parent_id", ["parent_id"])
        batch.drop_constraint("uq_shelf_owner_name", type_="unique")
    _restore_children()
    op.create_index(
        "uq_shelf_sibling_name",
        "shelves",
        ["owner_id", sa.text("coalesce(parent_id, 0)"), "name"],
        unique=True,
    )


def downgrade() -> None:
    # One owner may now have the same name in two folders, and the old schema has nowhere to
    # put a sub-shelf: everything goes back to the top level and later duplicates get the
    # shelf id appended, so no shelf, item, or grant is lost.
    op.drop_index("uq_shelf_sibling_name", table_name="shelves")
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, owner_id, name FROM shelves ORDER BY id")).all()
    taken = {(owner, name) for _, owner, name in rows}
    seen: set[tuple[int, str]] = set()
    for sid, owner, name in rows:
        if (owner, name) not in seen:
            seen.add((owner, name))
            continue
        n, new = 1, f"{name[:116]} ({sid})"
        while (owner, new) in taken:  # "Kids (4)" may already be a shelf of its own
            n += 1
            new = f"{name[:112]} ({sid}-{n})"
        taken.add((owner, new))
        conn.execute(sa.text("UPDATE shelves SET name = :n WHERE id = :i"), {"n": new, "i": sid})
    _save_children()
    with op.batch_alter_table("shelves") as batch:
        batch.drop_index("ix_shelves_parent_id")
        batch.drop_constraint("fk_shelves_parent_id", type_="foreignkey")
        batch.drop_column("parent_id")
        batch.create_unique_constraint("uq_shelf_owner_name", ["owner_id", "name"])
    _restore_children()
