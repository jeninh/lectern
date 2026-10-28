"""Cover and metadata editor groundwork (issue #85)

``works.cover_font``: the font variant of the generated cover (NULL = the default display
face). ``works.subtitle``, ``works.original_published``, ``works.contributors`` (a JSON list
of ``{name, role}``): descriptive fields the editor can set.

Revision ID: 0042_cover_metadata
Revises: 0029_playback_speed
Create Date: 2026-10-28

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042_cover_metadata"
down_revision = "0029_playback_speed"
branch_labels = None
depends_on = None

_COLUMNS = ("cover_font", "subtitle", "original_published", "contributors")


def upgrade() -> None:
    # Plain ADD COLUMN (nullable, no default), never a batch rebuild of works: a rebuild
    # DROPs the table, which with foreign_keys=ON would cascade into every row that
    # references it. Already there after a downgrade on SQLite before 3.35 (see downgrade).
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("works")}
    new = {
        "cover_font": sa.Column("cover_font", sa.String(length=16), nullable=True),
        "subtitle": sa.Column("subtitle", sa.String(length=512), nullable=True),
        "original_published": sa.Column("original_published", sa.String(length=32), nullable=True),
        "contributors": sa.Column("contributors", sa.JSON(), nullable=True),
    }
    for name in _COLUMNS:
        if name not in have:
            op.add_column("works", new[name])


def downgrade() -> None:
    # ALTER TABLE … DROP COLUMN needs SQLite 3.35. Before that the columns stay: they are
    # nullable and the previous release's code never reads them.
    if (op.get_bind().dialect.server_version_info or (0,)) >= (3, 35):
        for name in reversed(_COLUMNS):
            op.drop_column("works", name)
