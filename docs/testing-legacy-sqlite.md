# Testing migrations on SQLite before 3.35

SQLite 3.35 added `ALTER TABLE … DROP COLUMN`. Several downgrades (0012, 0017, 0018, 0019, 0021,
0023, 0027, 0029, 0032, 0042, 0043, 0045) take a different path on older SQLite, and the normal
suite fakes that version. `tests/test_sqlite_legacy.py` runs the whole chain on a real old
library instead: base → head → base → head, one revision at a time, with a row in every table, so
a downgrade that loses rows or fails fails the test. After the first upgrade and the last, the
database must match the ORM models.

```sh
scripts/test-sqlite-legacy.sh            # extra arguments go to pytest
```

It needs Docker and network access, and no secrets. It runs in
`python:3.12-slim-bullseye` (SQLite 3.34.1), pinned by digest in the script, with the
dependencies from `uv.lock`. The runtime image is bookworm, whose SQLite is 3.40, so it cannot
be used. The script sets `LECTERN_REQUIRE_LEGACY_SQLITE=1`, so if the image ever ships SQLite
3.35 or newer the test fails instead of skipping. To bump the digest, see the comment in the
script.

Under plain `uv run pytest` the test (marker `legacy_sqlite`) skips on any newer SQLite.

**This is local only, and is not run in GitHub Actions.** Rerun it after adding a migration:
a new revision that drops a column needs the same SQLite-version guard as the ones above.

Downgrading 0007, 0010 and 0013 loses rows on every SQLite version, because their batch table
copies cascade through foreign keys. The test skips the row count for those three steps
(`KNOWN_LOSSY`); that is a separate bug, not part of this suite.
