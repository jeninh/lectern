"""Dominant cover colour, used by the UI to tint Continue cards."""

from __future__ import annotations

import colorsys
from pathlib import Path


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
