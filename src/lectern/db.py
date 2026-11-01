"""SQLite engine with WAL and the pragmas a single-writer server wants."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import lectern.reading_state  # noqa: F401  (registers the reading-state flush hook)
from lectern.config import Settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _apply_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA temp_store=MEMORY")
    cur.close()
    # Accent- and case-insensitive text matching for library filters (lectern.filters).
    from lectern.filters import fold

    dbapi_conn.create_function("lectern_fold", 1, fold, deterministic=True)


def make_engine(url: str) -> Engine:
    engine = create_engine(url, future=True, connect_args={"check_same_thread": False})
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def init_engine(settings: Settings) -> Engine:
    global _engine, _SessionLocal
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    _engine = make_engine(settings.db_url)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    assert _engine is not None, "init_engine() not called"
    return _engine


def session_factory() -> sessionmaker[Session]:
    assert _SessionLocal is not None, "init_engine() not called"
    return _SessionLocal


def get_db() -> Generator[Session, None, None]:
    db = session_factory()()
    try:
        yield db
    finally:
        db.close()


def optimize(engine: Engine) -> None:
    """Give SQLite's query planner statistics for tables that have none (or very different
    row counts since the last run). Without them it assumes every index is equally selective:
    on a library of thousands of works that chose, for the shelf counts, a plan that probed
    ``shelf_items`` once per work and shelf (116 ms instead of 3 ms). Cheap when nothing
    changed; run at startup after the migrations."""
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA optimize=0x10002")
