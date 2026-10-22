"""AI keys: filing imports (confirm_import) moves from ``upload`` to its own ``file`` scope

Upgrade: every key that could file books (it holds ``upload``) keeps that ability by getting
``file`` too. Imports remember the AI key that opened them (``imports.ai_key_id``): a key
files only its own uploads. Imports already pending have none, so the person files those.

Downgrade never widens a key. Before 0012, ``upload`` also meant filing, and ``file`` did not
exist, so:

- ``upload`` + ``file`` → ``upload`` (the same abilities as before 0012);
- ``upload`` without ``file`` (filing deliberately turned off) → ``upload`` is dropped, since
  keeping it would turn filing back on;
- ``file`` without ``upload`` → dropped (the old schema cannot express it);
- a key left with no scopes is revoked (the old code never created an empty key).

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-21

"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

ORDER = ("upload", "file", "read", "organize")
ai_keys = sa.table(
    "ai_keys",
    sa.column("id", sa.Integer),
    sa.column("scopes", sa.JSON),
    sa.column("revoked_at", sa.DateTime(timezone=True)),
)


def _rewrite(change) -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.select(ai_keys.c.id, ai_keys.c.scopes, ai_keys.c.revoked_at)).all()
    for key_id, scopes, revoked_at in rows:
        before = list(scopes or [])
        after = [s for s in ORDER if s in change(set(before))]
        values = {}
        if after != before:
            values["scopes"] = after
        if not after and before and revoked_at is None:
            values["revoked_at"] = datetime.now(UTC)
        if values:
            bind.execute(ai_keys.update().where(ai_keys.c.id == key_id).values(**values))


def _upgrade_scopes(s: set[str]) -> set[str]:
    return s | {"file"} if "upload" in s else s


def _downgrade_scopes(s: set[str]) -> set[str]:
    if "upload" in s and "file" not in s:
        s = s - {"upload"}
    return s - {"file"}


def upgrade() -> None:
    _rewrite(_upgrade_scopes)
    with op.batch_alter_table("imports") as batch:
        batch.add_column(sa.Column("ai_key_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_imports_ai_key_id", "ai_keys", ["ai_key_id"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("imports") as batch:
        batch.drop_constraint("fk_imports_ai_key_id", type_="foreignkey")
        batch.drop_column("ai_key_id")
    _rewrite(_downgrade_scopes)
