"""Benchmark full-text search on a synthetic library (docs/search.md#raspberry-pi).

    uv run python scripts/search_bench.py <books> <chars per book> <empty dir>

Writes ``<books>`` EPUBs of ``<chars per book>`` characters (sentences of a Zipf-distributed
vocabulary, with a rare phrase in a few books) under ``<dir>/library``, indexes them with the
real indexer (``lectern.search.index_work``: EPUB reading, chunking, compression, FTS5), and
times searches as an owner (every book visible) and as a reader (40 books visible). Prints the
build rate, the size of search.db against the plain text, and the median of five runs per
query. Run it again with the same directory to time the queries only.
"""

from __future__ import annotations

import json
import random
import sqlite3
import statistics
import sys
import time
import zipfile
from pathlib import Path

from lectern import search
from lectern.config import Settings

RARE = "The lighthouse keeper whistled a quiet tune."


def vocabulary(rng: random.Random) -> list[str]:
    common = (
        "the of and to a in that he was it his i with as had for you be at on not is her by "
        "but she this from they all him which so were my have one or me an there no their"
    ).split()
    syllables = "ka lo mi ra ten sor vel an dor fi gul pe quin ust bri cha ell mor nat tis".split()
    made = {"".join(rng.choice(syllables) for _ in range(rng.randint(2, 4))) for _ in range(80_000)}
    return common + sorted(made - set(common))


def write_epub(path: Path, book: int, chars: int, sentences: list[str], rng: random.Random) -> None:
    opf_items, spine, chapters, total = [], [], [], 0
    while total < chars:
        text = " ".join(rng.choice(sentences) for _ in range(120))
        if len(chapters) == 3 and book % 500 == 7:
            text += " " + RARE
        chapters.append(text)
        total += len(text)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument'
            ':xmlns:container"><rootfiles><rootfile full-path="content.opf" media-type='
            '"application/oebps-package+xml"/></rootfiles></container>',
        )
        for i, text in enumerate(chapters):
            zf.writestr(f"c{i}.xhtml", f"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>{text}</p></body></html>")  # fmt: skip
            opf_items.append(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>')  # fmt: skip
            spine.append(f'<itemref idref="c{i}"/>')
        zf.writestr(
            "content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            f"<metadata/><manifest>{''.join(opf_items)}</manifest><spine>{''.join(spine)}</spine>"
            "</package>",
        )


def build(settings: Settings, books: int, chars: int) -> None:
    rng = random.Random(7)  # noqa: S311 - a reproducible benchmark, not a secret
    vocab = vocabulary(rng)
    weights = [1 / (rank + 1) ** 1.07 for rank in range(len(vocab))]
    sentences = [
        " ".join(rng.choices(vocab, weights=weights, k=rng.randint(8, 24))).capitalize() + "."
        for _ in range(20_000)
    ]
    settings.library_dir.mkdir(parents=True)
    con = search.connect()
    started, text_chars = time.monotonic(), 0
    for book in range(1, books + 1):
        folder = settings.library_dir / f"b{book}"
        folder.mkdir()
        write_epub(folder / "book.epub", book, chars, sentences, rng)
        want = search.Wanted(
            f"k{book}", f"b{book}", (search.Source(book, search.EPUB_KIND, "0" * 64, "book.epub"),)
        )
        text_chars += search.index_work(con, settings, book, want)["chars"]
        if book % 250 == 0:
            rate = text_chars / 1e6 / (time.monotonic() - started)
            print(f"{book} books, {text_chars / 1e6:.0f}M characters, {rate:.2f}M characters/s")
    con.close()
    print(f"built in {time.monotonic() - started:.0f} s; text {text_chars / 1e6:.0f} MB")


def timed(q: str, visible: list[int], budget_ms: int) -> tuple[float, int, bool]:
    con = search.connect()
    try:
        deadline = time.monotonic() + budget_ms / 1000
        con.set_progress_handler(lambda: int(time.monotonic() > deadline), 100)
        started, hits, broad = time.monotonic(), 0, False
        for fq in search.fts_queries(search.query_terms(q), prefix=True):
            try:
                hits += len(con.execute(search._RANKED, (fq, json.dumps(visible), 3, 8)).fetchall())
            except sqlite3.OperationalError:
                broad = True
                break
            if hits >= 8:
                break
        return (time.monotonic() - started) * 1000, hits, broad
    finally:
        con.close()


def main(books: int, chars: int, base: Path) -> None:
    settings = Settings(data_dir=base / "data", library_dir=base / "library")
    fresh = not (settings.data_dir / "search.db").exists()
    search.init(settings)
    if fresh:
        build(settings, books, chars)
    size = sum(f.stat().st_size for f in settings.data_dir.glob("search.db*"))
    print(f"search.db {size / 1e6:.0f} MB")
    vocab = vocabulary(random.Random(7))  # noqa: S311 - the same vocabulary as build()
    queries = ["lighthouse keeper", "the lighthouse keeper", "quiet tune whistled",
               vocab[2000], vocab[10_000], f"{vocab[60]} {vocab[61]}", "the of and", "the"]  # fmt: skip
    for who, visible in (("owner", list(range(1, books + 1))), ("reader", list(range(1, 41)))):
        for q in queries:
            runs = [timed(q, visible, 2000) for _ in range(5)]
            ms = statistics.median(r[0] for r in runs)
            print(f"{who:6} {q!r:30} {ms:8.1f} ms  hits {runs[0][1]:2}  too broad {runs[0][2]}")


if __name__ == "__main__":
    main(int(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3]))
