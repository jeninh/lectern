# Testing migrations on SQLite before 3.35

SQLite 3.35 added `ALTER TABLE … DROP COLUMN`. Several downgrades (0012, 0017, 0018, 0019, 0021,
0023, 0027, 0029, 0032, 0042, 0043, 0045, and since #149 0002, 0004, 0006, 0007, 0010, 0015) take a different path on older SQLite, and the normal
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

The chain walk also runs on whatever SQLite the normal suite has (`test_full_chain_up_down_up`),
because it counts more than rows lost on old SQLite: no downgrade may delete rows or clear
`ON DELETE SET NULL` references on any version (#149). The way to keep that true: never rebuild
a table that other tables reference. A downgrade that drops a column uses `DROP COLUMN` in place
(guarded for 3.35, like the others), and the matching upgrade adds a column only when it is
missing. A column inside a foreign key cannot be dropped that way. 0007 `works.trashed_by` stays
after the downgrade on every version (nullable, never read by the previous release). 0013
`imports.ai_key_id` is removed by rebuilding `imports`, after setting aside the `import_files`
rows and `upload_sessions.import_id` values that the rebuild would cascade into, and restoring
them (`PRAGMA foreign_keys=OFF` does nothing inside a migration's transaction). A batch rebuild
is also fine for a table nothing references (0006 and 0007 `upload_sessions`), or one the chain
has already emptied of referencing tables (0003 `app_passwords`).
On old SQLite the suite also checks that residue column values survive the downgrade and the
re-upgrade.
