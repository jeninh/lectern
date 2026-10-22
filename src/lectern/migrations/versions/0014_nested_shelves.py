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


def upgrade() -> None:
    with op.batch_alter_table("shelves") as batch:
        batch.add_column(sa.Column("parent_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_shelves_parent_id", "shelves", ["parent_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_index("ix_shelves_parent_id", ["parent_id"])
        batch.drop_constraint("uq_shelf_owner_name", type_="unique")
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
    op.execute(
        """
        UPDATE shelves SET name = substr(name, 1, 116) || ' (' || id || ')'
        WHERE EXISTS (
            SELECT 1 FROM shelves AS o
            WHERE o.owner_id = shelves.owner_id AND o.name = shelves.name AND o.id < shelves.id
        )
        """
    )
    with op.batch_alter_table("shelves") as batch:
        batch.drop_index("ix_shelves_parent_id")
        batch.drop_constraint("fk_shelves_parent_id", type_="foreignkey")
        batch.drop_column("parent_id")
        batch.create_unique_constraint("uq_shelf_owner_name", ["owner_id", "name"])
