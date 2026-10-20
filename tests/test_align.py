"""Alignment pipeline without a model: text map, segmentation, tokens, matcher, pipeline.

Expected values come from the hand-checked fixture (tests/fixtures/magi/sentence_map.json) or
from how the synthetic inputs were built, never from the code under test.
"""

from __future__ import annotations

import json
import random
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from lectern.align.matcher import AudioToken, TextToken, match
from lectern.align.pipeline import TrackInput, align_work
from lectern.align.text import read_spine_items, segment_items, segment_text
from lectern.align.tokens import normalize_word, tokenize
from lectern.align.transcribe import TrackAudio, Transcriber, Word, larger_model
from lectern.ingest.epub import read_spine
from lectern.ingest.hashing import sha256_file
from lectern.positions import TextPos, normalize_spine_text, text_to_xpointer, xpointer_to_text
from lectern.sentence_map import SentenceEntry, SentenceMap, TrackRef, audio_fingerprint
from lectern.textmap import SpineText

FIXTURE = Path(__file__).parent / "fixtures" / "magi"
EPUB = FIXTURE / "the-gift-of-the-magi.epub"
EXCERPT = FIXTURE / "excerpt.mp3"
HAND_MAP = json.loads((FIXTURE / "sentence_map.json").read_text())
TRANSCRIPT = json.loads((FIXTURE / "transcript_small.json").read_text())
STORY = 2  # spine index of the story


def recorded_words() -> list[Word]:
    return [Word(w[0], w[1], w[2], w[3]) for w in TRANSCRIPT["words"]]


# --- text extraction and the source map ------------------------------------------------------


def test_spine_text_is_the_frozen_normalisation_for_every_fixture_item():
    with zipfile.ZipFile(EPUB) as zf:
        for item in read_spine_items(EPUB):
            assert item.text.text == normalize_spine_text(zf.read(item.href)), item.href


def test_xpointer_round_trips_every_character_of_the_story():
    story = read_spine_items(EPUB)[STORY].text
    assert len(story.text) == 29283
    for c in range(len(story.text)):
        assert story.char_at(story.xpointer_tail(c)) == c


def test_xpointers_name_real_dom_nodes_in_crengine_form():
    with zipfile.ZipFile(EPUB) as zf:
        xhtml = zf.read(read_spine(EPUB)[STORY])
    story = normalize_spine_text(xhtml)
    # "Mr. James Dillingham Young" is in sentence 14 of the hand map
    char = story.index("Mr. James Dillingham Young")
    xp = text_to_xpointer(TextPos(STORY, char), xhtml)
    assert xp.startswith("/body/DocFragment[3]/body/") and "/text()" in xp
    assert xpointer_to_text(xp, xhtml) == TextPos(STORY, char)
    # crengine wrapper elements in an incoming pointer are transparent
    boxed = xp.replace("/body/DocFragment[3]/body/", "/body/DocFragment[3]/body/autoBoxing/", 1)
    assert xpointer_to_text(boxed, xhtml) == TextPos(STORY, char)


def test_xpointer_index_conventions():
    xhtml = (
        b"<html xmlns='http://www.w3.org/1999/xhtml'><body>"
        b"<div><p>Alpha <b>beta</b> gamma.</p>\n<p>Delta  \n epsilon.</p></div>"
        b"</body></html>"
    )
    text = SpineText(xhtml)
    assert text.text == "Alpha beta gamma. Delta epsilon."
    # single child elements carry no index; repeated siblings and split text nodes do
    assert text.xpointer_tail(0) == "/body/div/p[1]/text()[1].0"
    assert text.xpointer_tail(6) == "/body/div/p[1]/b/text().0"
    # the separating space belongs to the node before it (lectern.textmap), so the tail
    # text node " gamma." starts at offset 0
    assert text.xpointer_tail(11) == "/body/div/p[1]/text()[2].0"
    # offsets count collapsed whitespace, as crengine stores them
    assert text.xpointer_tail(text.text.index("epsilon")) == "/body/div/p[2]/text().6"
    # newer KOReader always writes indices; both forms resolve
    assert text.char_at("/body[1]/div[1]/p[2]/text()[1].6") == text.text.index("epsilon")
    assert text.char_at("/body/div/p[2].0") == text.text.index("Delta")


_WORDS = st.sampled_from(["a", "bb", "Ccc", "dé", "e.", "F!", "g?", "“h”", "1,000", "x"])
_SPACE = st.sampled_from(["", " ", "  ", "\n", "\t "])


@st.composite
def _xhtml_fragment(draw, depth: int = 0) -> str:
    parts = []
    for _ in range(draw(st.integers(0, 4))):
        kind = draw(st.sampled_from(["text", "text", "inline", "block"] if depth < 3 else ["text"]))
        if kind == "text":
            parts.append(draw(_SPACE) + " ".join(draw(st.lists(_WORDS, max_size=4))) + draw(_SPACE))
        else:
            tag = draw(
                st.sampled_from(
                    ["i", "b", "span"] if kind == "inline" else ["p", "div", "h2", "li"]
                )
            )
            parts.append(f"<{tag}>{draw(_xhtml_fragment(depth + 1))}</{tag}>")
        if draw(st.booleans()):
            parts.append(
                draw(st.sampled_from(["<br/>", "<script>var x=1;</script>", "<!-- c -->"]))
            )
    return "".join(parts)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_xhtml_fragment())
def test_property_source_map_matches_normalisation_and_round_trips(body):
    xhtml = f"<html xmlns='http://www.w3.org/1999/xhtml'><body>{body}</body></html>".encode()
    text = SpineText(xhtml)
    assert text.text == normalize_spine_text(xhtml)
    for c in range(len(text.text)):
        assert text.char_at(text.xpointer_tail(c)) == c
    # sentences never cross blocks, never overlap, never include edge whitespace
    spans = segment_text(text.text, text.block_starts())
    last = 0
    for a, b in spans:
        assert last <= a < b
        assert not text.text[a].isspace() and not text.text[b - 1].isspace()
        assert text.block_at(a) == text.block_at(b - 1)
        last = b


# --- sentence segmentation --------------------------------------------------------------------


def test_segmentation_reproduces_the_hand_checked_sentences():
    sentences = [s for s in segment_items(read_spine_items(EPUB)) if s.spine_index == STORY]
    for hand, got in zip(HAND_MAP["sentences"], sentences, strict=False):
        assert (got.char_start, got.char_end) == (hand["char_start"], hand["char_end"]), hand[
            "text"
        ]
    # numbering is global over the spine and deterministic
    again = segment_items(read_spine_items(EPUB))
    assert [s.n for s in again] == list(range(len(again)))
    assert again == segment_items(read_spine_items(EPUB))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Mr. Young said hi. Then he left.", ["Mr. Young said hi.", "Then he left."]),
        ("O. Henry wrote it. Yes.", ["O. Henry wrote it.", "Yes."]),
        ("“Stop!” she cried. He did.", ["“Stop!” she cried.", "He did."]),
        ("“Why?” Della asked.", ["“Why?”", "Della asked."]),
        ("It cost $1.87 in all. Fine.", ["It cost $1.87 in all.", "Fine."]),
        ("Wait… what? No.", ["Wait… what?", "No."]),  # lowercase next: same sentence
        ("Wait… What? No.", ["Wait…", "What?", "No."]),
    ],
)
def test_segmentation_rules(text, expected):
    assert [text[a:b] for a, b in segment_text(text)] == expected


def test_long_sentences_split_at_semicolons_but_short_ones_do_not():
    clause = "word " * 60
    long = f"{clause.strip()}; {clause.strip()}; {clause.strip()}."
    spans = segment_text(long)
    assert len(spans) >= 2 and all(b - a <= 500 for a, b in spans)
    assert segment_text("One; two; three.") == [(0, 16)]


# --- tokens -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Eighty-seven", ["eighty", "seven"]),
        ("$1.87", ["one", "dollar", "and", "eighty", "seven", "cents"]),
        ("$8", ["eight", "dollars"]),
        ("$0.60", ["sixty", "cents"]),
        ("1,000", ["one", "thousand"]),
        ("21st", ["twenty", "first"]),
        ("12th", ["twelfth"]),
        ("3.5", ["three", "point", "five"]),
        ("1905", ["nineteen", "oh", "five"]),
        ("1987", ["nineteen", "eighty", "seven"]),
        ("2000", ["two", "thousand"]),
        ("2007", ["two", "thousand", "seven"]),
        ("1900", ["nineteen", "hundred"]),
        ("101", ["one", "hundred", "one"]),
        ("Don’t", ["don't"]),
        ("CAFÉ", ["café"]),
        ("“Della,”", ["della"]),
    ],
)
def test_token_normalisation(raw, expected):
    assert [t.norm for t in tokenize(raw)] == expected


def test_tokens_keep_source_offsets():
    toks = tokenize("Hi, Jim-Bob!", base=100)
    assert [(t.norm, t.start, t.end) for t in toks] == [
        ("hi", 100, 102),
        ("jim", 104, 107),
        ("bob", 108, 111),
    ]
    assert normalize_word("'") == []


# --- matcher: synthetic books -----------------------------------------------------------------

_VOCAB = [f"w{i}" for i in range(400)]


def _book(sentences: int = 120, words: int = 10, seed: int = 7) -> list[list[str]]:
    rng = random.Random(seed)
    return [[rng.choice(_VOCAB) for _ in range(words)] for _ in range(sentences)]


def _streams(book, spoken_order=None, *, word_s=0.4, pause_s=0.3, intro=0, garble=()):
    """Text tokens, and audio tokens of the sentences in ``spoken_order`` read aloud."""
    text = [TextToken(w, n) for n, s in enumerate(book) for w in s]
    audio, t = [], 0.0
    for k in range(intro):
        audio.append(AudioToken(f"intro{k}", t, t + word_s))
        t += word_s
    t += pause_s
    spoken = {}
    for n in spoken_order if spoken_order is not None else range(len(book)):
        spoken[n] = t
        for w in book[n]:
            norm = f"zz{random.Random(n).random()}" if n in garble else w
            audio.append(AudioToken(norm, t, t + word_s))
            t += word_s
        t += pause_s
    return text, audio, [len(" ".join(s)) for s in book], t, spoken


def _check_times(result, spoken, book, word_s=0.4, tol=0.25):
    for n, start in spoken.items():
        tm = result.timings[n]
        assert tm.matched, n
        assert tm.start == pytest.approx(start, abs=tol)
        assert tm.end == pytest.approx(start + len(book[n]) * word_s, abs=tol)


def test_complete_reading_matches_every_sentence_exactly():
    book = _book()
    text, audio, chars, total, spoken = _streams(book)
    r = match(text, audio, chars, total)
    assert r.coverage == 1.0
    _check_times(r, spoken, book)
    assert r.stats["diagnosis"] == "ok" and not r.stats["likely_abridged"]


def test_narrator_intro_and_outro_are_ignored():
    book = _book()
    text, audio, chars, total, spoken = _streams(book, intro=40)
    audio += [AudioToken(f"outro{k}", total + k, total + k + 0.4) for k in range(30)]
    r = match(text, audio, chars, total + 30)
    assert r.coverage == 1.0
    _check_times(r, spoken, book)


def test_front_matter_is_not_matched_to_a_spoken_preamble_of_common_words():
    """A licence header and a narrator's preamble share words like "the" and "for", never a
    phrase; the edges must not be force-aligned word by word."""
    common = (
        "this is the for of and in to a recording public domain ebook use by at it you may "
        "copy give away under terms license online www org more information visit please "
        "read volunteer chapter title author release date language credits version html"
    ).split()
    rng = random.Random(11)
    weights = [12, 9, 20, 6, 14, 12, 8, 9, 10] + [1] * (len(common) - 9)  # Zipf-like

    def words(k):
        return rng.choices(common, weights, k=k)

    front = [words(25) for _ in range(6)]
    book = front + _book(sentences=60)
    order = range(6, len(book))  # the narrator reads only the story
    text, audio, chars, total, spoken = _streams(book, order)
    preamble = [AudioToken(w, k * 0.4, k * 0.4 + 0.4) for k, w in enumerate(words(60))]
    shift = 60 * 0.4
    audio = preamble + [AudioToken(x.norm, x.start + shift, x.end + shift) for x in audio]
    r = match(text, audio, chars, total + shift)
    assert not any(r.timings[n].matched for n in range(6))
    _check_times(r, {n: t + shift for n, t in spoken.items()}, book)


def test_missing_chapter_is_unmatched_and_not_interpolated():
    book = _book()
    order = [n for n in range(len(book)) if not 40 <= n < 70]
    text, audio, chars, total, spoken = _streams(book, order)
    r = match(text, audio, chars, total)
    _check_times(r, spoken, book)
    for n in range(40, 70):
        assert not r.timings[n].matched
        assert r.timings[n].start is None, "a cut must not get a guessed time"
    assert r.stats["likely_abridged"] and r.stats["diagnosis"] == "abridged"
    assert r.stats["longest_interior_unmatched_run"] == 30


def test_misheard_sentences_are_interpolated_and_flagged():
    book = _book()
    text, audio, chars, total, spoken = _streams(book, garble={50, 51})
    r = match(text, audio, chars, total)
    for n in (50, 51):
        tm = r.timings[n]
        assert not tm.matched
        # the guess lies between the neighbours, in order
        assert r.timings[49].end <= tm.start <= tm.end <= r.timings[52].start
    assert r.timings[50].end == pytest.approx(r.timings[51].start)
    assert not r.stats["likely_abridged"]


def test_poor_transcription_is_not_mistaken_for_abridgment():
    book = _book()
    rng = random.Random(3)
    bad = set(rng.sample(range(10, 110), 45))
    text, audio, chars, total, _ = _streams(book, garble=bad)
    r = match(text, audio, chars, total)
    assert r.coverage < 0.85 and r.stats["span_coverage"] < 0.85
    assert not r.stats["likely_abridged"]
    assert r.stats["diagnosis"] == "poor_transcription"
    assert r.stats["span_audio_vs_expected"] == pytest.approx(1.0, abs=0.2)


def test_reordered_chapters_keep_the_longest_in_order_chain():
    book = _book()
    order = list(range(0, 30)) + list(range(60, 120)) + list(range(30, 60))
    text, audio, chars, total, spoken = _streams(book, order)
    r = match(text, audio, chars, total)
    _check_times(r, {n: spoken[n] for n in [*range(30), *range(60, 120)]}, book)
    assert all(not r.timings[n].matched for n in range(30, 60))
    assert r.stats["reordered_text_tokens"] >= 250
    starts = [tm.start for tm in r.timings if tm.matched]
    assert starts == sorted(starts)


def test_excerpt_is_partial_audio_not_a_bad_model():
    book = _book()
    text, audio, chars, total, spoken = _streams(book, range(10, 25))
    r = match(text, audio, chars, total)
    _check_times(r, spoken, book)
    assert r.coverage == pytest.approx(15 / 120)
    assert r.stats["span_coverage"] == 1.0
    assert r.stats["diagnosis"] == "partial_audio" and not r.stats["likely_abridged"]
    assert larger_model("small") == "medium" and larger_model("large-v3") is None


def test_no_overlap_yields_nothing_matched():
    book = _book()
    text, _, chars, _, _ = _streams(book)
    audio = [AudioToken(f"other{k}", k * 0.4, k * 0.4 + 0.4) for k in range(500)]
    r = match(text, audio, chars, 200.0)
    assert r.coverage == 0.0 and r.stats["diagnosis"] == "no_match"


_EDIT = st.tuples(st.sampled_from(["drop", "insert", "replace"]), st.integers(0, 10_000))


@settings(max_examples=60, deadline=None)
@given(st.lists(_EDIT, max_size=80), st.integers(0, 2**16))
def test_property_matcher_output_is_consistent_under_transcript_noise(edits, seed):
    book = _book(sentences=60, seed=seed)
    text, audio, chars, total, _ = _streams(book)
    audio = list(audio)
    for op, pos in edits:
        if not audio:
            break
        i = pos % len(audio)
        if op == "drop":
            del audio[i]
        elif op == "insert":
            audio.insert(i, AudioToken("noise", audio[i].start, audio[i].start))
        else:
            audio[i] = AudioToken("noise", audio[i].start, audio[i].end)
    r = match(text, audio, chars, total)
    # pairs are a strictly increasing matching of equal (or near-equal) tokens
    assert all(a[0] < b[0] and a[1] < b[1] for a, b in zip(r.pairs, r.pairs[1:], strict=False))
    # matched starts never go backwards; every time lies on the audio timeline
    starts = [tm.start for tm in r.timings if tm.matched]
    assert starts == sorted(starts)
    for tm in r.timings:
        assert (tm.start is None) == (tm.end is None)
        if tm.start is not None:
            assert 0 <= tm.start <= tm.end <= total + 1e-9
        assert not tm.matched or tm.start is not None
    if not edits:
        assert r.coverage == 1.0
    # whatever the matcher produced is a valid sentence map
    SentenceMap(
        version=1,
        work_id=1,
        epub_sha256="0" * 64,
        audio_fingerprint=audio_fingerprint(["1" * 64]),
        model="test",
        generated_at="2026-10-20T00:00:00Z",
        tracks=[TrackRef(position=0, sha256="1" * 64, duration_s=total, offset_s=0)],
        sentences=[
            SentenceEntry(
                n=n,
                spine_index=0,
                char_start=n * 1000,
                char_end=n * 1000 + chars[n],
                audio_start_s=tm.start,
                audio_end_s=tm.end,
                matched=tm.matched,
            )
            for n, tm in enumerate(r.timings)
        ],
    )


# --- matcher and pipeline on the real fixture -------------------------------------------------


def _fixture_tracks() -> list[TrackInput]:
    return [TrackInput(0, EXCERPT, sha256_file(EXCERPT), 77.166, 0.0)]


def test_recorded_transcript_aligns_close_to_the_hand_checked_map():
    out = align_work(
        work_id=1,
        epub_path=EPUB,
        epub_sha256=sha256_file(EPUB),
        tracks=_fixture_tracks(),
        model_label="recorded-small",
        transcribe=lambda _t, _l, _p: (recorded_words(), "en"),
    )
    m = out.sentence_map
    SentenceMap.model_validate_json(m.model_dump_json())
    assert m.epub_sha256 == HAND_MAP["epub_sha256"]
    assert m.audio_fingerprint == HAND_MAP["audio_fingerprint"]
    story = [s for s in m.sentences if s.spine_index == STORY]
    for hand, got in zip(HAND_MAP["sentences"], story, strict=False):
        assert got.matched, hand["text"]
        assert got.audio_start_s == pytest.approx(hand["audio_start_s"], abs=1.0)
        assert got.audio_end_s == pytest.approx(hand["audio_end_s"], abs=0.7)
    errors = [
        abs(got.audio_start_s - hand["audio_start_s"])
        for hand, got in zip(HAND_MAP["sentences"], story, strict=False)
    ]
    assert sorted(errors)[len(errors) // 2] < 0.3  # median start error
    # only the excerpt's 15 sentences are narrated: whole-book coverage is low by design
    assert sum(s.matched for s in m.sentences) == 15
    assert out.stats["diagnosis"] == "partial_audio" and out.stats["span_coverage"] == 1.0
    assert out.stats["speed"] > 0 and out.stats["words"] == 207


def test_pipeline_places_tracks_on_one_timeline():
    """Two tracks: the second one's words arrive offset by the first one's duration."""
    words = recorded_words()
    seen = []

    def transcribe(tracks, _lang, progress):
        seen.extend(tracks)
        progress(1.0)
        return words, "en"

    sha = sha256_file(EXCERPT)
    out = align_work(
        work_id=1,
        epub_path=EPUB,
        epub_sha256=sha256_file(EPUB),
        tracks=[TrackInput(0, EXCERPT, sha, 40.0, 0.0), TrackInput(1, EXCERPT, sha, 37.166, 40.0)],
        model_label="recorded",
        transcribe=transcribe,
    )
    assert [t.offset_s for t in seen] == [0.0, 40.0]
    assert [t.offset_s for t in out.sentence_map.tracks] == [0.0, 40.0]
    assert out.sentence_map.audio_fingerprint == audio_fingerprint([sha, sha])


SR = 16_000
BEEP_S, BEEP_EVERY_S, FIRST_BEEP_S = 0.4, 1.7, 0.3


def _beep_times(duration: float) -> list[float]:
    out, t = [], FIRST_BEEP_S
    while t + BEEP_S <= duration:
        out.append(round(t, 3))
        t += BEEP_EVERY_S
    return out


def _beep_pcm(duration: float, block: int = 7_777):
    """int16 PCM with a 440 Hz burst of BEEP_S at every _beep_times() instant, in blocks of an
    odd size so window edges never coincide with block edges."""
    import numpy as np

    n = int(duration * SR)
    pcm = np.zeros(n, dtype=np.int16)
    tone = (0.5 * 32767 * np.sin(2 * np.pi * 440 * np.arange(int(BEEP_S * SR)) / SR)).astype(
        np.int16
    )
    for t in _beep_times(duration):
        i = int(round(t * SR))
        pcm[i : i + len(tone)] = tone
    for i in range(0, n, block):
        yield pcm[i : i + block].copy()


class _BeepModel:
    """Stands in for ``faster_whisper.WhisperModel``: hears each tone burst in the array it is
    given as the word "beep", with times relative to the array, like Whisper. A burst cut by
    the array's edge is heard cut, and every call is off by a few tens of milliseconds in its
    own direction, as two real transcriptions of the same audio are."""

    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **kw):
        import numpy as np

        assert isinstance(audio, np.ndarray) and audio.dtype == np.float32
        self.calls.append({"samples": len(audio), **kw})
        jitter = 0.04 if len(self.calls) % 2 else -0.03
        hop = SR // 100
        loud = np.abs(audio[: len(audio) // hop * hop]).reshape(-1, hop).max(axis=1) > 0.1
        words, i = [], 0
        while i < len(loud):
            if loud[i]:
                j = i
                while j < len(loud) and loud[j]:
                    j += 1
                start = max(0.0, i / 100 + jitter)
                words.append(
                    SimpleNamespace(
                        word=" beep", start=start, end=j / 100 + jitter, probability=0.9
                    )
                )
                i = j
            else:
                i += 1
        segs = [SimpleNamespace(end=len(audio) / SR, words=words)]
        return iter(segs), SimpleNamespace(language="en")


def _beep_transcriber(window_s: float, overlap_s: float, durations: dict) -> Transcriber:
    tr = Transcriber("small", window_s=window_s, overlap_s=overlap_s)
    tr._model = _BeepModel()
    tr.decode = lambda path: _beep_pcm(durations[path])
    return tr


def test_windows_join_into_one_copy_of_every_word_on_the_work_timeline():
    """Many seams (30 s windows, 9 s overlap, bursts every 1.7 s so they fall everywhere
    relative to the seams), two tracks on one timeline: every burst exactly once, at its
    absolute time, and no window larger than configured."""
    from lectern.align.transcribe import transcribe_tracks

    durations = {Path("a"): 200.0, Path("b"): 95.5}
    tr = _beep_transcriber(30.0, 9.0, durations)
    progress = []
    words, lang = transcribe_tracks(
        tr,
        [TrackAudio(Path("a"), 0.0, 200.0), TrackAudio(Path("b"), 200.0, 95.5)],
        None,
        progress.append,
    )
    expected = _beep_times(200.0) + [200.0 + t for t in _beep_times(95.5)]
    assert len(words) == len(expected)
    assert [w.start for w in words] == pytest.approx(expected, abs=0.06)
    assert all(w.text == " beep" for w in words)
    assert lang == "en"
    calls = tr._model.calls
    assert max(c["samples"] for c in calls) == 30 * SR
    # windows advance by 21 s: track a needs 10 (the last ends at 200 s), track b 5
    assert len(calls) == 10 + 5
    assert all(c["word_timestamps"] and c["vad_filter"] for c in calls)
    # detected once from the first window that heard speech, then fixed
    assert calls[0]["language"] is None and all(c["language"] == "en" for c in calls[1:])
    assert progress == sorted(progress) and progress[-1] == pytest.approx(1.0)
    assert tr.label == "faster-whisper-small-int8"


def test_one_window_when_the_track_is_shorter_than_a_window():
    durations = {Path("a"): 50.0}
    tr = _beep_transcriber(900.0, 30.0, durations)
    words, _ = tr.transcribe(TrackAudio(Path("a"), 12.5, 50.0))
    assert len(tr._model.calls) == 1
    assert [w.start for w in words] == pytest.approx(
        [12.5 + t for t in _beep_times(50.0)], abs=0.06
    )


def test_seam_drops_the_later_copy_of_a_word_heard_by_both_windows():
    from lectern.align.transcribe import merge_window_words, seam

    # overlap [100, 112); central half [103, 109). The earlier window heard a long pause
    # 105.0-108.0, so the seam is at 106.5.
    earlier = [Word(" at", 103.2, 103.5), Word(" the", 104.3, 105.0), Word(" door", 108.0, 108.5)]
    assert seam(earlier, 100.0, 112.0) == pytest.approx(106.5)
    later = [
        Word(" at", 103.25, 103.6),  # before the seam: the earlier window's copy is kept
        Word(" the", 104.35, 105.05),
        Word(" Door.", 108.1, 108.6),  # after it: kept from the later window
        Word(" She", 110.2, 110.5),
    ]
    kept, rest = merge_window_words([Word(" Knock", 99.0, 99.4)], earlier, later, 100.0, 112.0)
    assert [w.text for w in kept] == [" Knock", " at", " the"]
    assert [w.text for w in rest] == [" Door.", " She"]
    # A word both windows heard, timed on opposite sides of the seam, is kept once; a
    # different word just as close is not mistaken for it. Pauses in the central half
    # [103, 109): 0.1 s, 1.1 s (104.9-106.0), 0.1 s, so the seam is at 105.45.
    earlier = [Word(" so", 103.0, 104.4), Word(" again", 104.6, 104.9), Word(" on", 106.0, 108.9)]
    assert seam(earlier, 100.0, 112.0) == pytest.approx(105.45)
    for later_text, expected in [(" Again,", [" so", " again", " on"]),
                                 (" gain", [" so", " again", " gain", " on"])]:  # fmt: skip
        later = [Word(later_text, 105.5, 105.9), Word(" on", 106.05, 108.9)]
        kept, rest = merge_window_words([], earlier, later, 100.0, 112.0)
        assert [w.text for w in kept + rest] == expected


def test_silent_overlap_cuts_at_its_midpoint():
    from lectern.align.transcribe import seam

    assert seam([], 100.0, 130.0) == 115.0
    assert seam([Word(" far", 90.0, 91.0)], 100.0, 130.0) == 115.0


@pytest.mark.parametrize(
    ("total", "window", "overlap", "starts", "last_len"),
    [
        (100, 30, 10, [0, 20, 40, 60, 80], 20),  # the tail 80-100 holds 10 s of new audio
        (90, 30, 10, [0, 20, 40, 60], 30),  # exact: the last full window ends at 90
        (30, 30, 10, [0], 30),  # one full window; the leftover overlap is not sent again
        (12, 30, 10, [0], 12),
    ],
)
def test_window_boundaries(total, window, overlap, starts, last_len):
    import numpy as np

    from lectern.align.transcribe import iter_windows

    blocks = (np.full(3, i, dtype=np.int16) for i in range(total))  # 3 samples per "second"
    got = list(iter_windows(blocks, window, overlap, sampling_rate=3))
    assert [s for s, _ in got] == starts
    assert len(got[-1][1]) == last_len * 3
    # every window is the audio at its own place on the timeline
    for start, samples in got:
        assert samples[0] * 32768.0 == pytest.approx(start)


@pytest.mark.parametrize(("window", "overlap"), [(5, 1), (60, 21), (60, -1)])
def test_window_settings_are_checked(window, overlap):
    with pytest.raises(ValueError):
        Transcriber("small", window_s=window, overlap_s=overlap)


def test_long_track_is_decoded_with_memory_bounded_by_the_window(tmp_path):
    """Regression for out-of-memory on single-file audiobooks: 40 minutes of audio go through
    the real PyAV decoder and the windowing with Python-side allocations (NumPy included)
    bounded by a few windows, where decoding it whole would need ~150 MB of float32 alone."""
    pytest.importorskip("av")
    import tracemalloc
    import wave

    from lectern.align.transcribe import iter_pcm

    minutes, rate = 40, 8_000
    path = tmp_path / "long.wav"
    second = bytes(128 + (i % 50) for i in range(rate))  # 8-bit unsigned, a quiet buzz
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(rate)
        for _ in range(minutes * 60):
            w.writeframes(second)
    durations = {path: minutes * 60.0}
    tr = Transcriber("small", window_s=60.0, overlap_s=5.0)
    tr._model = _BeepModel()
    tr.decode = iter_pcm
    tracemalloc.start()
    try:
        tr.transcribe(TrackAudio(path, 0.0, durations[path]))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    whole_file_float32 = minutes * 60 * SR * 4
    assert peak < 25 * 2**20, f"peak {peak / 2**20:.1f} MiB"
    assert peak < whole_file_float32 / 6
    # every sample was decoded and placed: windows advance 55 s and the last ends at 40 min
    calls = tr._model.calls
    assert sum(c["samples"] for c in calls) - 5 * SR * (len(calls) - 1) == pytest.approx(
        minutes * 60 * SR, abs=SR // 10
    )


# --- messy real-world EPUBs ------------------------------------------------------------------

_GOOD1 = "Alpha bravo charlie delta echo foxtrot. Golf hotel india juliet kilo lima."
_GOOD2 = "Mike november oscar papa quebec romeo. Sierra tango uniform victor whiskey xray."


def _messy_epub(path: Path, big: int) -> Path:
    """Spine: good, empty, missing entry, dangling idref, oversized, binary, unparseable, good."""

    def page(text):
        return f"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>{text}</p></body></html>"

    items = [("good1", "a.xhtml"), ("empty", "b.xhtml"), ("missing", "gone.xhtml"),
             ("big", "d.xhtml"), ("binary", "e.xhtml"), ("broken", "f.xhtml"), ("good2", "g.xhtml")]  # fmt: skip
    manifest = "".join(
        f'<item id="{i}" href="{h}" media-type="application/xhtml+xml"/>' for i, h in items
    )
    refs = [i for i, _ in items]
    refs.insert(3, "dangling")
    spine = "".join(f'<itemref idref="{i}"/>' for i in refs)
    opf = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="u"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="u">x</dc:identifier><dc:title>Messy</dc:title></metadata>'
        f"<manifest>{manifest}</manifest><spine>{spine}</spine></package>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
            '<rootfiles><rootfile full-path="content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        zf.writestr("content.opf", opf)
        zf.writestr("a.xhtml", page(_GOOD1))
        zf.writestr("b.xhtml", b"")
        zf.writestr("d.xhtml", page("big " * big))
        zf.writestr("e.xhtml", b"\x00\xff\xfe\x00 not xml at all")
        zf.writestr("f.xhtml", b"<<<>>>&&&")
        zf.writestr("g.xhtml", page(_GOOD2))
    return path


def test_bad_spine_items_contribute_no_text_and_keep_their_index(tmp_path, monkeypatch):
    import lectern.align.text as text_mod
    from lectern.align.text import iter_spine_items, spine_lengths
    from lectern.ingest.epub import EpubError

    monkeypatch.setattr(text_mod, "MAX_ITEM_BYTES", 2000)
    epub = _messy_epub(tmp_path / "messy.epub", big=1000)
    with pytest.raises(EpubError):
        read_spine(epub)  # the strict reader (M4's spine text route) is unchanged
    assert spine_lengths(epub) == [len(_GOOD1), 0, 0, 0, 0, 0, 0, len(_GOOD2)]
    assert [i.index for i in iter_spine_items(epub)] == list(range(8))

    words = [
        Word(w, k * 0.4, k * 0.4 + 0.35) for k, w in enumerate((_GOOD1 + " " + _GOOD2).split())
    ]
    out = align_work(
        work_id=1,
        epub_path=epub,
        epub_sha256=sha256_file(epub),
        tracks=[TrackInput(0, EXCERPT, sha256_file(EXCERPT), 20.0, 0.0)],
        model_label="recorded",
        transcribe=lambda _t, _l, _p: (words, "en"),
    )
    assert [(s.spine_index, s.matched) for s in out.sentence_map.sentences] == [
        (0, True), (0, True), (7, True), (7, True)
    ]  # fmt: skip


def test_total_spine_bytes_are_capped(tmp_path, monkeypatch):
    import lectern.align.text as text_mod
    from lectern.align.text import spine_lengths

    epub = _messy_epub(tmp_path / "messy.epub", big=10)
    first = len(zipfile.ZipFile(epub).read("a.xhtml"))
    monkeypatch.setattr(text_mod, "MAX_TOTAL_BYTES", first + 10)
    lengths = spine_lengths(epub)
    assert lengths[0] == len(_GOOD1) and lengths[3:] == [0] * 5  # later items read nothing


def test_a_word_said_twice_across_the_seam_is_kept_twice():
    """Regression: "no, no" with the seam between the two copies lost one of them, even when
    both windows heard both. A word heard once but timed on opposite sides of the seam is still
    kept once, and a word the earlier window missed is kept."""
    from lectern.align.transcribe import merge_window_words

    def filler(a: float, b: float, d: float = 0.0) -> list[Word]:
        """A 0.3 s word every 0.35 s from ``a``, the last ending by ``b`` (shifted by ``d``)."""
        n = round((b - a + 0.05) / 0.35)
        return [Word(" la", a + 0.35 * i + d, a + 0.35 * i + 0.3 + d) for i in range(n)]

    def count(earlier, later, text=" no"):
        kept, rest = merge_window_words([], earlier, later, 100.0, 115.0)
        starts = [w.start for w in kept + rest]
        assert starts == sorted(starts)
        return sum(w.text == text for w in kept + rest)

    # Overlap [100, 115); the longest pause in its central half is between the two "no"s.
    earlier = filler(100, 106.3) + [Word(" no", 106.3, 106.6), Word(" no", 107.0, 107.3)]
    earlier += filler(107.35, 115)
    later = filler(100, 106.3, 0.02) + [Word(" no", 106.32, 106.6), Word(" no", 107.02, 107.3)]
    later += filler(107.35, 120, 0.02)
    assert count(earlier, later) == 2
    # Both copies before the seam in the earlier window; the later one times the second
    # copy 0.8 s late, after the seam: still two, not three.
    earlier = filler(100, 105.5) + [Word(" no", 105.6, 105.9), Word(" no", 105.95, 106.1)]
    earlier += filler(107.2, 115)
    later = filler(100, 105.5, 0.01) + [Word(" no", 105.62, 105.9), Word(" no", 106.75, 106.9)]
    later += filler(107.2, 120, 0.01)
    assert count(earlier, later) == 2
    # One "door", timed before the seam by the earlier window and after it by the later one.
    earlier = filler(100, 105.5) + [Word(" door", 105.6, 105.9)] + filler(107.5, 115)
    later = filler(100, 105.5, 0.01) + [Word(" door", 106.6, 106.9)] + filler(107.5, 120, 0.01)
    assert count(earlier, later, " door") == 1
    # A word only the later window heard, just after the seam, is kept.
    later = filler(100, 105.5, 0.01) + [Word(" door", 105.62, 105.9), Word(" bell", 106.7, 107)]
    later += filler(107.5, 120, 0.01)
    assert count(earlier, later, " bell") == 1


@pytest.mark.parametrize("every", [0.75, 0.9, 1.2])
def test_dense_speech_keeps_every_word_across_many_seams(monkeypatch, every):
    """Bursts closer together than the duplicate window (1 s): the old merge dropped a burst
    at about every second seam (315 of 328 at 0.9 s)."""
    from lectern.align.transcribe import transcribe_tracks

    monkeypatch.setattr(f"{__name__}.BEEP_EVERY_S", every)
    durations = {Path("a"): 200.0, Path("b"): 95.5}
    tr = _beep_transcriber(30.0, 9.0, durations)
    words, _ = transcribe_tracks(
        tr, [TrackAudio(Path("a"), 0.0, 200.0), TrackAudio(Path("b"), 200.0, 95.5)]
    )
    expected = _beep_times(200.0) + [200.0 + t for t in _beep_times(95.5)]
    assert [w.start for w in words] == pytest.approx(expected, abs=0.06)


def test_words_without_finite_times_are_dropped_and_times_stay_ordered():
    import math

    class Model:
        def transcribe(self, audio, **kw):
            nan = math.nan
            words = [
                SimpleNamespace(word=" a", start=1.0, end=1.2, probability=0.9),
                SimpleNamespace(word=" b", start=nan, end=nan, probability=nan),
                SimpleNamespace(word=" c", start=0.5, end=math.inf, probability=nan),
                SimpleNamespace(word=" d", start=2.0, end=2.4, probability=0.8),
            ]
            return iter([SimpleNamespace(end=3.0, words=words)]), SimpleNamespace(language="en")

    tr = Transcriber("small", window_s=30.0, overlap_s=0.0)
    tr._model = Model()
    tr.decode = lambda path: _beep_pcm(3.0)
    words, _ = tr.transcribe(TrackAudio(Path("a"), 10.0, 3.0))
    assert [w.text for w in words] == [" a", " c", " d"]
    assert all(math.isfinite(v) for w in words for v in (w.start, w.end, w.probability))
    assert [w.start for w in words] == [11.0, 11.0, 12.0]
