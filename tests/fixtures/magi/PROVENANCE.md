# Fixture: *The Gift of the Magi* (text + narration)

A short public-domain story paired with a public-domain LibriVox narration, used to test the
sentence-map schema, `lectern.positions`, and (from milestone 3) the alignment pipeline. It is
a **test fixture only**: Lectern has no feature that searches for or downloads books or audio.

## Files

| File | What | Size | SHA-256 |
| --- | --- | --- | --- |
| `the-gift-of-the-magi.epub` | Project Gutenberg EPUB #7256, unmodified | 69,775 B | `072d238c79d0d485837dbe17ea8f5a6eb1906603a863f0902b8cdb9017ba8479` |
| `excerpt.mp3` | 77.17 s cut from the LibriVox recording (see below) | 231,962 B | `6fbc9ec61ad5b6384dd3b0b4363cdf50bca7dd891ac1d2f26b042da91d60bd91` |
| `sentence_map.json` | Hand-checked sentence map for the first 15 sentences | 4 KB | — |
| `fetch_audio.py` | Downloads and verifies the full recording; can rebuild `excerpt.mp3` | — | — |

## Text

- Work: *The Gift of the Magi* by O. Henry (William Sydney Porter, 1862–1910), first published 1905.
- Source: Project Gutenberg eBook #7256, <https://www.gutenberg.org/ebooks/7256>.
- Exact file: <https://www.gutenberg.org/ebooks/7256.epub.noimages> (OPF `dc:date opf:event="conversion"`
  = 2026-10-03T16:35:42Z; `dc:source` = <https://www.gutenberg.org/files/7256/7256-h/7256-h.htm>).
- Status: the OPF declares `dc:rights` "Public domain in the USA." The author died in 1910, so the text
  is also public domain in life+70 jurisdictions.
- Licence of the file: the EPUB carries the Project Gutenberg License. It is redistributed here
  unmodified and free of charge, as that licence permits, with its header and trademark notices intact.
  Project Gutenberg is a registered trademark and is not affiliated with this project.
- Note: Project Gutenberg regenerates EPUBs from time to time, so a fresh download may not match the
  hash above. The checked-in copy is the one the sentence map refers to.
- Spine: index 0 = cover wrapper, 1 = Gutenberg header, **2 = the story**
  (`OEBPS/5088469616467574847_7256-h-1.htm.html`, 29,283 characters after normalisation).

## Audio

- Recording: *The Gift of the Magi*, read by Betsie Bush for LibriVox, catalogued 2005-12-09,
  runtime 13:22, one track. Catalogue page: <https://librivox.org/the-gift-of-the-magi-by-o-henry/>.
- Hosted at the Internet Archive, item `giftofmagi`: <https://archive.org/details/giftofmagi>.
  Item metadata declares `licenseurl` <http://creativecommons.org/licenses/publicdomain/>
  (LibriVox recordings are released into the public domain by their volunteer readers).
- Original upload: `gift_of_the_magi_henry_blb.mp3`, 12,840,278 B, VBR, 802.51 s,
  MD5 `9067145a0b8456282856f489debadc79`, SHA-1 `f283750365c6e95458c19225ba77d9032371b9de`.
- File used: the Archive's 64 kb/s derivative
  <https://archive.org/download/giftofmagi/gift_of_the_magi_henry_blb_64kb.mp3>,
  6,420,608 B, 802.536 s, MP3 24 kHz stereo 64 kb/s,
  MD5 `66d071b9e6e174e830ff3a9c407282ab`, SHA-1 `a93db30397acfeda0f75bd7ab999dd8037b97b69`,
  SHA-256 `4dfe1b8eb7d7617360b6ae3f6b27fc342f8e14a718ca80e603e4780c398717f9`.
- The full file is not checked in (6.4 MB). `fetch_audio.py` downloads it and verifies the SHA-256.
- `excerpt.mp3` is seconds **24.50–101.60** of the 64 kb/s file (the LibriVox preamble and title
  end at 23.2 s), re-encoded to mono 22.05 kHz 24 kb/s MP3 with
  `ffmpeg -ss 24.50 -t 77.10 -i gift_of_the_magi_henry_blb_64kb.mp3 -ac 1 -ar 22050 -c:a libmp3lame -b:a 24k excerpt.mp3`
  (plus ID3 title/artist/comment tags). Re-encoding with a different ffmpeg/LAME build will not be
  byte-identical, so the checked-in file is the reference and its hash is in `sentence_map.json`.

## Sentence map

`sentence_map.json` covers sentences 0–14 (the story's first paragraph and a half, text offsets
0–1091 of spine item 2). Times are on the **excerpt's** timeline; add 24.50 s for the full
recording. Each boundary was found with `ffmpeg silencedetect` (−32 dB, 0.3 s) and then confirmed by
listening to / transcribing the cut between the boundaries. Observations:

- Sentence 3 contains an internal pause at 13.84–14.23 s (after "butcher"); it is one sentence.
- Sentence 10: the reader says "mistress of the **house**"; the text says "home". The map follows
  the text, as an aligner would.
- Sentence 0's start (0.25 s) follows the spoken title; the preceding LibriVox announcement is
  not part of the excerpt.

The `text` fields are copies of the exact normalised EPUB text and are checked against the EPUB by
`tests/test_contracts.py`.
