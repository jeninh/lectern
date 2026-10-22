# Fixtures: Kindle formats

Original test content written for Lectern (no third-party text or images), built by
`make_fixtures.py` with calibre 6.13 `ebook-convert` from an EPUB the script writes:
*The Lighthouse Keeper’s Almanac* by the made-up authors Ada Quill and Bram Ellis, ISBN
9780000000002 (a valid check digit, not a real book), language `en`, publisher "Lectern Test
Fixtures", two chapters, one PNG drawing, and a 600×900 dark-blue JPEG cover. The script then
sets the ASIN (EXTH 113) to the made-up `B0LECTERN1`.

| File | What |
| --- | --- |
| `lighthouse.mobi` | Mobipocket 6 (MOBI7 only), `--mobi-file-type old` |
| `lighthouse-joint.mobi` | joint MOBI7 + KF8, `--mobi-file-type both` |
| `lighthouse.azw3` | KF8 only |

DRM-flagged copies are not checked in: tests make them by setting the encryption field of a
fixture's MOBI header (record 0, or only the KF8 header of the joint file).

calibre output varies between versions, so rebuilding will not be byte-identical; the
checked-in files are the reference.
