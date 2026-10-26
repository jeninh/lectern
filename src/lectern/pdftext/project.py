"""EPUB ↔ PDF projection: which work sentence each PDF sentence is.

A work with an EPUB and a PDF of the same book has one canonical sentence list, the EPUB's
(alignment and generated audiobooks time it). The PDF's sentences are segmented from its own
text, so neither their number nor their boundaries need agree with the EPUB's: line breaks,
hyphenation, running heads, footnotes, and editions all differ. Both texts are tokenised with
the alignment tokeniser and matched with the alignment matcher's anchors
(:func:`lectern.align.matcher.match_tokens`: unique n-grams, longest increasing chain, edit
distance in the gaps). Each PDF sentence then gets the work sentence most of its matched tokens
fall in, or none when too few of its words were found (front matter only one edition has).

Going the other way, a work sentence's PDF sentences are those that point at it; a work
sentence no PDF sentence points at is shown at the nearest earlier one (:func:`pdf_for_work`).
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Sequence

from lectern.align.matcher import match_tokens
from lectern.align.tokens import tokenize

# A PDF sentence is that work sentence with this share of its tokens matched, or this many.
MIN_SHARE = 0.4
MIN_TOKENS = 4


def project(work_texts: Sequence[str], pdf_texts: Sequence[str]) -> list[int | None]:
    """``out[k]`` = the work sentence PDF sentence ``k`` is, or ``None``."""
    t_norm: list[str] = []
    t_sent: list[int] = []
    for n, text in enumerate(work_texts):
        for tok in tokenize(text):
            t_norm.append(tok.norm)
            t_sent.append(n)
    a_norm: list[str] = []
    a_sent: list[int] = []
    totals = Counter()
    for k, text in enumerate(pdf_texts):
        for tok in tokenize(text):
            a_norm.append(tok.norm)
            a_sent.append(k)
            totals[k] += 1
    votes: dict[int, Counter] = {}
    for i, j in match_tokens(t_norm, a_norm):
        votes.setdefault(a_sent[j], Counter())[t_sent[i]] += 1
    out: list[int | None] = []
    last = -1
    for k in range(len(pdf_texts)):
        found = votes.get(k)
        if not found:
            out.append(None)
            continue
        n, count = max(found.items(), key=lambda kv: (kv[1], -kv[0]))
        if (count >= MIN_TOKENS or count >= MIN_SHARE * totals[k]) and n >= last:
            out.append(n)
            last = n
        else:
            out.append(None)
    return out


def pdf_for_work(epub_ns: Sequence[int | None]) -> dict[int, list[int]]:
    """``{work sentence: [PDF sentences]}`` from :func:`project`'s output."""
    out: dict[int, list[int]] = {}
    for k, n in enumerate(epub_ns):
        if n is not None:
            out.setdefault(n, []).append(k)
    return out


def nearest_pdf_sentence(mapping: dict[int, list[int]], work_n: int) -> int | None:
    """The first PDF sentence of ``work_n``, else of the nearest earlier work sentence that
    has one (text one edition lacks is shown where the other edition was)."""
    if work_n in mapping:
        return mapping[work_n][0]
    keys = sorted(mapping)
    i = bisect_right(keys, work_n) - 1
    return mapping[keys[i]][-1] if i >= 0 else None
