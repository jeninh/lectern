"""Auto-shelves: shelves generated from authors, tags, genres, languages, narrators (#73)

``shelf_auto_groups``: a shelf whose children lectern.auto_shelves keeps in step with the
library, one per value of ``field``. ``shelf_auto_children``: those children, and the A–Z
buckets between them and the group. ``ix_works_updated_at``: the library stamp that tells the
engine something changed.

Revision ID: 0041_auto_shelves
Revises: 0042_cover_metadata
Create Date: 2026-10-28

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041_auto_shelves"
down_revision = "0042_cover_metadata"
branch_labels = None
depends_on = None


# New tables only, never ALTER on shelves: a batch rebuild of shelves on SQLite would cascade
# into its items and grants and silently drop the uq_shelf_sibling_name expression index.


def upgrade() -> None:
    op.create_table(
        "shelf_auto_groups",
        sa.Column("shelf_id", sa.Integer(), nullable=False),
        sa.Column("field", sa.String(length=16), nullable=False),
        sa.Column("min_books", sa.Integer(), server_default="2", nullable=False),
        sa.Column("child_order", sa.String(length=8), server_default="name", nullable=False),
        sa.Column("buckets", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("sort", sa.String(length=16), server_default="title", nullable=False),
        sa.Column("descending", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["shelf_id"], ["shelves.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("shelf_id"),
    )
    op.create_table(
        "shelf_auto_children",
        sa.Column("shelf_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("key", sa.String(length=256), nullable=False),
        sa.ForeignKeyConstraint(["shelf_id"], ["shelves.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["shelf_auto_groups.shelf_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("shelf_id"),
        sa.UniqueConstraint("group_id", "kind", "key", name="uq_shelf_auto_children_key"),
    )
    op.create_index(
        "ix_shelf_auto_children_group_id", "shelf_auto_children", ["group_id"], unique=False
    )
    op.create_index("ix_works_updated_at", "works", ["updated_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_works_updated_at", table_name="works")
    # The generated shelves themselves stay, as ordinary shelves (buckets empty, children
    # smart shelves with the same rule), so a DeviceFollow on one keeps working.
    op.drop_index("ix_shelf_auto_children_group_id", table_name="shelf_auto_children")
    op.drop_table("shelf_auto_children")
    op.drop_table("shelf_auto_groups")
