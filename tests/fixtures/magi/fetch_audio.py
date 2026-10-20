#!/usr/bin/env python3
"""Fetch the full public-domain LibriVox recording this fixture is cut from, and verify it.

This is a development/test helper for one specific, documented public-domain file (see
PROVENANCE.md). It is not part of the Lectern server and Lectern has no general audio or book
download feature.

Usage:
    python tests/fixtures/magi/fetch_audio.py [DEST_DIR] [--excerpt]

Downloads gift_of_the_magi_henry_blb_64kb.mp3 into DEST_DIR (default: a `full/` directory next
to this script, which is git-ignored), checks its SHA-256, and with --excerpt re-cuts excerpt.mp3
from it with ffmpeg so the cut can be reproduced or adjusted.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import urllib.request
from pathlib import Path

URL = "https://archive.org/download/giftofmagi/gift_of_the_magi_henry_blb_64kb.mp3"
SHA256 = "4dfe1b8eb7d7617360b6ae3f6b27fc342f8e14a718ca80e603e4780c398717f9"
SIZE = 6_420_608
EXCERPT_START_S = 24.50
EXCERPT_LEN_S = 77.10

HERE = Path(__file__).resolve().parent


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / URL.rsplit("/", 1)[1]
    if out.exists() and out.stat().st_size == SIZE and sha256_of(out) == SHA256:
        print(f"already present and verified: {out}")
        return out
    print(f"downloading {URL}")
    with urllib.request.urlopen(URL, timeout=120) as resp, out.open("wb") as f:  # noqa: S310
        while chunk := resp.read(1 << 20):
            f.write(chunk)
    digest = sha256_of(out)
    if digest != SHA256:
        out.unlink()
        raise SystemExit(f"SHA-256 mismatch: got {digest}, expected {SHA256}")
    print(f"verified: {out} ({out.stat().st_size} bytes)")
    return out


def cut_excerpt(full: Path, out: Path) -> None:
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-ss", f"{EXCERPT_START_S:.2f}", "-t", f"{EXCERPT_LEN_S:.2f}", "-i", str(full),
        "-ac", "1", "-ar", "22050", "-c:a", "libmp3lame", "-b:a", "24k",
        "-metadata", "title=The Gift of the Magi (excerpt 24.5-101.6 s)",
        "-metadata", "artist=Betsie Bush (LibriVox)",
        "-metadata", f"comment=Public domain. Excerpt of {URL}",
        str(out),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)  # noqa: S603
    print(f"wrote {out} ({out.stat().st_size} bytes, sha256 {sha256_of(out)})")
    print("note: the sentence map's track sha256 refers to the checked-in excerpt.mp3")


def main(argv: list[str]) -> None:
    args = [a for a in argv if not a.startswith("--")]
    dest = Path(args[0]) if args else HERE / "full"
    full = fetch(dest)
    if "--excerpt" in argv:
        cut_excerpt(full, dest / "excerpt.mp3")


if __name__ == "__main__":
    main(sys.argv[1:])
