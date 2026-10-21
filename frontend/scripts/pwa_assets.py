"""Draw the installed app's icons and iPhone launch screens into frontend/public/pwa/.

Run from the repository root after changing the mark or the colours:

    uv run python frontend/scripts/pwa_assets.py

The outputs are committed; the build only copies them. Android draws its own launch screen
from the manifest (name, background_color, the 512 px icon); iOS needs one image per screen
size, listed in frontend/index.html.
"""

from __future__ import annotations

from pathlib import Path

import resvg_py

OUT = Path(__file__).resolve().parent.parent / "public" / "pwa"
ACCENT = "#1f3a93"
PAPER = "#f5efe4"
INK = "#221d17"

# The sidebar's brand mark (components/Shell.tsx), on a 24-unit grid.
MARK = (
    '<path d="M6 4h9a3 3 0 0 1 3 3v13l-4.5-2.5L9 20V4.5" fill="{c}" opacity=".55"/>'
    '<path d="M6 4h3v16l-3-1.7z" fill="{c}"/>'
)


def mark(x: float, y: float, size: float, colour: str) -> str:
    s = size / 24
    return f'<g transform="translate({x} {y}) scale({s})">{MARK.format(c=colour)}</g>'


def icon_svg(size: int, *, maskable: bool, rounded: bool) -> str:
    # Maskable icons keep the mark inside the central 80 % safe zone; Android crops the rest.
    glyph = size * (0.5 if maskable else 0.62)
    radius = size * 0.22 if rounded else 0
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}">'
        f'<rect width="{size}" height="{size}" rx="{radius}" fill="{ACCENT}"/>'
        f"{mark((size - glyph) / 2 + glyph * 0.02, (size - glyph) / 2, glyph, PAPER)}</svg>"
    )


def splash_svg(w: int, h: int, scale: int) -> str:
    tile = 88 * scale
    x, y = (w - tile) / 2, h * 0.42 - tile / 2
    glyph = tile * 0.62
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
        f'<rect width="{w}" height="{h}" fill="{PAPER}"/>'
        f'<rect x="{x}" y="{y}" width="{tile}" height="{tile}" rx="{tile * 0.22}" fill="{ACCENT}"/>'
        f"{mark(x + (tile - glyph) / 2, y + (tile - glyph) / 2, glyph, PAPER)}"
        f'<text x="{w / 2}" y="{y + tile + 44 * scale}" text-anchor="middle" fill="{INK}" '
        f'font-family="DejaVu Serif, serif" font-size="{30 * scale}">Lectern</text></svg>'
    )


def png(svg: str) -> bytes:
    return bytes(resvg_py.svg_to_bytes(svg_string=svg))


# iPhone screens in portrait: CSS width, height, and device pixel ratio.
SCREENS = [
    (440, 956, 3),  # 16 Pro Max
    (402, 874, 3),  # 16 Pro
    (430, 932, 3),  # 14 Pro Max, 15 Plus / Pro Max, 16 Plus
    (393, 852, 3),  # 14 Pro, 15, 15 Pro, 16
    (428, 926, 3),  # 12 / 13 Pro Max, 14 Plus
    (390, 844, 3),  # 12, 13, 14
    (375, 812, 3),  # X, XS, 11 Pro, 12 mini, 13 mini
    (414, 896, 3),  # XS Max, 11 Pro Max
    (414, 896, 2),  # XR, 11
    (375, 667, 2),  # SE, 8
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "icon.svg").write_text(icon_svg(32, maskable=False, rounded=True))
    (OUT / "icon-192.png").write_bytes(png(icon_svg(192, maskable=False, rounded=True)))
    (OUT / "icon-512.png").write_bytes(png(icon_svg(512, maskable=False, rounded=True)))
    (OUT / "icon-maskable-512.png").write_bytes(png(icon_svg(512, maskable=True, rounded=False)))
    # iOS rounds the corners itself and shows transparency as black.
    (OUT / "apple-touch-icon.png").write_bytes(png(icon_svg(180, maskable=False, rounded=False)))
    links = []
    for w, h, dpr in SCREENS:
        name = f"splash-{w * dpr}x{h * dpr}.png"
        (OUT / name).write_bytes(png(splash_svg(w * dpr, h * dpr, dpr)))
        links.append(
            f'<link rel="apple-touch-startup-image" href="/app/pwa/{name}" media="(device-width: '
            f"{w}px) and (device-height: {h}px) and (-webkit-device-pixel-ratio: {dpr}) and "
            '(orientation: portrait)" />'
        )
    print("\n".join(links))


if __name__ == "__main__":
    main()
