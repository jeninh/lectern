# Export your data

**Settings → Security → Export your data → Download my data** gives you one zip of your own
data. It is streamed as it is made, so a Raspberry Pi can build it without holding it all in
memory.

## What is in it

- `data.json`: everything, with a `schema_version`, one list of rows per table.
- `csv/`: the same tables, one CSV each. A cell that would start a spreadsheet formula gets a
  leading `'`; the JSON is exact.
- `markdown/highlights.md` and `markdown/notes.md`: your highlights (with their notes) and your
  ratings and notes on books, grouped by book.

Tables: `annotations`, `reviews` (rating in half stars and as `stars`, notes), `read_throughs`,
`reading_status`, `reading_stats_days`, `vocabulary`, `shelves`, `shelf_items`, `want_to_read`,
and `listening_queue`.

## Reading stats

`reading_stats_days` is your per-day reading and listening totals per book, the rollups the
stats page is built from. They are exported rather than the raw reading events, so the export
stays complete if Lectern prunes old raw events.

## What is never in it

Only your own rows, and only books you can see today (the same rule as the rest of the app, for
owners, members, readers, and guests). Never other people's data, and never passwords, sessions,
sync keys, app passwords, AI keys, pairing codes, or device secrets.
