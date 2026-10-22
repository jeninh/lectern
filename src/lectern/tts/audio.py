"""Chapter files: raw PCM from an engine, encoded once by ffmpeg into AAC (``.m4a``) or FLAC.

The encoder must not trim or pad the start (sentence times are measured in samples from the
first one), so ffmpeg reads the PCM as is and writes no leading silence removal; AAC priming
is hidden by the MP4 edit list ffmpeg writes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from lectern.ingest.hashing import sha256_file

EXT = {"aac": "m4a", "flac": "flac"}
MEDIA_TYPE = {"aac": "audio/mp4", "flac": "audio/flac"}


def encode(
    ffmpeg: str,
    pcm: Path,
    dest: Path,
    *,
    sample_rate: int,
    codec: str,
    bitrate_kbps: int,
    tags: dict[str, str],
) -> str:
    """Encode 16-bit mono PCM at ``pcm`` into ``dest`` atomically; returns its SHA-256."""
    if shutil.which(ffmpeg) is None:
        raise RuntimeError("ffmpeg is not installed")
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    args = [ffmpeg, "-nostdin", "-v", "error", "-y", "-f", "s16le", "-ar", str(sample_rate),
            "-ac", "1", "-i", str(pcm), "-map_metadata", "-1"]  # fmt: skip
    for k, v in tags.items():
        if v:
            args += ["-metadata", f"{k}={v[:250]}"]
    if codec == "flac":
        args += ["-c:a", "flac", "-f", "flac"]
    else:
        args += ["-c:a", "aac", "-b:a", f"{bitrate_kbps}k", "-movflags", "+faststart", "-f", "mp4"]
    try:
        res = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [*args, str(tmp)], capture_output=True, timeout=3600, check=False
        )
        if res.returncode != 0:
            raise RuntimeError(res.stderr.decode(errors="replace")[-500:] or "ffmpeg failed")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return sha256_file(dest)
