"""Server-wide switches the owner sets in Admin, persisted in ``instance_settings``."""

from __future__ import annotations

from sqlalchemy.orm import Session

from lectern.models import InstanceSetting

# key -> default. Metadata lookup sends titles, authors, and ISBNs to Open Library and Google
# Books, so it is off until the owner turns it on.
DEFAULTS: dict[str, bool] = {"metadata_lookup_enabled": False}


def get(db: Session, key: str) -> bool:
    row = db.get(InstanceSetting, key)
    return DEFAULTS[key] if row is None or row.value is None else bool(row.value)


def all_settings(db: Session) -> dict[str, bool]:
    return {k: get(db, k) for k in DEFAULTS}


def set_value(db: Session, key: str, value: bool, user_id: int) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    row = db.get(InstanceSetting, key)
    if row is None:
        db.add(InstanceSetting(key=key, value=value, updated_by=user_id))
    else:
        row.value, row.updated_by = value, user_id
    db.flush()
