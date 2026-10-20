"""Single-range HTTP file responses (RFC 9110), as specified for audio streaming.

- no ``Range`` (or one we do not support): ``200`` with the whole body
- one satisfiable ``bytes=a-b`` / ``a-`` / ``-n``: ``206`` with ``Content-Range``
- a start beyond the end, or ``-0``: ``416`` with ``Content-Range: bytes */size``
- several ranges, or a malformed header: ignored, so ``200`` (allowed by RFC 9110 14.2)
- ``If-None-Match`` matching the ETag: ``304``; ``If-Range`` not matching: ``200``
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

# At most 19 digits (fits in 64 bits): longer numbers do not match and the header is ignored.
_RANGE = re.compile(r"^bytes=(\d{0,19})-(\d{0,19})$")
CHUNK = 64 * 1024


class Unsatisfiable(Exception):
    pass


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Inclusive (start, end) for a single byte range, None to serve everything.

    Raises :class:`Unsatisfiable` for a well-formed range that selects nothing.
    """
    if not header:
        return None
    m = _RANGE.match(header.strip().replace(" ", ""))
    if not m:
        return None  # multiple ranges or junk: ignore Range
    first, last = m.group(1), m.group(2)
    if first == "" and last == "":
        return None
    if first == "":  # suffix: the last n bytes
        n = int(last)
        if n == 0 or size == 0:
            raise Unsatisfiable
        return max(0, size - n), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if last and end < start:
        return None  # invalid range-spec: ignore
    if start >= size:
        raise Unsatisfiable
    return start, min(end, size - 1)


def _iter_file(path: Path, start: int, length: int) -> Iterator[bytes]:
    with path.open("rb") as fh:
        fh.seek(start)
        remaining = length
        while remaining > 0:
            block = fh.read(min(CHUNK, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block


def _etag_matches(header: str, etag: str) -> bool:
    tags = [t.strip() for t in header.split(",")]
    bare = etag.removeprefix("W/")
    return "*" in tags or any(t.removeprefix("W/") == bare for t in tags)


def file_response(
    request: Request,
    path: Path,
    media_type: str,
    etag_value: str,
    *,
    cache_control: str = "private, max-age=3600",
    headers: dict[str, str] | None = None,
) -> Response:
    st = os.stat(path)
    size = st.st_size
    etag = f'"{etag_value}"'
    last_modified = formatdate(st.st_mtime, usegmt=True)
    base = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Last-Modified": last_modified,
        "Cache-Control": cache_control,
        **(headers or {}),
    }
    inm = request.headers.get("if-none-match")
    if inm and _etag_matches(inm, etag):
        return Response(status_code=304, headers=base)

    range_header = request.headers.get("range")
    if_range = request.headers.get("if-range")
    if range_header and if_range:
        if if_range.startswith('"') or if_range.startswith("W/"):
            if if_range != etag:  # strong comparison only
                range_header = None
        else:
            try:
                if int(parsedate_to_datetime(if_range).timestamp()) < int(st.st_mtime):
                    range_header = None
            except (TypeError, ValueError):
                range_header = None

    try:
        rng = parse_range(range_header, size)
    except Unsatisfiable:
        return Response(status_code=416, headers={**base, "Content-Range": f"bytes */{size}"})
    if rng is None:
        return StreamingResponse(
            _iter_file(path, 0, size),
            status_code=200,
            media_type=media_type,
            headers={**base, "Content-Length": str(size)},
        )
    start, end = rng
    length = end - start + 1
    return StreamingResponse(
        _iter_file(path, start, length),
        status_code=206,
        media_type=media_type,
        headers={
            **base,
            "Content-Length": str(length),
            "Content-Range": f"bytes {start}-{end}/{size}",
        },
    )
