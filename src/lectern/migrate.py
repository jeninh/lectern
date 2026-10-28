"""Run Alembic migrations programmatically at startup."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def include_name(name: str | None, type_: str, _parents: dict) -> bool:
    """What autogenerate compares: not the highlights' FTS5 index (migration 0026), a virtual
    table and its shadow tables that the models do not describe."""
    return not (type_ == "table" and (name or "").startswith("annotations_fts"))


def upgrade_to_head(engine: Engine) -> None:
    cfg = alembic_config(str(engine.url))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
