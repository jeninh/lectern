"""Email-in: a per-person address on the configured mailbox, and a shelf for its books

``email_inboxes``: one row per person who turned email-in on (lectern.email_in): the tag of
their address (``<local>+<token>@<domain>``) and the shelf emailed books go on.
``imports.shelf_id``: the shelf a filed import's work is put on (NULL = none, as before).
Neither has a foreign key, so a deleted shelf is noticed instead of silently forgotten.

Revision ID: 0021_email_in
Revises: 0020_merge_0019_heads
Create Date: 2026-10-27

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_email_in"
down_revision = "0020_merge_0019_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_inboxes",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("token", sa.String(32), nullable=False),
        sa.Column("shelf_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("token", name="uq_email_inboxes_token"),
    )
    # Plain ADD COLUMN, never a batch rebuild (see 0017): nullable, no default. Already there
    # after a downgrade on SQLite before 3.35 (see downgrade): kept, since a shelf an import
    # was meant for is still its person's choice (a shelf deleted since is noticed at filing).
    if "shelf_id" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("imports")}:
        op.add_column("imports", sa.Column("shelf_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    # ALTER TABLE … DROP COLUMN needs SQLite 3.35. Before that the column stays: it is
    # nullable and 0020's code never reads it. Never a batch rebuild of imports (see 0017).
    if (op.get_bind().dialect.server_version_info or (0,)) >= (3, 35):
        op.drop_column("imports", "shelf_id")
    op.drop_table("email_inboxes")
