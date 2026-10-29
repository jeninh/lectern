"""Server-wide switches the owner sets in Admin, persisted in ``instance_settings``."""

from __future__ import annotations

from sqlalchemy.orm import Session

from lectern.models import InstanceSetting

# key -> default. Metadata lookup sends titles, authors, and ISBNs to Open Library and Google
# Books, so it is off until the owner turns it on. Offline mode (lectern.offline_mode) refuses
# every outbound internet connection. Data-saver copies (lectern.jobs) are made unless the owner
# turns them off for the whole library.
DEFAULTS: dict[str, bool] = {
    "metadata_lookup_enabled": False,
    "offline_mode": False,
    "data_saver_enabled": True,
    # The owner paused the whole job queue (lectern.queue.queue_paused, the Jobs page).
    "queue_paused": False,
}
# The library settings editors see and the owner changes through /api/library-settings.
LIBRARY = ("metadata_lookup_enabled", "data_saver_enabled")


def get(db: Session, key: str) -> bool:
    row = db.get(InstanceSetting, key)
    return DEFAULTS[key] if row is None or row.value is None else bool(row.value)


GENRE_MAP = "genre_map"
MAX_GENRES = 40
MAX_GENRE_TAGS = 20


def genre_map(db: Session) -> dict[str, list[str]]:
    """``{genre: [tags…]}``: the tags that make up each genre (lectern.auto_shelves), at most
    :data:`MAX_GENRES` genres of :data:`MAX_GENRE_TAGS` tags; ``{}`` when not set. A stored
    value of another shape counts as unset, and string entries only are kept."""
    row = db.get(InstanceSetting, GENRE_MAP)
    value = row.value if row is not None else None
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[str]] = {}
    for genre, tags in list(value.items())[:MAX_GENRES]:
        if isinstance(genre, str) and isinstance(tags, list):
            out[genre] = [t for t in tags if isinstance(t, str)][:MAX_GENRE_TAGS]
    return out


def all_settings(db: Session) -> dict[str, bool]:
    return {k: get(db, k) for k in LIBRARY}


def set_value(db: Session, key: str, value: bool, user_id: int) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    row = db.get(InstanceSetting, key)
    if row is None:
        db.add(InstanceSetting(key=key, value=value, updated_by=user_id))
    else:
        row.value, row.updated_by = value, user_id
    db.flush()
