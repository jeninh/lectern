"""Match a transcript's words to the book's sentences.

Inputs are two token streams produced by :mod:`lectern.align.tokens`: the text side with a
sentence number per token, the audio side with a time span per token (already on the work
timeline). The output is a time span per sentence, flagged ``matched`` when the words were
actually found, plus statistics.

Algorithm
    1. *Anchors*: n-grams (n = 5) that occur exactly once in both streams. Sorted by text
       position, the longest chain whose audio positions also increase (longest increasing
       subsequence) is kept; everything off the chain is noise or reordered content.
    2. *Gaps* between consecutive anchors are matched recursively with shorter anchors
       (n = 4, then 3, only inside the gap, so a phrase that is common in the whole book can
       still anchor a small gap), and finally by edit distance (``rapidfuzz`` Levenshtein
       opcodes) when the gap has an anchor on both sides and is small enough to afford it.
       Before the first anchor and after the last, only a word-by-word extension outward
       from the anchor is trusted (a narrator's preamble is not the book's front matter).
       Equal tokens and near-equal replacements count as matches. Large gaps that never anchor stay unmatched: that is
       where a chapter is missing from the audio, or the text skips something the narrator
       reads.
    3. *Sentences*: a sentence's span runs from its first matched word's start to its last
       matched word's end. Sentences with too few matched words are unmatched.
    4. *Interpolation*: a run of unmatched sentences between two matched ones gets the audio
       between them, split in proportion to character length, as a flagged guess, unless the
       text is far longer than the audio could hold (a cut), in which case it gets no time.

Reordered chapters cannot be represented (sentence audio must be monotone), so their
sentences are reported unmatched and counted in ``stats["reordered_text_tokens"]``.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from rapidfuzz.distance import Levenshtein

ANCHOR_N = 5
MIN_ANCHOR_N = 3
# Three-word anchors are only trusted in small gaps between two anchors; in a big gap, or
# before the first / after the last anchor, a phrase of common words can be unique by accident.
SMALL_GAP_TOKENS = 400
# Edit distance costs O(len_a * len_b); beyond this, a gap that never anchored stays unmatched.
LEVENSHTEIN_MAX_CELLS = 4_000_000
REPLACE_SIMILARITY = 0.75
EXTEND_MAX_MISSES = 1
# A sentence counts as matched with this share of its tokens found, or this many tokens.
MATCHED_MIN_FRACTION = 0.2
MATCHED_MIN_TOKENS = 4
# Whisper hangs the silence before a phrase on its first word, so a sentence starts no earlier
# than its first word's end minus what that word can plausibly take to say.
FIRST_WORD_BASE_S = 0.15
FIRST_WORD_PER_CHAR_S = 0.12
# Interpolation gives up when the text would need more than this multiple of the audio gap.
INTERPOLATION_MAX_STRETCH = 3.0
INTERPOLATION_SLACK_S = 5.0
LOW_COVERAGE = 0.85
# Abridged: the narrated span's audio is under this share of an unabridged reading's length
# while more than this share of the span's words is unmatched.
ABRIDGED_MAX_RATIO = 0.8
ABRIDGED_MIN_GAP_FRACTION = 0.15


@dataclass(frozen=True)
class TextToken:
    norm: str
    sentence: int


@dataclass(frozen=True)
class AudioToken:
    norm: str
    start: float
    end: float


@dataclass
class SentenceTiming:
    start: float | None = None
    end: float | None = None
    matched: bool = False
    matched_tokens: int = 0
    tokens: int = 0


@dataclass
class MatchResult:
    timings: list[SentenceTiming]
    pairs: list[tuple[int, int]]  # (text token index, audio token index), both increasing
    stats: dict = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        if not self.timings:
            return 0.0
        return sum(1 for t in self.timings if t.matched) / len(self.timings)


def match(
    text_tokens: Sequence[TextToken],
    audio_tokens: Sequence[AudioToken],
    sentence_chars: Sequence[int],
    total_audio_s: float,
) -> MatchResult:
    """Align the two token streams; ``sentence_chars[n]`` is the length of sentence ``n``."""
    t_norm = [t.norm for t in text_tokens]
    a_norm = [a.norm for a in audio_tokens]
    pairs = _match_region(t_norm, a_norm, 0, len(t_norm), 0, len(a_norm), ANCHOR_N, False, False)
    pairs = _monotone(pairs)

    timings = _sentence_timings(text_tokens, audio_tokens, pairs, len(sentence_chars))
    rate = _chars_per_second(timings, sentence_chars)
    _interpolate(timings, sentence_chars, rate)
    stats = _stats(t_norm, a_norm, pairs, timings, text_tokens, total_audio_s, rate)
    return MatchResult(timings, pairs, stats)


# --- anchors and gaps -------------------------------------------------------------------------


def _match_region(
    t: list[str],
    a: list[str],
    t0: int,
    t1: int,
    a0: int,
    a1: int,
    n: int,
    left: bool,
    right: bool,
) -> list[tuple[int, int]]:
    """Pairs inside one region. ``left``/``right``: the region is bounded by an anchor on that
    side. Only a region bounded on both sides gets the edit-distance fallback: before the first
    anchor and after the last one, the two streams need not correspond at all (a narrator's
    preamble against a licence header), and common words would line up by chance."""
    if t0 >= t1 or a0 >= a1:
        return []
    size = max(t1 - t0, a1 - a0)
    bounded = left and right
    min_n = MIN_ANCHOR_N if bounded and size <= SMALL_GAP_TOKENS else MIN_ANCHOR_N + 1
    while n >= min_n and n <= min(t1 - t0, a1 - a0):
        anchors = _anchor_chain(t, a, t0, t1, a0, a1, n)
        if anchors:
            out: list[tuple[int, int]] = []
            pt, pa = t0, a0
            bounded = left
            for i, j in anchors:
                out += _match_region(t, a, pt, i, pa, j, n - 1, bounded, True)
                out += [(i + k, j + k) for k in range(n)]
                pt, pa = i + n, j + n
                bounded = True
            out += _match_region(t, a, pt, t1, pa, a1, n - 1, True, right)
            return out
        n -= 1
    if left and right:
        if (t1 - t0) * (a1 - a0) <= LEVENSHTEIN_MAX_CELLS:
            return _edit_distance_pairs(t, a, t0, t1, a0, a1)
        return []
    if right:  # head: walk back from the first anchor while the streams agree
        return _extend(t, a, range(t1 - 1, t0 - 1, -1), range(a1 - 1, a0 - 1, -1))[::-1]
    if left:  # tail: walk on from the last anchor
        return _extend(t, a, range(t0, t1), range(a0, a1))
    return []


def _extend(t: list[str], a: list[str], ti: range, ai: range) -> list[tuple[int, int]]:
    """Diagonal walk outward from an anchor: equal or near-equal words pair up, and the first
    miss ends it (that is where the reading and the text stop corresponding; tolerating misses
    lets frequent words like "the" pair up by chance)."""
    out: list[tuple[int, int]] = []
    misses = 0
    for i, j in zip(ti, ai, strict=False):
        if t[i] == a[j] or Levenshtein.normalized_similarity(t[i], a[j]) >= REPLACE_SIMILARITY:
            out.append((i, j))
            misses = 0
        else:
            misses += 1
            if misses >= EXTEND_MAX_MISSES:
                break
    return out


def _anchor_chain(
    t: list[str], a: list[str], t0: int, t1: int, a0: int, a1: int, n: int
) -> list[tuple[int, int]]:
    """Unique n-grams of both regions, as the longest chain increasing in both streams."""
    t_grams = Counter(tuple(t[i : i + n]) for i in range(t0, t1 - n + 1))
    a_grams = Counter(tuple(a[j : j + n]) for j in range(a0, a1 - n + 1))
    a_pos = {}
    for j in range(a0, a1 - n + 1):
        g = tuple(a[j : j + n])
        if a_grams[g] == 1:
            a_pos[g] = j
    candidates: list[tuple[int, int]] = []
    for i in range(t0, t1 - n + 1):
        g = tuple(t[i : i + n])
        if t_grams[g] == 1 and g in a_pos:
            candidates.append((i, a_pos[g]))
    chain = _longest_increasing(candidates)
    # Anchors may overlap (shifted by one word); keep the chain non-overlapping.
    out: list[tuple[int, int]] = []
    last_t = last_a = -1
    for i, j in chain:
        if i >= last_t and j >= last_a:
            out.append((i, j))
            last_t, last_a = i + n, j + n
    return out


def _longest_increasing(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Longest subsequence of ``pairs`` (sorted by first) with strictly increasing second."""
    tails: list[int] = []  # tails[k] = smallest second value ending a chain of length k+1
    tail_idx: list[int] = []
    prev = [-1] * len(pairs)
    for idx, (_, j) in enumerate(pairs):
        k = bisect_left(tails, j)
        if k == len(tails):
            tails.append(j)
            tail_idx.append(idx)
        else:
            tails[k] = j
            tail_idx[k] = idx
        prev[idx] = tail_idx[k - 1] if k else -1
    out: list[tuple[int, int]] = []
    cur = tail_idx[-1] if tail_idx else -1
    while cur != -1:
        out.append(pairs[cur])
        cur = prev[cur]
    out.reverse()
    return out


def _edit_distance_pairs(
    t: list[str], a: list[str], t0: int, t1: int, a0: int, a1: int
) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for tag, i0, i1, j0, j1 in Levenshtein.opcodes(t[t0:t1], a[a0:a1]):
        if tag == "equal":
            out += [(t0 + i0 + k, a0 + j0 + k) for k in range(i1 - i0)]
        elif tag == "replace":
            for k in range(min(i1 - i0, j1 - j0)):
                x, y = t[t0 + i0 + k], a[a0 + j0 + k]
                if Levenshtein.normalized_similarity(x, y) >= REPLACE_SIMILARITY:
                    out.append((t0 + i0 + k, a0 + j0 + k))
    return out


def match_tokens(t: list[str], a: list[str]) -> list[tuple[int, int]]:
    """Steps 1–2 alone: increasing ``(i, j)`` pairs of equal or near-equal tokens of two
    streams. Used to project one text onto another edition of it (an EPUB onto a PDF,
    :mod:`lectern.pdftext.project`) with the same anchors that align text to speech."""
    pairs = _match_region(t, a, 0, len(t), 0, len(a), ANCHOR_N, False, False)
    return _monotone(pairs)


def _monotone(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    pairs.sort()
    out: list[tuple[int, int]] = []
    last_a = -1
    for i, j in pairs:
        if j > last_a and (not out or i > out[-1][0]):
            out.append((i, j))
            last_a = j
    return out


# --- sentences ---------------------------------------------------------------------------------


def _sentence_timings(
    text_tokens: Sequence[TextToken],
    audio_tokens: Sequence[AudioToken],
    pairs: list[tuple[int, int]],
    sentence_count: int,
) -> list[SentenceTiming]:
    timings = [SentenceTiming() for _ in range(sentence_count)]
    for tok in text_tokens:
        timings[tok.sentence].tokens += 1
    for i, j in pairs:
        tm = timings[text_tokens[i].sentence]
        word = audio_tokens[j]
        tm.matched_tokens += 1
        if tm.start is None:
            spoken = FIRST_WORD_BASE_S + FIRST_WORD_PER_CHAR_S * len(word.norm)
            tm.start = max(word.start, word.end - spoken)
        tm.end = word.end if tm.end is None else max(tm.end, word.end)
    last_start = 0.0
    for tm in timings:
        if tm.start is None:
            continue
        enough = tm.matched_tokens >= MATCHED_MIN_TOKENS or (
            tm.tokens and tm.matched_tokens / tm.tokens >= MATCHED_MIN_FRACTION
        )
        if not enough:
            tm.start = tm.end = None
            continue
        tm.matched = True
        tm.start = max(tm.start, last_start)
        tm.end = max(tm.end, tm.start)  # type: ignore[type-var]
        last_start = tm.start
    return timings


def _chars_per_second(timings: list[SentenceTiming], chars: Sequence[int]) -> float | None:
    total_chars = 0
    total_s = 0.0
    for tm, n in zip(timings, chars, strict=False):
        if tm.matched and tm.end is not None and tm.start is not None:
            total_chars += n
            total_s += tm.end - tm.start
    return total_chars / total_s if total_s > 0 and total_chars else None


def _interpolate(timings: list[SentenceTiming], chars: Sequence[int], rate: float | None) -> None:
    matched_idx = [i for i, tm in enumerate(timings) if tm.matched]
    for p, q in zip(matched_idx, matched_idx[1:], strict=False):
        if q - p < 2:
            continue
        run = range(p + 1, q)
        gap_start = timings[p].end
        gap_end = max(timings[q].start, gap_start)  # type: ignore[type-var]
        gap_s = gap_end - gap_start  # type: ignore[operator]
        run_chars = sum(chars[i] for i in run)
        if rate:
            expected_s = run_chars / rate
            if expected_s > INTERPOLATION_MAX_STRETCH * gap_s + INTERPOLATION_SLACK_S:
                continue  # the audio skips this text; a guess would be wrong by design
        done = 0
        for i in run:
            frac0 = done / run_chars if run_chars else 0.0
            done += chars[i]
            frac1 = done / run_chars if run_chars else 1.0
            timings[i].start = gap_start + gap_s * frac0  # type: ignore[operator]
            timings[i].end = gap_start + gap_s * frac1  # type: ignore[operator]


# --- statistics --------------------------------------------------------------------------------


def _stats(
    t: list[str],
    a: list[str],
    pairs: list[tuple[int, int]],
    timings: list[SentenceTiming],
    text_tokens: Sequence[TextToken],
    total_audio_s: float,
    rate: float | None,
) -> dict:
    """Coverage and diagnosis.

    ``coverage`` is over the whole book (the contract's number). The *narrated span* is the
    text from the first to the last matched sentence; text outside it (front matter, a
    licence, an excerpt's missing remainder) was simply not read and says nothing about the
    transcription. ``span_coverage`` therefore separates "the model misheard" (low span
    coverage: try a larger model) from "the audio does not cover the text" (high span
    coverage, low coverage).

    Abridgment: an unabridged reading of the span takes about ``span_tokens / rate``; when the
    span's audio is much shorter than that *and* a real share of the span is unmatched, the
    narration skips text. A poor transcription leaves gaps too, but its audio still has the
    expected length, so it is not flagged.
    """
    matched = [i for i, tm in enumerate(timings) if tm.matched]
    n = len(timings)
    coverage = len(matched) / n if n else 0.0
    first, last = (matched[0], matched[-1]) if matched else (0, -1)
    interior_unmatched = [i for i in range(first, last + 1) if not timings[i].matched]
    runs = _runs(interior_unmatched)
    tokens_per_sentence = Counter(tok.sentence for tok in text_tokens)
    interior_tokens = sum(tokens_per_sentence[i] for i in interior_unmatched)
    leading_tokens = sum(tokens_per_sentence[i] for i in range(0, first))
    trailing_tokens = sum(tokens_per_sentence[i] for i in range(last + 1, n))
    span_sentences = last - first + 1 if matched else 0
    span_tokens = sum(tokens_per_sentence[i] for i in range(first, last + 1))
    span_coverage = len(matched) / span_sentences if span_sentences else 0.0

    # Speaking rate over runs of consecutive matched sentences, pauses between them included.
    run_tokens = 0
    run_s = 0.0
    for run in _runs(matched):
        run_tokens += sum(tokens_per_sentence[i] for i in run)
        run_s += timings[run[-1]].end - timings[run[0]].start  # type: ignore[operator]
    tokens_per_s = run_tokens / run_s if run_s > 0 else None
    span_audio_s = (
        timings[last].end - timings[first].start  # type: ignore[operator]
        if matched
        else 0.0
    )
    expected_span_s = span_tokens / tokens_per_s if tokens_per_s else None
    span_ratio = span_audio_s / expected_span_s if expected_span_s else None
    interior_fraction = interior_tokens / span_tokens if span_tokens else 0.0
    likely_abridged = bool(
        span_ratio is not None
        and span_ratio < ABRIDGED_MAX_RATIO
        and interior_fraction > ABRIDGED_MIN_GAP_FRACTION
    )
    return {
        "text_tokens": len(t),
        "audio_tokens": len(a),
        "matched_tokens": len(pairs),
        "sentences": n,
        "matched_sentences": len(matched),
        "coverage": round(coverage, 4),
        "low_coverage": coverage < LOW_COVERAGE,
        "span_first_sentence": first if matched else None,
        "span_last_sentence": last if matched else None,
        "span_coverage": round(span_coverage, 4),
        "leading_unmatched_tokens": leading_tokens,
        "trailing_unmatched_tokens": trailing_tokens,
        "interior_unmatched_tokens": interior_tokens,
        "interior_unmatched_runs": len(runs),
        "longest_interior_unmatched_run": max((len(r) for r in runs), default=0),
        "reordered_text_tokens": _reordered_tokens(t, a, pairs),
        "chars_per_second": round(rate, 2) if rate else None,
        "tokens_per_second": round(tokens_per_s, 3) if tokens_per_s else None,
        "span_audio_s": round(span_audio_s, 1),
        "expected_span_audio_s": round(expected_span_s, 1) if expected_span_s else None,
        "span_audio_vs_expected": round(span_ratio, 3) if span_ratio else None,
        "audio_s": round(total_audio_s, 3),
        "likely_abridged": likely_abridged,
        "diagnosis": _diagnosis(coverage, span_coverage, likely_abridged, bool(matched)),
    }


def _diagnosis(coverage: float, span_coverage: float, abridged: bool, any_matched: bool) -> str:
    if not any_matched:
        return "no_match"  # wrong book, wrong language, or silent audio
    if abridged:
        return "abridged"
    if span_coverage < LOW_COVERAGE:
        return "poor_transcription"  # a larger model should help
    if coverage < LOW_COVERAGE:
        return "partial_audio"  # the audio covers only part of the text
    return "ok"


def _runs(indices: list[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for i in indices:
        if runs and runs[-1][-1] == i - 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    return runs


def _reordered_tokens(t: list[str], a: list[str], pairs: list[tuple[int, int]]) -> int:
    """Text tokens in unmatched stretches whose unique 5-grams occur elsewhere in the audio
    (content the narrator read in a different order than the book)."""
    if not pairs:
        return 0
    matched_t = {i for i, _ in pairs}
    a_grams = Counter(tuple(a[j : j + ANCHOR_N]) for j in range(len(a) - ANCHOR_N + 1))
    t_grams = Counter(tuple(t[i : i + ANCHOR_N]) for i in range(len(t) - ANCHOR_N + 1))
    count = 0
    i = 0
    while i < len(t) - ANCHOR_N + 1:
        if i in matched_t:
            i += 1
            continue
        g = tuple(t[i : i + ANCHOR_N])
        if t_grams[g] == 1 and a_grams.get(g) == 1:
            count += ANCHOR_N
            i += ANCHOR_N
        else:
            i += 1
    return count


def sentence_for_time(timings: list[SentenceTiming], t: float) -> int | None:
    """Index of the matched sentence spoken at ``t`` (used by tests and diagnostics)."""
    starts = [(tm.start, i) for i, tm in enumerate(timings) if tm.matched]
    if not starts:
        return None
    k = bisect_right([s for s, _ in starts], t) - 1
    return starts[max(k, 0)][1]
