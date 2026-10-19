"""Content digests.

partial_md5 reproduces KOReader's Document:fastDigest(): MD5 over 1 KiB samples taken at
offsets 0, 1024, 4096, 16384, ... up to 1024 << 20 (i.e. 1024 << (2*i) for i = -1..10, where
LuaJIT's lshift by -2 wraps to 0). KOReader uses this digest as the sync "document" id when
its match method is "binary" (the default), so it is how we tie synced positions to files.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

SAMPLE_SIZE = 1024
STEP = 1024


def partial_md5_offsets() -> Iterable[int]:
    yield 0
    for i in range(0, 11):
        yield STEP << (2 * i)


def partial_md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as f:
        for offset in partial_md5_offsets():
            f.seek(offset)
            sample = f.read(SAMPLE_SIZE)
            if not sample:
                break
            digest.update(sample)
    return digest.hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()
