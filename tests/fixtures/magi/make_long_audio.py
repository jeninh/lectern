"""Build a long synthetic audiobook track from the checked-in excerpt, for memory benchmarks.

The excerpt (77 s of LibriVox narration) is repeated, with two seconds of silence between
copies, into one file of at least ``--minutes``. Nothing is re-encoded (ffmpeg's concat
demuxer copies the MP3 frames), so a 10-hour file takes seconds to make and is never
committed:

    python tests/fixtures/magi/make_long_audio.py --minutes 600 /tmp/magi-10h.mp3
    uv run lectern align-bench tests/fixtures/magi/the-gift-of-the-magi.epub /tmp/magi-10h.mp3

Needs ffmpeg on PATH. See docs/alignment.md, "Long recordings".
"""

from __future__ import annotations

import argparse
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
EXCERPT = HERE / "excerpt.mp3"
EXCERPT_S = 77.166
GAP_S = 2.0


def build(out: Path, minutes: float) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("ffmpeg is required")
    copies = max(1, math.ceil(minutes * 60 / (EXCERPT_S + GAP_S)))
    with tempfile.TemporaryDirectory() as tmp:
        gap = Path(tmp) / "gap.mp3"
        subprocess.run(  # noqa: S603 - fixed argv
            [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-t", str(GAP_S), "-i",
             "anullsrc=r=22050:cl=mono", "-b:a", "24k", str(gap)],
            check=True,
        )  # fmt: skip
        listing = Path(tmp) / "list.txt"
        listing.write_text(
            "".join(f"file '{EXCERPT.resolve()}'\nfile '{gap}'\n" for _ in range(copies))
        )
        subprocess.run(  # noqa: S603 - fixed argv
            [ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-c", "copy", str(out)],
            check=True,
        )  # fmt: skip
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("out", type=Path)
    p.add_argument("--minutes", type=float, default=60.0)
    args = p.parse_args()
    print(build(args.out, args.minutes))


if __name__ == "__main__":
    main()
