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
