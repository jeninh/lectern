"""The whole Alembic chain, base -> head -> base -> head, on a real SQLite older than 3.35.

SQLite before 3.35 has no ALTER TABLE ... DROP COLUMN, so the downgrade guards (#64, #99, #103,
#136) take their fallback paths there. Those paths are otherwise only simulated (tests/
test_filters.py fakes the version), so this runs on the library itself. It skips on a newer
SQLite; run it with scripts/test-sqlite-legacy.sh, which uses a pinned python:3.12-slim-bullseye
(SQLite 3.34.1) and sets LECTERN_REQUIRE_LEGACY_SQLITE=1 so image drift fails the test instead
of skipping it. Local only, never CI (docs/testing-legacy-sqlite.md).
"""

from __future__ import annotations

import datetime as dt
import enum
import os
import sqlite3

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from tests.conftest import schema_diff

from lectern.db import make_engine
from lectern.migrate import alembic_config
from lectern.models import Base

LEGACY = sqlite3.sqlite_version_info < (3, 35)

pytestmark = [
    pytest.mark.legacy_sqlite,
    pytest.mark.skipif(
        not LEGACY and not os.environ.get("LECTERN_REQUIRE_LEGACY_SQLITE"),
        reason=f"SQLite {sqlite3.sqlite_version} is 3.35+; run scripts/test-sqlite-legacy.sh",
    ),
]

# Downgrades that drop a table, or clear a derived one, on purpose: not a data-loss signal.
CLEARED_BY_DESIGN = {"annotations", "pdf_texts"}

# Pre-existing losses on every SQLite version, not only before 3.35 (so not what this suite is
# about): downgrading these revisions batch-copies a table that other tables reference, and
# with foreign keys on the copy cascades into them. Each is a follow-up; delete the entry once
# fixed. Revisions are listed by id; their rows are not counted when they step down.
KNOWN_LOSSY: set[str] = {"0007", "0010", "0013"}


# Columns whose CHECK constraints the type-driven values below would break (models.py).
OVERRIDES: dict[str, dict[str, object]] = {
    "invites": {"role": "MEMBER", "used_by": None, "used_at": None},
    "read_throughs": {"started_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC)},
    "sentences": {"char_start": 0, "char_end": 5},
}


def _value(col: sa.Column, n: int):
    t = col.type
    while isinstance(t, sa.types.TypeDecorator):  # e.g. UTCDateTime: seed its underlying kind
        t = t.impl
    if isinstance(t, sa.Enum):
        first = t.enum_class and next(iter(t.enum_class)) if t.enum_class else None
        if isinstance(first, enum.Enum):
            return first.name
        return t.enums[0]
    if isinstance(t, sa.Boolean):
        return 0
    if isinstance(t, sa.Integer):
        return n
    if isinstance(t, sa.Float | sa.Numeric):
        return 1.0
    if isinstance(t, sa.DateTime):
        return dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    if isinstance(t, sa.Date):
        return dt.date(2026, 1, 1)
    if isinstance(t, sa.LargeBinary):
        return b"\x01"
    if isinstance(t, sa.JSON):
        return {}
    return f"seed-{col.table.name}-{col.name}-{n}"


def seed_every_table(engine) -> dict[str, int]:
    """One row in every model table, parents first, so every table-rebuild path has data (and
    foreign keys with cascades something to cascade into). Values come from the column types."""
    seeded: dict[str, int] = {}
    failed: dict[str, str] = {}
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name.startswith("annotations_fts"):
                continue
            values = {}
            for col in table.columns:
                fks = list(col.foreign_keys)
                if fks:
                    parent = fks[0].column.table.name
                    if parent not in seeded:
                        if col.nullable:
                            continue
                        raise AssertionError(f"cannot seed {table.name}: {parent} has no row")
                    values[col.name] = conn.execute(
                        sa.text(f"SELECT {fks[0].column.name} FROM {parent} LIMIT 1")
                    ).scalar()
                elif col.primary_key and isinstance(col.type, sa.Integer):
                    values[col.name] = 1
                elif col.default is not None or col.server_default is not None or col.nullable:
                    continue
                else:
                    values[col.name] = _value(col, 1)
            values.update(OVERRIDES.get(table.name, {}))
            try:
                with conn.begin_nested():
                    conn.execute(sa.insert(table).values(**values))
            except sa.exc.DBAPIError as e:
                failed[table.name] = str(e.orig)
                continue
            seeded[table.name] = 1
    assert not failed, failed
    return seeded


def counts(engine, tables) -> dict[str, int]:
    with engine.connect() as conn:
        existing = set(sa.inspect(conn).get_table_names())
        return {
            t: conn.execute(sa.text(f'SELECT count(*) FROM "{t}"')).scalar()
            for t in tables
            if t in existing
        }


def test_full_chain_up_down_up_on_legacy_sqlite(tmp_path):
    # Fails (rather than quietly testing a newer library) if the pinned image drifts.
    assert sqlite3.sqlite_version_info < (3, 35), sqlite3.sqlite_version

    engine = make_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    # No monkeypatch: the migrations read the version from the real driver.
    assert engine.dialect.dbapi.sqlite_version_info < (3, 35)
    cfg = alembic_config(str(engine.url))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, heads
    # Oldest first. A merge revision (0020) has two parents; walk_revisions orders them safely.
    order = [r.revision for r in reversed(list(script.walk_revisions()))]
    assert len(order) == len(set(order)) >= 39

    def run(fn, target):
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            fn(cfg, target)

    for revision in order:  # up, one revision at a time
        run(command.upgrade, revision)
    with engine.connect() as conn:
        assert schema_diff(conn) == []
    seeded = seed_every_table(engine)
    assert len(seeded) > 30

    before = counts(engine, seeded)
    steps = 0
    while True:  # down, one revision at a time (a merge steps to both parents), to base
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_heads()
        if not current:
            break
        for head in current:  # a fork (0019) has one head per branch, each stepped alone
            parents = script.get_revision(head).down_revision
            if isinstance(parents, tuple):  # a merge (0020) of a diamond: back to its fork point
                forks = {script.get_revision(p).down_revision for p in parents}
                assert len(forks) == 1, forks
                parents = forks.pop()
            run(command.downgrade, parents or "base")
        steps += 1
        after = counts(engine, seeded)
        lost = {
            t: (n, after[t])
            for t, n in before.items()
            if t in after
            and after[t] < n
            and t not in CLEARED_BY_DESIGN
            and not KNOWN_LOSSY.intersection(current)
        }
        assert not lost, f"downgrade from {current} lost rows: {lost}"
        before = after
        assert steps <= len(order)
    assert steps >= len(order) - 3  # the 0019 diamond (0019 x2, 0020) is two steps, not three

    for revision in order:  # and back up
        run(command.upgrade, revision)
    with engine.connect() as conn:
        assert schema_diff(conn) == []
