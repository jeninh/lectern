"""The script a TTS job reads: the work's EPUB as chapters of numbered sentences.

Sentences are exactly the ones alignment would produce (:func:`lectern.align.text.segment_items`
over the same spine walk), so the sentence map a generated audiobook installs uses the same
coordinates as everything else and every stored reading position keeps its place. A chapter
is a spine item that has something to say; its title comes from the EPUB's table of contents
(EPUB 3 nav or EPUB 2 NCX), else from a short first line, else "Chapter N".

``pause_s`` is the silence after a sentence: longer at the end of a paragraph and after a
heading-like line. It belongs to no sentence, so highlighting stays on the words.

Wire format (docs/contracts.md, "Generated audiobooks: the tts kind")::

    {"version": 1, "chapters": [{"index": 0, "title": "…",
      "sentences": [{"n": 0, "text": "…", "pause_s": 0.3}]}]}
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from lectern.align.text import SentenceSpan, iter_spine_items, segment_items
from lectern.ingest.epub import NS, EpubError, _read_opf

SCRIPT_VERSION = 1
PAUSE_SENTENCE_S = 0.3
PAUSE_PARAGRAPH_S = 0.7
PAUSE_HEADING_S = 1.0
TITLE_MAX_CHARS = 120
_SPEAKABLE = re.compile(r"[^\W_]", re.UNICODE)


@dataclass(frozen=True)
class ScriptSentence:
    n: int
    text: str
    pause_s: float
    spine_index: int
    char_start: int
    char_end: int


@dataclass
class Chapter:
    spine_index: int
    title: str
    sentences: list[ScriptSentence] = field(default_factory=list)

    @property
    def characters(self) -> int:
        """Characters a provider is sent for this chapter: the sentences joined by single
        spaces (an upper bound: a request boundary drops one space)."""
        return sum(len(s.text) for s in self.sentences) + max(0, len(self.sentences) - 1)


@dataclass
class Book:
    """Every chapter the EPUB offers plus the full sentence list (for the sentence map)."""

    chapters: list[Chapter]
    sentences: list[SentenceSpan]


def read_book(epub: Path) -> Book:
    titles = toc_titles(epub)
    by_spine: dict[int, list[SentenceSpan]] = {}
    items = list(iter_spine_items(epub))
    sentences = segment_items(items)
    for s in sentences:
        by_spine.setdefault(s.spine_index, []).append(s)
    chapters: list[Chapter] = []
    for item in items:
        spans = [s for s in by_spine.get(item.index, []) if _SPEAKABLE.search(s.text)]
        if not spans:
            continue
        chapter = Chapter(item.index, "")
        for i, s in enumerate(spans):
            nxt = spans[i + 1] if i + 1 < len(spans) else None
            same_block = nxt is not None and item.text.block_at(s.char_start) == item.text.block_at(
                nxt.char_start
            )
            single_line_block = (
                not same_block
                and (i == 0 or item.text.block_at(spans[i - 1].char_start)
                     != item.text.block_at(s.char_start))
                and len(s.text) <= TITLE_MAX_CHARS
                and not s.text.rstrip().endswith((".", "!", "?", "…", '"', "”"))
            )  # fmt: skip
            pause = (
                PAUSE_HEADING_S
                if single_line_block
                else PAUSE_SENTENCE_S
                if same_block
                else PAUSE_PARAGRAPH_S
            )
            chapter.sentences.append(
                ScriptSentence(s.n, s.text, pause, s.spine_index, s.char_start, s.char_end)
            )
        first = chapter.sentences[0].text
        chapter.title = (
            _title_at_start(epub, item.index, titles.get(item.href, []))
            or (first if len(first) <= TITLE_MAX_CHARS // 2 else "")
            or f"Chapter {len(chapters) + 1}"
        )[:TITLE_MAX_CHARS]
        chapters.append(chapter)
    return Book(chapters, sentences)


def select(book: Book, spine_indices: list[int] | None) -> list[Chapter]:
    """The chapters to narrate, in reading order (``None`` = all)."""
    if spine_indices is None:
        return list(book.chapters)
    wanted = set(spine_indices)
    return [c for c in book.chapters if c.spine_index in wanted]


def script_json(chapters: list[Chapter]) -> bytes:
    """The ``script`` input: canonical JSON, so its SHA-256 identifies the job's text."""
    doc = {
        "version": SCRIPT_VERSION,
        "chapters": [
            {
                "index": i,
                "title": c.title,
                "sentences": [
                    {"n": s.n, "text": s.text, "pause_s": s.pause_s} for s in c.sentences
                ],
            }
            for i, c in enumerate(chapters)
        ],
    }
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def script_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_script(data: bytes) -> dict:
    doc = json.loads(data)
    if doc.get("version") != SCRIPT_VERSION:
        raise ValueError("unsupported script version")
    return doc


# --- table of contents -------------------------------------------------------------------------


# A TOC entry names a chapter only if its anchor is at the start of the spine item: at most
# this much speakable text before it. (Gutenberg books point the licence entry at the end of
# the file the story is in; that title must not name the story.)
TITLE_ANCHOR_SLACK_CHARS = 200


def _title_at_start(epub: Path, spine_index: int, entries: list[tuple[str, str]]) -> str:
    from lectern.align.text import read_spine_item

    raw = None
    for fragment, title in entries:
        if not fragment:
            return title
        if raw is None:
            raw = read_spine_item(epub, spine_index)
            try:
                root = etree.fromstring(
                    raw, etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
                )
            except etree.XMLSyntaxError:
                root = None
        if root is None:
            continue
        found = root.xpath("//*[@id=$i]", i=fragment)
        if not found:
            continue
        before = "".join(found[0].xpath("preceding::text()[not(ancestor::*[local-name()='head'])]"))
        if len(_SPEAKABLE.findall(before)) <= TITLE_ANCHOR_SLACK_CHARS:
            return title
    return ""


def toc_titles(epub: Path) -> dict[str, list[tuple[str, str]]]:
    """``{zip member name: [(fragment, title), …]}`` in TOC order, from the nav document or
    NCX. An unreadable TOC is no TOC (titles then fall back)."""
    try:
        with zipfile.ZipFile(epub) as zf:
            opf, opf_path = _read_opf(zf)
            opf_dir = posixpath.dirname(opf_path)
            manifest = opf.find("opf:manifest", NS)
            if manifest is None:
                return {}
            nav = ncx = None
            for it in manifest.findall("opf:item", NS):
                props = (it.get("properties") or "").split()
                if "nav" in props:
                    nav = it.get("href")
                elif it.get("media-type") == "application/x-dtbncx+xml":
                    ncx = it.get("href")
            for href, parse in ((nav, _nav_entries), (ncx, _ncx_entries)):
                if not href:
                    continue
                name = posixpath.normpath(posixpath.join(opf_dir, href))
                try:
                    root = etree.fromstring(
                        zf.read(name),
                        etree.XMLParser(recover=True, resolve_entities=False, no_network=True),
                    )
                except (KeyError, etree.XMLSyntaxError):
                    continue
                if root is None:
                    continue
                out: dict[str, list[tuple[str, str]]] = {}
                base = posixpath.dirname(name)
                for target, title in parse(root):
                    target, _, fragment = target.partition("#")
                    title = " ".join(title.split())
                    if not target or not title:
                        continue
                    member = posixpath.normpath(posixpath.join(base, target))
                    out.setdefault(member, []).append((fragment, title[:TITLE_MAX_CHARS]))
                if out:
                    return out
    except (EpubError, zipfile.BadZipFile, OSError, KeyError):
        return {}
    return {}


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _nav_entries(root):
    for nav in root.iter():
        if _local(nav.tag) != "nav":
            continue
        kind = next((v for k, v in nav.attrib.items() if k.endswith("type")), "")
        if "toc" not in kind.split():
            continue
        for a in nav.iter():
            if _local(a.tag) == "a" and a.get("href"):
                yield a.get("href"), "".join(a.itertext())
        return


def _ncx_entries(root):
    for point in root.iter():
        if _local(point.tag) != "navPoint":
            continue
        label = content = None
        for child in point:
            if _local(child.tag) == "navLabel":
                label = "".join(child.itertext())
            elif _local(child.tag) == "content":
                content = child.get("src")
        if content and label:
            yield content, label
