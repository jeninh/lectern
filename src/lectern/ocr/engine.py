"""The OCR engine: Tesseract, found on the PATH, and its languages.

Tesseract names languages by its own codes (``eng``, ``deu``, ``chi_sim``…), one trained
data file each; several are joined with ``+`` (``eng+fra``). A work's language metadata is
whatever its EPUB, PDF, or editor said (``en``, ``en-GB``, ``fra``, ``zh-Hant``), so
:func:`language_for` maps it to Tesseract's code.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time

ENGINE = "tesseract"
DEFAULT_LANGUAGE = "eng"
# At most this many languages in one run (Tesseract slows down with each).
MAX_LANGUAGES = 3
LANG_CODE = re.compile(r"^[a-z]{3}(?:_[a-z]{3,4})?$")
_CACHE_S = 300.0
_lock = threading.Lock()
_cache: dict[str, tuple[float, str | None, tuple[str, ...]]] = {}

# ISO 639-1 (and the BCP 47 forms metadata uses) to Tesseract's codes.
_ISO1 = {
    "af": "afr", "am": "amh", "ar": "ara", "as": "asm", "az": "aze", "be": "bel",
    "bg": "bul", "bn": "ben", "bo": "bod", "bs": "bos", "br": "bre", "ca": "cat",
    "cs": "ces", "cy": "cym", "da": "dan", "de": "deu", "dv": "div", "el": "ell",
    "en": "eng", "eo": "epo", "es": "spa", "et": "est", "eu": "eus", "fa": "fas",
    "fi": "fin", "fo": "fao", "fr": "fra", "fy": "fry", "ga": "gle", "gd": "gla",
    "gl": "glg", "gu": "guj", "he": "heb", "iw": "heb", "hi": "hin", "hr": "hrv",
    "ht": "hat", "hu": "hun", "hy": "hye", "id": "ind", "is": "isl", "it": "ita",
    "iu": "iku", "ja": "jpn", "jv": "jav", "ka": "kat", "kk": "kaz", "km": "khm",
    "kn": "kan", "ko": "kor", "ku": "kmr", "ky": "kir", "la": "lat", "lb": "ltz",
    "lo": "lao", "lt": "lit", "lv": "lav", "mi": "mri", "mk": "mkd", "ml": "mal",
    "mn": "mon", "mr": "mar", "ms": "msa", "mt": "mlt", "my": "mya", "ne": "nep",
    "nl": "nld", "no": "nor", "nb": "nor", "nn": "nor", "oc": "oci", "or": "ori",
    "pa": "pan", "pl": "pol", "ps": "pus", "pt": "por", "qu": "que", "ro": "ron",
    "ru": "rus", "sa": "san", "sd": "snd", "si": "sin", "sk": "slk", "sl": "slv",
    "sq": "sqi", "sr": "srp", "su": "sun", "sv": "swe", "sw": "swa", "ta": "tam",
    "te": "tel", "tg": "tgk", "th": "tha", "ti": "tir", "tl": "tgl", "to": "ton",
    "tr": "tur", "tt": "tat", "ug": "uig", "uk": "ukr", "ur": "urd", "uz": "uzb",
    "vi": "vie", "yi": "yid", "yo": "yor",
}  # fmt: skip
# ISO 639-2/B codes that differ from the /T codes Tesseract uses.
_ISO2B = {
    "alb": "sqi", "arm": "hye", "baq": "eus", "bur": "mya", "chi": "chi_sim", "cze": "ces",
    "dut": "nld", "fre": "fra", "geo": "kat", "ger": "deu", "gre": "ell", "ice": "isl",
    "mac": "mkd", "may": "msa", "per": "fas", "rum": "ron", "slo": "slk", "tib": "bod",
    "wel": "cym", "zho": "chi_sim",
}  # fmt: skip
_TRADITIONAL = ("hant", "tw", "hk", "mo")


def language_for(work_language: str | None) -> str:
    """Tesseract's code for a work's language metadata, or English when it is unknown."""
    raw = (work_language or "").strip().lower().replace("_", "-")
    if not raw:
        return DEFAULT_LANGUAGE
    parts = raw.split("-")
    base = parts[0]
    if base == "zh" or base in ("chi", "zho"):
        return "chi_tra" if any(p in _TRADITIONAL for p in parts[1:]) else "chi_sim"
    if base == "sr" and "latn" in parts[1:]:
        return "srp_latn"
    if len(base) == 2:
        return _ISO1.get(base, DEFAULT_LANGUAGE)
    if len(base) == 3 and base.isalpha() and base != "osd":
        return _ISO2B.get(base, base)
    return DEFAULT_LANGUAGE


def parse_spec(spec: str) -> list[str]:
    """``eng+fra`` → ``["eng", "fra"]``; ``ValueError`` for anything that is not 1 to
    :data:`MAX_LANGUAGES` Tesseract codes (they become a command-line argument)."""
    codes = (spec or "").split("+")
    if not 1 <= len(codes) <= MAX_LANGUAGES or not all(LANG_CODE.fullmatch(c) for c in codes):
        raise ValueError(f"not a Tesseract language: {spec!r:.40}")
    if len(set(codes)) != len(codes):
        raise ValueError("a language is listed twice")
    return codes


def tesseract(path: str = ENGINE) -> str | None:
    """The Tesseract executable, or ``None`` when it is not installed."""
    return shutil.which(path)


def probe(path: str = ENGINE) -> tuple[str | None, tuple[str, ...]]:
    """``(version, languages)`` of the Tesseract at ``path`` (cached for a few minutes);
    ``(None, ())`` when there is none. ``osd`` (orientation data) is not a language."""
    exe = tesseract(path)
    if exe is None:
        return None, ()
    now = time.monotonic()
    with _lock:
        hit = _cache.get(exe)
        if hit and now - hit[0] < _CACHE_S:
            return hit[1], hit[2]
    env = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}
    if prefix := os.environ.get("TESSDATA_PREFIX"):
        env["TESSDATA_PREFIX"] = prefix
    version: str | None = None
    langs: tuple[str, ...] = ()
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv
            [exe, "--version"], capture_output=True, timeout=20, check=False, env=env
        )
        text = (out.stdout or out.stderr).decode("utf-8", "replace")
        m = re.search(r"tesseract\s+v?(\d+(?:\.\d+)*)", text, re.I)
        version = m.group(1) if m else None
        out = subprocess.run(  # noqa: S603 - fixed argv
            [exe, "--list-langs"], capture_output=True, timeout=20, check=False, env=env
        )
        lines = (out.stdout or b"").decode("utf-8", "replace").splitlines()
        langs = tuple(
            sorted(
                c for c in (ln.strip() for ln in lines[1:]) if LANG_CODE.fullmatch(c) and c != "osd"
            )
        )
    except (OSError, subprocess.SubprocessError):
        version, langs = None, ()
    if version is None:
        return None, ()
    with _lock:
        _cache[exe] = (now, version, langs)
    return version, langs


def missing_languages(spec: str, installed: tuple[str, ...]) -> list[str]:
    return [c for c in parse_spec(spec) if c not in installed]


def label(version: str | None) -> str | None:
    return f"{ENGINE} {version}" if version else None
