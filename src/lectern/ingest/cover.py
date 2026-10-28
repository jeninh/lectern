"""Cover images: the one guarded way to decode one, and the dominant colour the UI uses to
tint Continue cards.

Every cover (uploaded in the editor, embedded in an EPUB, attached to an audio file) is
untrusted bytes. :func:`save_cover_jpeg` refuses oversized input, opens it with an allowlist of
Pillow decoders, and checks the dimensions from the header *before* decoding pixels, so a small
file claiming a gigapixel canvas (a decompression bomb) is refused without allocating it.
"""

from __future__ import annotations

import colorsys
import math
from io import BytesIO
from pathlib import Path

COVER_MAX_BYTES = 10 * 1024 * 1024
COVER_MAX_PIXELS = 40_000_000
COVER_MAX_SIDE = 16_000
UPLOAD_FORMATS = ("JPEG", "PNG", "WEBP")
EMBEDDED_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")
CROP_MIN_PX = 200


class CoverRejected(ValueError):
    """``code`` is ``cover_too_large`` or ``not_an_image``."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def save_cover_jpeg(
    data: bytes, dest: Path, *, formats: tuple[str, ...] = UPLOAD_FORMATS, quality: int = 88
) -> None:
    """Decode ``data`` (bounded), scale it to at most 1200×1600, and write it as JPEG."""
    from PIL import Image

    if len(data) > COVER_MAX_BYTES:
        raise CoverRejected("cover_too_large")
    try:
        img = Image.open(BytesIO(data), formats=list(formats))
    except Image.DecompressionBombError:
        raise CoverRejected("cover_too_large") from None
    except Exception:  # noqa: BLE001 - anything Pillow will not open is not a cover
        raise CoverRejected("not_an_image") from None
    with img:
        w, h = img.size
        if w < 1 or h < 1 or w > COVER_MAX_SIDE or h > COVER_MAX_SIDE or w * h > COVER_MAX_PIXELS:
            raise CoverRejected("cover_too_large")
        try:
            rgb = img.convert("RGB")
            rgb.thumbnail((1200, 1600))
        except Exception:  # noqa: BLE001 - truncated or corrupt pixel data
            raise CoverRejected("not_an_image") from None
    dest.parent.mkdir(parents=True, exist_ok=True)
    rgb.save(dest, "JPEG", quality=quality)


class CropRejected(ValueError):
    """The crop rectangle is not finite, not inside the image, or too small."""


def crop_jpeg(src: Path, x: float, y: float, w: float, h: float, *, quality: int = 88) -> bytes:
    """Crop ``src`` (a staged JPEG this server wrote) to the normalised rectangle and return
    the JPEG bytes. The rectangle must lie inside the image and cover at least
    :data:`CROP_MIN_PX` pixels on each side. The caller re-validates the result with
    :func:`save_cover_jpeg`."""
    from PIL import Image

    vals = (x, y, w, h)
    if not all(isinstance(v, int | float) and math.isfinite(v) for v in vals):
        raise CropRejected("not finite")
    eps = 1e-6
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1 + eps or y + h > 1 + eps:
        raise CropRejected("outside the image")
    try:
        with Image.open(src, formats=["JPEG"]) as img:
            width, height = img.size
            if width * height > COVER_MAX_PIXELS:
                raise CropRejected("image too large")
            left, top = round(x * width), round(y * height)
            right, bottom = min(width, round((x + w) * width)), min(height, round((y + h) * height))
            if right - left < CROP_MIN_PX or bottom - top < CROP_MIN_PX:
                raise CropRejected("too small")
            piece = img.convert("RGB").crop((left, top, right, bottom))
    except CropRejected:
        raise
    except Exception:  # noqa: BLE001 - an unreadable staged file cannot be cropped
        raise CropRejected("unreadable") from None
    buf = BytesIO()
    piece.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def dominant_tint(path: Path) -> str | None:
    """Return '#rrggbb' for the most prominent mid-lightness colour, or None."""
    from PIL import Image

    try:
        with Image.open(path) as img:
            small = img.convert("RGB").resize((32, 32))
            quant = small.quantize(colors=6, method=Image.Quantize.MEDIANCUT)
            palette = quant.getpalette()
            counts = sorted(quant.getcolors() or [], reverse=True)
    except Exception:  # noqa: BLE001 - a bad image just means no tint
        return None
    if not palette or not counts:
        return None
    candidates: list[tuple[float, tuple[int, int, int]]] = []
    for count, idx in counts:
        r, g, b = palette[idx * 3 : idx * 3 + 3]
        _h, lightness, sat = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        # Score prominence by count, but prefer colours that are neither paper-white nor ink-black.
        usable = 0.12 <= lightness <= 0.82
        score = count * (1.0 + 2.0 * sat) * (1.0 if usable else 0.15)
        candidates.append((score, (r, g, b)))
    _score, (r, g, b) = max(candidates)
    return f"#{r:02x}{g:02x}{b:02x}"
