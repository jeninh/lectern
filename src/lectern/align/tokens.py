"""Token normalisation shared by the text and the transcript side of the matcher.

Both streams go through :func:`tokenize`, so "Eighty-seven" in the book and "$1.87" or
"eighty seven" in the transcript become the same tokens ``eighty``, ``seven``. Rules:

- Unicode NFKC, casefold; curly apostrophes become ``'``; a token is a run of letters, digits
  and inner apostrophes (``don't`` stays one token, ``eighty-seven`` becomes two).
- Numbers become English words: ``1,000`` → ``one thousand``; ``21st`` → ``twenty first``;
  ``3.5`` → ``three point five``; ``$8`` → ``eight dollars``; ``$1.87`` → ``one dollar and
  eighty seven cents``; four-digit years (1100-1999, 2001-2099) are read as years
  (``nineteen eighty seven``). Whisper writes digits for spoken numbers; books usually spell
  them out, and small mismatches only cost a local anchor, never the alignment.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_TOKEN = re.compile(
    r"\$\d[\d,]*(?:\.\d+)?"  # currency
    r"|\d[\d,]*(?:\.\d+)?(?:st|nd|rd|th)?"  # numbers, ordinals
    r"|[^\W\d_]+(?:['’][^\W\d_]+)*",  # words with inner apostrophes
    re.UNICODE,
)

_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = "zero ten twenty thirty forty fifty sixty seventy eighty ninety".split()
_SCALES = ("", "thousand", "million", "billion", "trillion")
_ORDINAL_SPECIAL = {
    "one": "first",
    "two": "second",
    "three": "third",
    "five": "fifth",
    "eight": "eighth",
    "nine": "ninth",
    "twelve": "twelfth",
}


@dataclass(frozen=True)
class Token:
    norm: str
    start: int  # char offset of the source word in the text it was cut from
    end: int


def tokenize(text: str, base: int = 0) -> list[Token]:
    """Normalised tokens of ``text``; ``base`` shifts the recorded offsets."""
    normalized = unicodedata.normalize("NFKC", text)
    if len(normalized) != len(text):
        # NFKC can change the length (ligatures, full-width forms). Offsets must stay those
        # of the original text, so tokenize the original and normalise each word instead.
        normalized = text
    out: list[Token] = []
    for m in _TOKEN.finditer(normalized):
        raw = m.group(0)
        for norm in normalize_word(raw):
            out.append(Token(norm, base + m.start(), base + m.end()))
    return out


def normalize_word(raw: str) -> list[str]:
    """Normalised token(s) for one matched word; may be empty."""
    w = unicodedata.normalize("NFKC", raw).casefold().replace("\u2019", "'").replace("`", "'")
    w = w.strip("'")
    if not w:
        return []
    if w[0] == "$":
        return _currency(w[1:])
    if w[0].isdigit():
        return _number(w)
    return [w]


def _number(w: str) -> list[str]:
    ordinal = w[-2:] in ("st", "nd", "rd", "th")
    if ordinal:
        w = w[:-2]
    w = w.replace(",", "")
    if "." in w:
        whole, _, frac = w.partition(".")
        words = _integer(whole) + ["point"] + [_ONES[int(d)] for d in frac if d.isdigit()]
        return words
    words = _year(w) if _looks_like_year(w) else _integer(w)
    if ordinal and words:
        words[-1] = _ordinal(words[-1])
    return words


def _currency(w: str) -> list[str]:
    w = w.replace(",", "")
    whole, _, frac = w.partition(".")
    if not whole:
        whole = "0"
    dollars = int(whole)
    cents = int((frac + "00")[:2]) if frac else 0
    words: list[str] = []
    if dollars or not cents:
        words += _integer(str(dollars)) + ["dollar" if dollars == 1 else "dollars"]
    if cents:
        if words:
            words.append("and")
        words += _integer(str(cents)) + ["cent" if cents == 1 else "cents"]
    return words


def _looks_like_year(w: str) -> bool:
    if len(w) != 4 or not w.isdigit():
        return False
    n = int(w)
    return 1100 <= n <= 1999 or 2001 <= n <= 2099


def _year(w: str) -> list[str]:
    hi, lo = int(w[:2]), int(w[2:])
    if lo == 0:
        return _integer(str(hi)) + ["hundred"]
    if lo < 10 and hi >= 20:
        return _integer(str(hi * 100 + lo))  # "two thousand five"
    if lo < 10:
        return _integer(str(hi)) + ["oh", _ONES[lo]]  # "nineteen oh five"
    return _integer(str(hi)) + _integer(str(lo))


def _integer(w: str) -> list[str]:
    digits = w.lstrip("0") or "0"
    if len(digits) > 15:
        return [_ONES[int(d)] for d in digits]  # read digit by digit
    n = int(digits)
    if n == 0:
        return ["zero"]
    words: list[str] = []
    groups: list[int] = []
    while n:
        groups.append(n % 1000)
        n //= 1000
    for scale_i in range(len(groups) - 1, -1, -1):
        g = groups[scale_i]
        if not g:
            continue
        words += _below_thousand(g)
        if _SCALES[scale_i]:
            words.append(_SCALES[scale_i])
    return words


def _below_thousand(n: int) -> list[str]:
    words: list[str] = []
    if n >= 100:
        words += [_ONES[n // 100], "hundred"]
        n %= 100
    if n >= 20:
        words.append(_TENS[n // 10])
        n %= 10
        if n:
            words.append(_ONES[n])
    elif n:
        words.append(_ONES[n])
    return words


def _ordinal(word: str) -> str:
    if word in _ORDINAL_SPECIAL:
        return _ORDINAL_SPECIAL[word]
    if word.endswith("y"):
        return word[:-1] + "ieth"
    return word + "th"
