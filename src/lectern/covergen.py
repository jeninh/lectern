"""Generated covers for works without cover art.

A work with no uploaded or embedded cover gets a designed one: a palette, a style (classic,
modern, minimal), and a pattern chosen from a hash of its title and author, so every book
looks distinct and keeps its look across restarts and machines. The cover is drawn as SVG and
rasterised by resvg to JPEG on the server, so the web app, OPDS clients, KOReader, and the
Kindle all receive the same image from ``/library/works/<id>/cover.jpg``.

Precedence: ``Work.has_cover`` (uploaded or embedded art in ``cover.jpg``) always wins. A
generated cover is never written to ``cover.jpg``; it lives in ``<work>/derived/cover-<key>.jpg``,
the key hashing everything it was drawn from (fonts included), and is redrawn on the next request when
the title, author, style, palette seed, or this renderer changes. ``Work.cover_style`` (None
= chosen by the hash) and ``Work.cover_seed`` (the editor's shuffle) are the only stored
choices.

Text: titles are set in a static instance of Newsreader (the app's display serif); any
character it lacks falls back, per character, to bundled Noto subsets (Greek and Cyrillic,
Coptic, Arabic, Hebrew, Devanagari) and to the system's Noto Serif/Sans CJK for Chinese,
Japanese, and Korean (``fonts-noto-cjk`` in the Docker image). Fonts are passed to resvg
explicitly and system fonts are skipped, so output does not depend on the host. Each line is
emitted as runs of ``<tspan>`` with one font each; resvg shapes them (joining Arabic,
conjuncts in Devanagari) and applies the Unicode bidi algorithm, and right-to-left titles are
right-aligned where the style aligns to a side.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import secrets
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

log = logging.getLogger(__name__)

# Bump when the drawing changes, so every cached generated cover is redrawn. (Font changes
# need no bump: the cache key includes the bundled fonts' and the CJK font's content.)
RENDERER_VERSION = 2
WIDTH, HEIGHT = 800, 1200
STYLES = ("classic", "modern", "minimal")
# The title face: "display" (Newsreader Display, the default), "text" (Newsreader Text), or
# "noto" (Noto Serif). Only the title's first face changes; fallbacks and the author line do not.
FONTS = ("display", "text", "noto")
DEFAULT_FONT = "display"
PATTERNS = ("stripes", "arcs", "grid", "dots")
MAX_TEXT = 512  # characters of title or author considered (bounds render cost)
DERIVED_DIR = "derived"
# derived/cover-<key>.jpg: the key is in the name, so an image can never be served under
# another design's key, even when two requests render different designs at the same time.
CACHE_PREFIX = "cover-"

FONT_DIR = Path(__file__).parent / "fonts"


# --- palettes and contrast ---------------------------------------------------------------------


@dataclass(frozen=True)
class Palette:
    name: str
    ground: str  # the cover
    ink: str  # title and author
    accent: str  # rules, frames, ornaments, patterns (never under text)
    block: str  # the modern style's colour block, which the author sits on


# Deep, muted, literary. Every pair that carries text meets WCAG AA for normal-size text
# (4.5:1) even under the cover's light and shade overlays; accents meet 3:1 (graphics).
# tests/test_covergen.py enforces both.
PALETTES: tuple[Palette, ...] = (
    Palette("oxblood", "#5b1a22", "#f4e9d8", "#d4a95f", "#3e1016"),
    Palette("forest", "#1f3d2c", "#ede6d1", "#c7a85c", "#132a1d"),
    Palette("ink", "#1a2433", "#ece4d2", "#c9a25b", "#0e1520"),
    Palette("ochre", "#c99036", "#1e1408", "#4a2810", "#dcae62"),
    Palette("plum", "#4b2346", "#f3e5ee", "#d7ae6f", "#331731"),
    Palette("teal", "#1c4a4d", "#e9f0ea", "#e0b46a", "#123336"),
    Palette("parchment", "#ece3cd", "#2b2118", "#8c3a26", "#ddd0b2"),
    Palette("sage", "#c6cdb4", "#1f2a1d", "#4b5e3a", "#b2bb9c"),
    Palette("terracotta", "#8e3d24", "#fbefe2", "#f0c987", "#6f2d19"),
    Palette("midnight", "#202c55", "#efe8dc", "#d9b56c", "#151e40"),
    Palette("rosewood", "#6b3a46", "#f6ebe6", "#e3b98a", "#512a34"),
)

# The faint lighting laid over the whole cover: up to this much white at the top left and
# black at the bottom right. Contrast is checked against both extremes.
LIGHT_OVERLAY = 0.07
SHADE_OVERLAY = 0.16


def _rgb(hex_colour: str) -> tuple[float, float, float]:
    h = hex_colour.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(min(1.0, max(0.0, c)) * 255):02x}" for c in rgb)


def mix(a: str, b: str, amount: float) -> str:
    """``amount`` of ``b`` over ``a`` (alpha compositing)."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(x * (1 - amount) + y * amount for x, y in zip(ra, rb, strict=True)))  # type: ignore[arg-type]


def luminance(hex_colour: str) -> float:
    """WCAG 2 relative luminance."""

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in _rgb(hex_colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# --- the design a work gets ----------------------------------------------------------------------


def _clean(text: str | None) -> str:
    text = unicodedata.normalize("NFC", text or "")
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" or ch in "\u200c\u200d")
    return " ".join(text.split())[:MAX_TEXT]


@dataclass(frozen=True)
class Design:
    title: str
    author: str
    style: str
    palette: Palette
    pattern: str
    ornament: int
    lang: str | None
    font: str = DEFAULT_FONT

    def __post_init__(self) -> None:
        if self.font not in FONTS:
            raise ValueError(f"unknown cover font {self.font!r}")

    @property
    def key(self) -> str:
        """Everything the image depends on, fonts included (names ``derived/cover-<key>.jpg``)."""
        parts = [
            RENDERER_VERSION,
            self.title,
            self.author,
            self.style,
            self.palette.name,
            self.pattern,
            self.ornament,
            _cjk_variant(self.lang),
            _bundled_fonts_digest(),
            # Only CJK text is drawn with the system CJK font: its file and content.
            _cjk_fonts_digest() if any(_is_cjk(c) for c in self.title + self.author) else "",
        ]
        if self.font != DEFAULT_FONT:  # appended only then: existing keys (and ?v=) stay valid
            parts.append(self.font)
        raw = json.dumps(parts, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


def design_for(
    title: str | None,
    author: str | None,
    *,
    style: str | None = None,
    seed: int = 0,
    lang: str | None = None,
    font: str | None = None,
) -> Design:
    """The deterministic design for a title and author. ``style`` overrides the hashed style;
    ``seed`` (the editor's shuffle) steps through the palettes from the hashed one."""
    t, a = _clean(title) or "Untitled", _clean(author)
    digest = hashlib.sha256(f"{t}\x1f{a}".encode()).digest()
    pick = int.from_bytes(digest[:4], "big")
    palette = PALETTES[(pick + max(0, seed)) % len(PALETTES)]
    return Design(
        title=t,
        author=a,
        style=style if style in STYLES else STYLES[digest[4] % len(STYLES)],
        palette=palette,
        pattern=PATTERNS[digest[5] % len(PATTERNS)],
        ornament=digest[6] % 3,
        lang=lang,
        font=font or DEFAULT_FONT,
    )


UNKNOWN_AUTHOR = "Unknown Author"  # lectern.workmeta.display_author's placeholder


def design_of(work) -> Design:
    author = "" if work.author == UNKNOWN_AUTHOR else work.author
    return design_for(
        work.title,
        author,
        style=work.cover_style,
        seed=work.cover_seed or 0,
        lang=work.language,
        font=work.cover_font,
    )


def tint_of(work) -> str | None:
    """The tint the UI uses for cards: the cover art's, or the generated cover's ground."""
    if work.has_cover:
        return work.cover_tint
    return design_of(work).palette.ground


def cover_url(work) -> str:
    """Generated covers also carry their design key, so a new renderer (or palette set) reaches
    browsers that cached the old image under the same work version."""
    v = str(work.version) if work.has_cover else f"{work.version}.{design_of(work).key[:10]}"
    return f"/library/works/{work.id}/cover.jpg?v={v}"


def cover_fields(work) -> dict:
    """``has_cover``/``cover_url``/``cover_tint``/``cover_generated`` for API payloads.

    Every work has a cover URL: the art when there is some, the generated cover otherwise.
    ``?v=`` changes with every edit (title, author, style, shuffle, or art), so a browser never
    shows a stale cover from its cache."""
    return {
        "has_cover": work.has_cover,
        "cover_url": cover_url(work),
        "cover_tint": tint_of(work),
        "cover_generated": not work.has_cover,
    }


# --- fonts -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Face:
    family: str
    path: Path
    index: int = 0


DISPLAY = Face("Lectern Cover Display", FONT_DIR / "LecternCover-Display.ttf")
TEXT = Face("Lectern Cover Text", FONT_DIR / "LecternCover-Text.ttf")
NOTO_SERIF = Face("Lectern Noto Serif", FONT_DIR / "LecternCover-NotoSerif.ttf")
COPTIC = Face("Lectern Noto Sans Coptic", FONT_DIR / "LecternCover-NotoSansCoptic.ttf")
ARABIC = Face("Lectern Noto Naskh Arabic", FONT_DIR / "LecternCover-NotoNaskhArabic.ttf")
HEBREW = Face("Lectern Noto Serif Hebrew", FONT_DIR / "LecternCover-NotoSerifHebrew.ttf")
DEVANAGARI = Face(
    "Lectern Noto Serif Devanagari", FONT_DIR / "LecternCover-NotoSerifDevanagari.ttf"
)
BUNDLED_FALLBACKS = (NOTO_SERIF, COPTIC, ARABIC, HEBREW, DEVANAGARI)

# fonts-noto-cjk (Debian/Ubuntu) and the usual alternatives. Serif first: it suits a cover.
FONT_SEARCH_DIRS = (
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path.home() / ".local/share/fonts",
)
CJK_FILES = (
    "NotoSerifCJK-Regular.ttc",
    "NotoSerifCJK-Medium.ttc",
    "NotoSansCJK-Regular.ttc",
    "NotoSansCJK-Medium.ttc",
)
_CJK_FAMILY = {"jp": "JP", "sc": "SC", "tc": "TC", "kr": "KR"}


def _digest(paths) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(p.name.encode() + b"\0")
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()[:16]


@functools.cache
def _bundled_fonts_digest() -> str:
    return _digest(sorted(FONT_DIR.glob("*.ttf")))


@functools.cache
def _cjk_fonts_digest() -> str:
    """Which CJK font file is used and what is in it (hashed once per process)."""
    try:
        return _digest(_cjk_font_files()) if _cjk_font_files() else "none"
    except OSError:
        return "unreadable"


@functools.cache
def _cjk_font_files() -> tuple[Path, ...]:
    extra = os.environ.get("LECTERN_COVER_FONT_DIRS", "")
    dirs = [Path(d) for d in extra.split(os.pathsep) if d] + list(FONT_SEARCH_DIRS)
    for name in CJK_FILES:
        for d in dirs:
            try:
                found = next(iter(sorted(d.rglob(name))), None) if d.is_dir() else None
            except OSError:
                found = None
            if found:
                return (found,)
    return ()


def _cjk_variant(lang: str | None) -> str:
    lang = (lang or "").lower().replace("_", "-")
    if lang.startswith("ko"):
        return "kr"
    if lang.startswith("zh"):
        return "tc" if any(r in lang for r in ("-tw", "-hk", "-mo", "hant")) else "sc"
    return "jp"


@functools.cache
def _cjk_face(variant: str) -> Face | None:
    files = _cjk_font_files()
    if not files:
        return None
    from fontTools.ttLib import TTCollection, TTFont

    path = files[0]
    want = _CJK_FAMILY[variant]
    try:
        if path.suffix.lower() == ".ttc":
            coll = TTCollection(str(path), lazy=True)
            fonts = list(coll.fonts)
        else:
            fonts = [TTFont(str(path), lazy=True)]
        names = [f["name"].getDebugName(1) or "" for f in fonts]
    except Exception:  # noqa: BLE001 - an unreadable system font just means no CJK
        log.warning("could not read CJK font %s", path)
        return None
    for i, n in enumerate(names):
        if n.endswith(f" {want}"):
            return Face(n, path, i)
    return Face(names[0], path, 0) if names else None


@functools.cache
def _cmap(face: Face) -> frozenset[int]:
    from fontTools.ttLib import TTFont

    font = TTFont(str(face.path), fontNumber=face.index, lazy=True)
    return frozenset(font.getBestCmap() or {})


@functools.cache
def _pil_font(face: Face):
    from PIL import ImageFont

    return ImageFont.truetype(
        str(face.path), 1000, index=face.index, layout_engine=ImageFont.Layout.BASIC
    )


def _width(face: Face, text: str) -> float:
    """Advance width of ``text`` at 1 px font size (unshaped: a slight overestimate for joined
    scripts, which only errs towards smaller type)."""
    return _pil_font(face).getlength(text) / 1000


# --- scripts and runs ------------------------------------------------------------------------


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x2E80 <= o <= 0x9FFF  # radicals, kana, CJK punctuation, unified ideographs
        or 0xAC00 <= o <= 0xD7AF  # Hangul syllables
        or 0x1100 <= o <= 0x11FF
        or 0xF900 <= o <= 0xFAFF
        or 0xFF00 <= o <= 0xFFEF  # full-width forms
        or 0x20000 <= o <= 0x3FFFF
    )


def _is_cased(ch: str) -> bool:
    """Scripts that take tracking and small caps (Latin, Greek, Cyrillic, Coptic, Armenian)."""
    o = ord(ch)
    return ch.isalpha() and (o < 0x0590 or 0x1E00 <= o <= 0x1FFF or 0x2C80 <= o <= 0x2CFF)


def is_rtl(text: str) -> bool:
    """Paragraph direction from the first strong character (Unicode bidi rule P2)."""
    for ch in text:
        d = unicodedata.bidirectional(ch)
        if d in ("R", "AL"):
            return True
        if d == "L":
            return False
    return False


def _chain(primary: Face, lang: str | None) -> tuple[Face, ...]:
    cjk = _cjk_face(_cjk_variant(lang))
    return (primary, *BUNDLED_FALLBACKS, *((cjk,) if cjk else ()))


def _face_for(ch: str, chain: tuple[Face, ...], current: Face | None) -> Face | None:
    # Greek-derived Coptic letters (U+03E2-03EF) belong with the Coptic face.
    if 0x03E2 <= ord(ch) <= 0x03EF and ord(ch) in _cmap(COPTIC):
        return COPTIC
    if current is not None and ord(ch) in _cmap(current):
        cat = unicodedata.category(ch)
        # Spaces, punctuation, digits, and marks stay with the run they are in.
        if not ch.isalpha() or cat.startswith("M") or current is chain[0]:
            return current
    for face in chain:
        if ord(ch) in _cmap(face):
            return face
    return current


def runs(text: str, chain: tuple[Face, ...]) -> list[tuple[Face, str]]:
    """Split ``text`` into maximal runs that one face can draw."""
    out: list[tuple[Face, str]] = []
    current: Face | None = None
    for ch in text:
        if unicodedata.category(ch).startswith("M") or ch in "\u200c\u200d":
            face = current or chain[0]  # a mark or joiner stays with its base
        else:
            face = _face_for(ch, chain, current) or chain[0]
        if out and out[-1][0] == face:
            out[-1] = (face, out[-1][1] + ch)
        else:
            out.append((face, ch))
        current = face
    return out


def _tracked(text: str) -> bool:
    return any(_is_cased(c) for c in text) and not any(
        unicodedata.bidirectional(c) in ("R", "AL") or 0x0900 <= ord(c) <= 0x0DFF for c in text
    )


def measure(text: str, chain: tuple[Face, ...], size: float, tracking: float = 0.0) -> float:
    total = 0.0
    for face, run in runs(text, chain):
        total += _width(face, run) * size
        if tracking and _tracked(run):
            total += tracking * size * len(run)
    return total


# --- line breaking and fitting ---------------------------------------------------------------

# Never at the start of a line (kinsoku) / never at the end of one.
_NO_START = set(
    "、。，．・：；？！ー’”）〕］｝〉》」』】〙〗ゝゞ々ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ…‥"
)
_NO_END = set("‘“（〔［｛〈《「『【〘〖")
_BREAK_AFTER = set("-‐–—/")


@dataclass
class Token:
    text: str
    space_before: bool


def tokens(text: str) -> list[Token]:
    """Breakable units: words (split after hyphens and dashes), and single CJK characters with
    kinsoku punctuation kept with its neighbour."""
    out: list[Token] = []
    for w_i, word in enumerate(text.split(" ")):
        if not word:
            continue
        pieces: list[str] = []
        buf = ""
        for ch in word:
            if _is_cjk(ch) and ch not in _NO_START and ch not in _NO_END:
                if buf and buf[-1] not in _NO_END:
                    pieces.append(buf)
                    buf = ""
                buf += ch
            elif ch in _NO_START:
                buf += ch
            else:
                if buf and _is_cjk(buf[-1]) and buf[-1] not in _NO_END:
                    pieces.append(buf)
                    buf = ""
                buf += ch
                if ch in _BREAK_AFTER and len(buf) > 1:
                    pieces.append(buf)
                    buf = ""
        if buf:
            pieces.append(buf)
        for p_i, piece in enumerate(pieces):
            out.append(Token(piece, space_before=p_i == 0 and w_i > 0 and bool(out)))
    return out


def _join(toks: list[Token]) -> str:
    return "".join((" " if t.space_before and i else "") + t.text for i, t in enumerate(toks))


def _greedy(toks, widths, space, limit) -> list[list[int]] | None:
    lines: list[list[int]] = []
    cur: list[int] = []
    w = 0.0
    for i, t in enumerate(toks):
        add = widths[i] + (space if t.space_before and cur else 0.0)
        if cur and w + add > limit:
            lines.append(cur)
            cur, w = [i], widths[i]
        else:
            cur.append(i)
            w += add
        if widths[i] > limit:
            return None
    if cur:
        lines.append(cur)
    return lines


def balance(toks: list[Token], widths: list[float], space: float, limit: float):
    """Fewest lines that fit ``limit``, then the narrowest measure that keeps that many lines,
    so lines come out even (like CSS ``text-wrap: balance``) instead of a long line and a
    stray word."""
    best = _greedy(toks, widths, space, limit)
    if best is None or len(best) <= 1:
        return best
    lo, hi = max(widths), limit
    for _ in range(18):
        mid = (lo + hi) / 2
        trial = _greedy(toks, widths, space, mid)
        if trial is not None and len(trial) <= len(best):
            hi, best = mid, trial
        else:
            lo = mid
    return best


@dataclass
class Block:
    lines: list[str]
    size: float
    leading: float  # baseline to baseline

    @property
    def height(self) -> float:
        return self.size * 0.78 + self.leading * (len(self.lines) - 1) + self.size * 0.24


def _leading(text: str) -> float:
    if any(unicodedata.bidirectional(c) == "AL" or 0x0900 <= ord(c) <= 0x0DFF for c in text):
        return 1.42
    if any(_is_cjk(c) for c in text):
        return 1.28
    return 1.1


def fit(
    text: str,
    chain: tuple[Face, ...],
    *,
    width: float,
    height: float,
    max_size: float,
    min_size: float,
    max_lines: int,
    tracking: float = 0.0,
) -> Block:
    """The largest size (stepping down by 2 px) at which ``text`` wraps, balanced, into at
    most ``max_lines`` lines inside ``width`` × ``height``. Below ``min_size`` it is cut with
    an ellipsis instead; a single word too wide even then is broken between characters."""
    toks = tokens(text) or [Token(text or " ", False)]
    unit = [measure(t.text, chain, 1, tracking) for t in toks]
    space1 = measure(" ", chain, 1, tracking)
    lead = _leading(text)
    # A word too wide for the measure at min_size may shrink further before it is broken.
    floor = min_size * 0.6 if max(unit) * min_size > width else min_size
    size = max_size
    first: Block | None = None
    while size >= floor:
        widths = [u * size for u in unit]
        lines = balance(toks, widths, space1 * size, width)
        if lines is not None and len(lines) <= max_lines:
            block = Block([_join([toks[i] for i in ln]) for ln in lines], size, size * lead)
            if block.height <= height:
                # Prefer a slightly smaller size to a stranded last word or character.
                # Up to 30% smaller is worth it; beyond that the stranded line stays.
                spans = [sum(widths[i] for i in ln) for ln in lines]
                if len(lines) == 1 or min(spans) >= 0.45 * max(spans):
                    return block
                first = first or block
                if size < max_size * 0.7:
                    return first
        size -= 2
    if first is not None:
        return first
    size = floor
    lines_fit = max(1, min(max_lines, int((height - size * 1.02) // (size * lead)) + 1))
    # Too long even at the smallest size: break inside words if needed, then cut.
    chars = [Token(c, False) for c in text] if len(toks) == 1 else toks
    widths = [measure(t.text, chain, size, tracking) for t in chars]
    lines = _greedy_force(chars, widths, space1 * size, width)
    out = [_join([chars[i] for i in ln]) for ln in lines]
    if len(out) > lines_fit:
        out = out[:lines_fit]
        last = out[-1]
        while last and measure(last + "…", chain, size, tracking) > width:
            last = last[:-1]
        out[-1] = last.rstrip(" ,;:–—-") + "…"
    return Block(out, size, size * lead)


def _greedy_force(toks, widths, space, limit) -> list[list[int]]:
    lines: list[list[int]] = []
    cur: list[int] = []
    w = 0.0
    for i, t in enumerate(toks):
        add = widths[i] + (space if t.space_before and cur else 0.0)
        if cur and w + add > limit:
            lines.append(cur)
            cur, w = [i], widths[i]
        else:
            cur.append(i)
            w += add
    if cur:
        lines.append(cur)
    return lines


# --- drawing ---------------------------------------------------------------------------------


def _text_svg(
    line: str,
    chain: tuple[Face, ...],
    *,
    x: float,
    y: float,
    size: float,
    anchor: str,
    fill: str,
    tracking: float = 0.0,
    opacity: float = 1.0,
) -> str:
    spans = []
    for face, run in runs(line, chain):
        ls = f' letter-spacing="{tracking * size:.2f}"' if tracking and _tracked(run) else ""
        spans.append(f"<tspan font-family={quoteattr(face.family)}{ls}>{escape(run)}</tspan>")
    rtl = ' direction="rtl"' if is_rtl(line) else ""
    op = f' fill-opacity="{opacity}"' if opacity < 1 else ""
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size:.1f}" text-anchor="{anchor}"'
        f'{rtl} fill="{fill}"{op} xml:space="preserve">{"".join(spans)}</text>'
    )


def _block_svg(block: Block, chain, *, x: float, top: float, anchor: str, fill: str, **kw):
    first = top + block.size * 0.78
    return "".join(
        _text_svg(
            ln,
            chain,
            x=x,
            y=first + i * block.leading,
            size=block.size,
            anchor=anchor,
            fill=fill,
            **kw,
        )
        for i, ln in enumerate(block.lines)
    )


def _ornament(kind: int, cx: float, y: float, colour: str, half: float = 70) -> str:
    rule = (
        f'<line x1="{cx - half:.1f}" y1="{y:.1f}" x2="{cx + half:.1f}" y2="{y:.1f}" '
        f'stroke="{colour}" stroke-width="2"/>'
    )
    if kind == 0:  # rule with a lozenge
        d = 9
        return (
            f'<line x1="{cx - half:.1f}" y1="{y:.1f}" x2="{cx - d - 8:.1f}" y2="{y:.1f}" stroke="{colour}" stroke-width="2"/>'
            f'<line x1="{cx + d + 8:.1f}" y1="{y:.1f}" x2="{cx + half:.1f}" y2="{y:.1f}" stroke="{colour}" stroke-width="2"/>'
            f'<path d="M{cx:.1f} {y - d:.1f}L{cx + d:.1f} {y:.1f}L{cx:.1f} {y + d:.1f}L{cx - d:.1f} {y:.1f}Z" fill="{colour}"/>'
        )
    if kind == 1:  # three dots
        return "".join(
            f'<circle cx="{cx + dx:.1f}" cy="{y:.1f}" r="4" fill="{colour}"/>'
            for dx in (-18, 0, 18)
        )
    return rule  # a plain thin rule


def _pattern(kind: str, colour: str, x0: float, y0: float, x1: float, y1: float) -> str:
    clip = f'<clipPath id="pat"><rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}"/></clipPath>'
    g = []
    if kind == "stripes":
        step = 34
        for i in range(-int((y1 - y0) / step) - 2, int((x1 - x0) / step) + 2):
            x = x0 + i * step
            g.append(
                f'<line x1="{x}" y1="{y1}" x2="{x + (y1 - y0)}" y2="{y0}" stroke="{colour}" stroke-width="3"/>'
            )
    elif kind == "arcs":
        cx, cy = x1 - 40, y1
        for r in range(60, int(max(x1 - x0, y1 - y0) * 1.5), 44):
            g.append(
                f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{colour}" stroke-width="3"/>'
            )
    elif kind == "grid":
        step = 48
        x = x0 + step / 2
        while x < x1:
            g.append(
                f'<line x1="{x}" y1="{y0}" x2="{x}" y2="{y1}" stroke="{colour}" stroke-width="2"/>'
            )
            x += step
        y = y0 + step / 2
        while y < y1:
            g.append(
                f'<line x1="{x0}" y1="{y}" x2="{x1}" y2="{y}" stroke="{colour}" stroke-width="2"/>'
            )
            y += step
    else:  # dots
        step = 40
        y, row = y0 + step / 2, 0
        while y < y1:
            x = x0 + step / 2 + (step / 2 if row % 2 else 0)
            while x < x1:
                g.append(f'<circle cx="{x}" cy="{y}" r="5" fill="{colour}"/>')
                x += step
            y += step
            row += 1
    return f'{clip}<g clip-path="url(#pat)" opacity="0.38">{"".join(g)}</g>'


def svg(design: Design) -> str:
    """The cover as an SVG document (``WIDTH`` × ``HEIGHT``)."""
    p = design.palette
    title_chain = _chain({"text": TEXT, "noto": NOTO_SERIF}.get(design.font, DISPLAY), design.lang)
    text_chain = _chain(TEXT, design.lang)
    author = design.author
    author_tracking = 0.1 if _tracked(author) else 0.0
    author_text = author.upper() if author_tracking else author
    rtl = is_rtl(design.title)
    body: list[str] = []

    if design.style == "classic":
        body.append(
            f'<rect x="56" y="56" width="{WIDTH - 112}" height="{HEIGHT - 112}" fill="none" stroke="{p.accent}" stroke-width="3"/>'
            f'<rect x="72" y="72" width="{WIDTH - 144}" height="{HEIGHT - 144}" fill="none" stroke="{p.accent}" stroke-width="1.2" stroke-opacity="0.75"/>'
        )
        cx = (WIDTH + 36) / 2  # centred on the face, not counting the spine
        title = fit(
            design.title, title_chain, width=540, height=560, max_size=150, min_size=40, max_lines=6
        )
        top = 170 + (600 - title.height) / 2
        body.append(_block_svg(title, title_chain, x=cx, top=top, anchor="middle", fill=p.ink))
        body.append(_ornament(design.ornament, cx, 852, p.accent))
        if author:
            a = fit(
                author_text,
                text_chain,
                width=520,
                height=140,
                max_size=48,
                min_size=32,
                max_lines=2,
                tracking=author_tracking,
            )
            body.append(
                _block_svg(
                    a,
                    text_chain,
                    x=cx,
                    top=905 + (150 - a.height) / 2,
                    anchor="middle",
                    fill=p.ink,
                    tracking=author_tracking,
                )
            )
    elif design.style == "modern":
        left, right = 104, WIDTH - 80
        body.append(
            f'<rect x="0" y="820" width="{WIDTH}" height="{HEIGHT - 820}" fill="{p.block}"/>'
        )
        body.append(f'<rect x="0" y="812" width="{WIDTH}" height="8" fill="{p.accent}"/>')
        ax = right - 96 if rtl else left
        body.append(f'<rect x="{ax}" y="112" width="96" height="10" fill="{p.accent}"/>')
        title = fit(
            design.title,
            title_chain,
            width=right - left,
            height=600,
            max_size=176,
            min_size=44,
            max_lines=6,
        )
        top = 760 - title.height  # sits on the colour block
        x, anchor = (right, "end") if rtl else (left, "start")
        body.append(_block_svg(title, title_chain, x=x, top=top, anchor=anchor, fill=p.ink))
        if author:
            a = fit(
                author_text,
                text_chain,
                width=right - left,
                height=200,
                max_size=50,
                min_size=32,
                max_lines=3,
                tracking=author_tracking,
            )
            ax2, an2 = (right, "end") if is_rtl(author) else (left, "start")
            body.append(
                _block_svg(
                    a, text_chain, x=ax2, top=900, anchor=an2, fill=p.ink, tracking=author_tracking
                )
            )
    else:  # minimal
        left, right = 104, WIDTH - 88
        body.append(_pattern(design.pattern, p.accent, 36, 0, WIDTH, 470))
        title = fit(
            design.title,
            title_chain,
            width=right - left,
            height=400,
            max_size=132,
            min_size=40,
            max_lines=5,
        )
        x, anchor = (right, "end") if rtl else (left, "start")
        body.append(_block_svg(title, title_chain, x=x, top=560, anchor=anchor, fill=p.ink))
        rule_y = 560 + title.height + 44
        rx = right - 120 if rtl else left
        body.append(
            f'<line x1="{rx}" y1="{rule_y:.1f}" x2="{rx + 120}" y2="{rule_y:.1f}" stroke="{p.accent}" stroke-width="3"/>'
        )
        if author:
            a = fit(
                author_text,
                text_chain,
                width=right - left,
                height=150,
                max_size=46,
                min_size=32,
                max_lines=2,
                tracking=author_tracking,
            )
            ax2, an2 = (right, "end") if is_rtl(author) else (left, "start")
            body.append(
                _block_svg(
                    a,
                    text_chain,
                    x=ax2,
                    top=max(rule_y + 40, HEIGHT - 110 - a.height),
                    anchor=an2,
                    fill=p.ink,
                    tracking=author_tracking,
                )
            )

    # Lighting, then the spine: a shadow along the left edge and the crease beside it.
    defs = (
        "<defs>"
        f'<linearGradient id="light" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="#fff" stop-opacity="{LIGHT_OVERLAY}"/>'
        '<stop offset="0.5" stop-color="#fff" stop-opacity="0"/>'
        f'<stop offset="1" stop-color="#000" stop-opacity="{SHADE_OVERLAY}"/></linearGradient>'
        '<linearGradient id="spine" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0" stop-color="#000" stop-opacity="0.38"/>'
        '<stop offset="0.55" stop-color="#000" stop-opacity="0.12"/>'
        '<stop offset="1" stop-color="#000" stop-opacity="0"/></linearGradient>'
        "</defs>"
    )
    spine = (
        '<rect x="0" y="0" width="34" height="100%" fill="url(#spine)"/>'
        '<rect x="34" y="0" width="2" height="100%" fill="#fff" fill-opacity="0.10"/>'
        '<rect x="36" y="0" width="3" height="100%" fill="#000" fill-opacity="0.10"/>'
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}">{defs}'
        f'<rect width="100%" height="100%" fill="{p.ground}"/>'
        f"{''.join(body)}"
        '<rect width="100%" height="100%" fill="url(#light)"/>'
        f"{spine}</svg>"
    )


def _font_files(design: Design) -> list[str]:
    files = [str(f.path) for f in (DISPLAY, TEXT, *BUNDLED_FALLBACKS)]
    if any(_is_cjk(c) for c in design.title + design.author):
        files += [str(p) for p in _cjk_font_files()]
    return files


# At most two renders at once, so a grid of previews cannot saturate a small host.
_RENDERS = threading.BoundedSemaphore(2)


def render_png(design: Design, *, width: int = WIDTH) -> bytes:
    import resvg_py

    with _RENDERS:
        return bytes(
            resvg_py.svg_to_bytes(
                svg_string=svg(design),
                skip_system_fonts=True,
                font_files=_font_files(design),
                width=width,
                height=round(width * HEIGHT / WIDTH),
            )
        )


def render_jpeg(design: Design, *, width: int = WIDTH, quality: int = 88) -> bytes:
    from PIL import Image

    with Image.open(BytesIO(render_png(design, width=width))) as img:
        out = BytesIO()
        img.convert("RGB").save(out, "JPEG", quality=quality, optimize=True, progressive=True)
        return out.getvalue()


# --- in-memory previews -----------------------------------------------------------------------

PREVIEW_WIDTHS = (240, 480)
PREVIEW_QUALITY = 86
PREVIEW_CACHE_BYTES = 8 * 1024 * 1024
_previews: OrderedDict[tuple[str, int], bytes] = OrderedDict()
_previews_size = 0
_previews_lock = threading.Lock()


def preview_jpeg(design: Design, width: int) -> bytes:
    """A preview JPEG from a small LRU (about 8 MB) keyed by ``(design.key, width)``; drawn
    (at most two at a time) on a miss. The key covers everything drawn, so entries never go stale."""
    global _previews_size
    k = (design.key, width)
    with _previews_lock:
        if k in _previews:
            _previews.move_to_end(k)
            return _previews[k]
    data = render_jpeg(design, width=width, quality=PREVIEW_QUALITY)
    with _previews_lock:
        if k not in _previews:
            _previews[k] = data
            _previews_size += len(data)
        while _previews_size > PREVIEW_CACHE_BYTES and len(_previews) > 1:
            _, old = _previews.popitem(last=False)
            _previews_size -= len(old)
    return data


# --- the cache in derived/ ---------------------------------------------------------------------


def cache_path(work_dir: Path, design: Design, width: int = WIDTH) -> Path:
    """The full-size cover is ``cover-<key>.jpg``; a thumbnail (smaller ``width``) beside it,
    ``cover-<key>-<width>.jpg``."""
    suffix = "" if width == WIDTH else f"-{width}"
    return work_dir / DERIVED_DIR / f"{CACHE_PREFIX}{design.key}{suffix}.jpg"


def cached_cover(work_dir: Path, design: Design, width: int = WIDTH) -> bytes:
    """The generated cover's JPEG, from ``<work>/derived/cover-<key>.jpg`` or drawn and stored
    there. Returns the bytes (not the path) so a concurrent cleanup cannot pull the file out
    from under the response.

    Never creates the work folder: a title edit or trash can move it while the cover renders,
    and recreating the old path would leave an orphan folder (and make undo pick a different
    name). Only ``derived/`` is created, inside a folder that still exists; if the folder moved,
    the image is served without being cached and the next request caches it in the new place.
    """
    path = cache_path(work_dir, design, width)
    try:
        return path.read_bytes()
    except OSError:
        pass
    data = render_jpeg(design, width=width)
    derived = path.parent
    tmp = derived / f".{path.name}.{secrets.token_hex(6)}.tmp"
    try:
        if not work_dir.is_dir():
            return data
        derived.mkdir(exist_ok=True)  # no parents=True: never recreate a moved work folder
        tmp.write_bytes(data)
        os.replace(tmp, path)  # atomic; the key is in the name, so no sidecar can disagree
    except OSError:
        log.warning("could not cache the generated cover in %s", derived)
        tmp.unlink(missing_ok=True)
        return data
    # Best effort: drop covers drawn for earlier titles, authors, or styles.
    for old in derived.glob(f"{CACHE_PREFIX}*.jpg"):
        if not old.name.startswith(f"{CACHE_PREFIX}{design.key}"):
            old.unlink(missing_ok=True)
    return data
