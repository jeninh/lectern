"""AI keys: filing imports (confirm_import) moves from ``upload`` to its own ``file`` scope

Existing keys that could file books (they hold ``upload``) keep that ability: they get
``file`` too. Downgrading drops ``file`` again (``upload`` covered filing before).

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-21

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

ORDER = ("upload", "file", "read", "organize")
ai_keys = sa.table("ai_keys", sa.column("id", sa.Integer), sa.column("scopes", sa.JSON))


def _rewrite(change) -> None:
    bind = op.get_bind()
    for key_id, scopes in bind.execute(sa.select(ai_keys.c.id, ai_keys.c.scopes)).all():
        before = list(scopes or [])
        after = [s for s in ORDER if s in change(set(before))]
        if after != before:
            bind.execute(ai_keys.update().where(ai_keys.c.id == key_id).values(scopes=after))


def upgrade() -> None:
    _rewrite(lambda s: s | {"file"} if "upload" in s else s)


def downgrade() -> None:
    _rewrite(lambda s: s - {"file"})
