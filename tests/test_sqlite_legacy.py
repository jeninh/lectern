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

legacy_only = [
    pytest.mark.legacy_sqlite,
    pytest.mark.skipif(
        not LEGACY and not os.environ.get("LECTERN_REQUIRE_LEGACY_SQLITE"),
        reason=f"SQLite {sqlite3.sqlite_version} is 3.35+; run scripts/test-sqlite-legacy.sh",
    ),
]


def on_legacy_sqlite(fn):
    for mark in legacy_only:
        fn = mark(fn)
    return fn


# Downgrades that drop a table, or clear a derived one, on purpose: not a data-loss signal.
CLEARED_BY_DESIGN = {"annotations", "pdf_texts"}

# Columns a downgrade leaves behind on SQLite before 3.35 (no DROP COLUMN), with a marker value
# set at head: re-upgrading must keep it (those migrations only add what is missing).
# The 0007/0010 columns outside a foreign key are dropped on 3.35+, so there they start over.
RESIDUE: dict[tuple[str, str], object] = {
    ("works", "data_saver_enabled"): 0,  # 0017
    ("users", "data_saver"): 1,  # 0017
    ("invites", "guest"): '{"marker": 1}',  # 0045
    ("works", "cover_style"): "marker",  # 0010
    ("works", "cover_seed"): 7,  # 0010
    ("works", "cover_tint"): "#123456",  # 0002
    ("users", "show_technical_details"): 1,  # 0004
    ("users", "download_cap_per_hour"): 9,  # 0006
    ("jobs", "crashes"): 3,  # 0015
    ("users", "reading_timezone"): "Marker/Zone",  # 0043
    ("works", "series"): "Marker series",  # 0007
    ("works", "authors"): '[{"name": "Marker", "sort": "Marker"}]',  # 0007
    ("users", "auto_file_imports"): 1,  # 0007
}
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


def fk_values(engine) -> dict[tuple[str, str], int]:
    """Non-null values per nullable foreign-key column. ON DELETE SET NULL clears these
    without deleting a row, so counts() cannot see it."""
    out = {}
    with engine.connect() as conn:
        insp = sa.inspect(conn)
        for table in Base.metadata.sorted_tables:
            if table.name not in insp.get_table_names():
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.foreign_keys and col.nullable and col.name in have:
                    out[(table.name, col.name)] = conn.execute(
                        sa.text(
                            f'SELECT count(*) FROM "{table.name}" WHERE "{col.name}" IS NOT NULL'
                        )
                    ).scalar()
    return out


@on_legacy_sqlite
def test_full_chain_up_down_up_on_legacy_sqlite(tmp_path):
    # Fails (rather than quietly testing a newer library) if the pinned image drifts.
    assert sqlite3.sqlite_version_info < (3, 35), sqlite3.sqlite_version
    assert make_engine("sqlite://").dialect.dbapi.sqlite_version_info < (3, 35)
    _chain(tmp_path)


def test_full_chain_up_down_up(tmp_path):
    """The same walk on whatever SQLite the suite runs on: no downgrade loses rows or clears
    foreign keys, on any version (0007, 0010 and 0013 once did, through batch table copies)."""
    _chain(tmp_path)


def _residue(engine) -> dict[tuple[str, str], object]:
    """The marker columns that exist now, with their values."""
    out = {}
    with engine.connect() as conn:
        insp = sa.inspect(conn)
        for table, column in RESIDUE:
            if table in insp.get_table_names() and column in {
                c["name"] for c in insp.get_columns(table)
            }:
                out[(table, column)] = conn.execute(
                    sa.text(f'SELECT "{column}" FROM "{table}"')
                ).scalar()
    return out


def _residue_survives_reupgrade(engine, run, revision):
    """Right after a downgrade step, upgrade it again and back: a column the downgrade left
    behind (no DROP COLUMN before 3.35) must keep its values through the re-upgrade, which
    only adds what is missing. Columns the downgrade dropped start over, so are not checked."""
    left = _residue(engine)
    run(command.upgrade, revision)
    now = _residue(engine)
    assert {k: now[k] for k in left} == left, f"re-upgrade to {revision} changed residue values"
    # Back to the state the walk was in (the next downgrade steps from this revision again).
    run(command.downgrade, "-1")


def _chain(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    # No monkeypatch: the migrations read the version from the real driver.
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
    with engine.begin() as conn:
        for (table, column), value in RESIDUE.items():
            conn.execute(sa.text(f'UPDATE "{table}" SET "{column}" = :v'), {"v": value})
    before = counts(engine, seeded)
    fk_before = fk_values(engine)
    assert fk_before[("works", "trashed_by")] == fk_before[("upload_sessions", "import_id")] == 1
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
        if (
            LEGACY  # elsewhere DROP COLUMN leaves no residue, and a re-upgrade rebuilds jobs
            and len(current) == 1
            and isinstance(script.get_revision(current[0]).down_revision, str)
        ):
            _residue_survives_reupgrade(engine, run, current[0])
        steps += 1
        after = counts(engine, seeded)
        lost = {
            t: (n, after[t])
            for t, n in before.items()
            if t in after and after[t] < n and t not in CLEARED_BY_DESIGN
        }
        assert not lost, f"downgrade from {current} lost rows: {lost}"
        before = after
        fk_after = fk_values(engine)
        # A column that went with its table is not a loss, nor are references to a table the
        # step dropped on purpose (0009 drops ai_keys): SET NULL is the point.
        gone = set(seeded) - set(after)
        cleared = {
            k: (n, fk_after[k])
            for k, n in fk_before.items()
            if k in fk_after
            and fk_after[k] < n
            and k[0] not in CLEARED_BY_DESIGN
            and not {fk.column.table.name for fk in Base.metadata.tables[k[0]].c[k[1]].foreign_keys}
            & gone
        }
        assert not cleared, f"downgrade from {current} cleared SET NULL references: {cleared}"
        fk_before = fk_after
        assert steps <= len(order)
    assert steps >= len(order) - 3  # the 0019 diamond (0019 x2, 0020) is two steps, not three

    for revision in order:  # and back up
        run(command.upgrade, revision)
    with engine.connect() as conn:
        assert schema_diff(conn) == []
