"""Read-aloud (milestone 4): web read-along support and EPUB 3 Media Overlay export.

- :mod:`lectern.readaloud.text` maps a spine item's DOM onto the frozen normalised-text
  coordinates of :func:`lectern.positions.normalize_spine_text`.
- :mod:`lectern.readaloud.overlay` builds the read-aloud EPUB (a copy of the original with
  sentence spans, SMIL files, and embedded audio).
- :mod:`lectern.readaloud.cache` gathers a work's inputs from the database and caches the
  generated file under the work's ``derived/`` directory.
"""
