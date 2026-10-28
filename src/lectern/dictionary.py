"""Offline dictionaries for looking up a word in the reader (issue #78, docs/dictionary.md).

The owner installs dictionaries on the server, once: an upload in Admin → Dictionaries or
``lectern dictionary-import`` for a file already on the machine. Lectern never downloads one
by itself, and a lookup reads only these local files: no request leaves the server while
reading, and lookups work unchanged in offline mode (:mod:`lectern.egress`).

Format: StarDict (``.ifo`` + ``.idx``/``.idx.gz`` + ``.dict``/``.dict.dz``, optionally
``.syn``), as a folder, a ``.zip``, or a ``.tar(.gz|.bz2|.xz)``. Import converts it once into
one SQLite file per dictionary, ``<data>/dictionaries/<id>/index.sqlite``:

- ``entries(id, headword, body)``: the definition as plain text (HTML, Pango, and XDXF
  markup become text with line breaks; resources and binary fields are dropped);
- ``keys(key, entry)``: the folded headword and every ``.syn`` synonym, indexed, so a lookup
  is a handful of index probes, never a scan.

Beside it, ``dictionary.json`` holds what Admin shows and edits (name, source and target
language, entry count, on/off, the ``.ifo`` copyright and description) and the licence files
that came with the dictionary (``LICENSE*``, ``COPYING*``, ``README*``) are kept next to it, so
the licence travels with the data. Nothing else knows about dictionaries: no database table,
no migration, no job (an import runs in the request or the CLI, once).

Matching (:func:`candidates`): case and accent folding, then simple lemma fallbacks (English
inflections, plurals and a few endings in French, Spanish, Portuguese, Italian, German); a
multi-word selection tries the phrase, then each word. A lookup is not logged anywhere.
"""

from __future__ import annotations

import contextlib
import gzip
import html
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import struct
import tarfile
import tempfile
import threading
import time
import unicodedata
import zipfile
import zlib
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path

from lectern.config import Settings

log = logging.getLogger("lectern.dictionary")

DIRNAME = "dictionaries"
INDEX = "index.sqlite"
META = "dictionary.json"
STAGING = ".staging"
LICENCE_FILES = re.compile(r"^(licen[cs]e|copying|copyright|readme|notice)", re.I)

MAX_UPLOAD_BYTES = 1024 * 2**20  # an uploaded archive
MAX_UNPACKED_BYTES = 4 * 1024 * 2**20  # what an archive may unpack to
MAX_ARCHIVE_MEMBERS = 2000
MAX_INDEX_BYTES = 128 * 2**20  # an .idx or .syn, decompressed (read into memory)
STALE_STAGING_S = 24 * 3600  # scratch left by an import that was killed
MAX_BODY_CHARS = 20_000  # one entry's text, stored
MAX_QUERY_CHARS = 100
MAX_WORDS = 6
MAX_DEFINITIONS = 12  # lines shown per entry
MAX_ENTRIES = 4  # entries per dictionary per word
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8})*$")

_import_lock = threading.Lock()


class DictionaryError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Dictionary:
    id: str
    name: str
    lang: str
    to: str
    entries: int
    size_bytes: int
    enabled: bool
    format: str = "stardict"
    author: str = ""
    description: str = ""
    copyright: str = ""
    website: str = ""
    licence_files: tuple[str, ...] = ()
    installed_at: str = ""

    @property
    def bilingual(self) -> bool:
        return self.lang != self.to

    def json(self) -> dict:
        d = asdict(self)
        d["licence_files"] = list(self.licence_files)
        d["bilingual"] = self.bilingual
        return d


def root(settings: Settings) -> Path:
    return settings.data_dir / DIRNAME


def primary(lang: str | None) -> str | None:
    """``pt-BR`` → ``pt``: dictionaries and books are matched on the primary language."""
    if not lang:
        return None
    lang = lang.strip()
    if not LANG_RE.match(lang):
        return None
    return re.split(r"[-_]", lang, maxsplit=1)[0].lower()


# --- the installed dictionaries ----------------------------------------------------------------


def _read(d: Path) -> Dictionary | None:
    try:
        meta = json.loads((d / META).read_text("utf-8"))
        if not (d / INDEX).is_file():
            return None
        meta["licence_files"] = tuple(meta.get("licence_files") or ())
        return Dictionary(**{k: v for k, v in meta.items() if k in Dictionary.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return None


def installed(settings: Settings) -> list[Dictionary]:
    base = root(settings)
    if not base.is_dir():
        return []
    found = [x for d in sorted(base.iterdir()) if d.is_dir() and (x := _read(d))]
    return sorted(found, key=lambda x: (x.lang, x.to, x.name.casefold()))


def get(settings: Settings, dict_id: str) -> Dictionary | None:
    if not re.fullmatch(r"[a-z0-9-]{1,80}", dict_id):
        return None
    return _read(root(settings) / dict_id)


def _write_meta(d: Path, meta: Dictionary) -> None:
    tmp = d / (META + ".tmp")
    tmp.write_text(json.dumps(meta.json(), ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, d / META)


def update(
    settings: Settings,
    dict_id: str,
    *,
    enabled: bool | None = None,
    lang: str | None = None,
    to: str | None = None,
) -> Dictionary:
    meta = get(settings, dict_id)
    if meta is None:
        raise DictionaryError("not_found", "No such dictionary.")
    if enabled is not None:
        meta.enabled = enabled
    if lang is not None:
        meta.lang = _lang_or_fail(lang)
    if to is not None:
        meta.to = _lang_or_fail(to)
    _write_meta(root(settings) / dict_id, meta)
    return meta


def remove(settings: Settings, dict_id: str) -> Dictionary:
    meta = get(settings, dict_id)
    if meta is None:
        raise DictionaryError("not_found", "No such dictionary.")
    d = root(settings) / dict_id
    # Renamed first, so a lookup never sees half a folder.
    gone = root(settings) / STAGING / f"removed-{dict_id}-{secrets.token_hex(4)}"
    gone.parent.mkdir(parents=True, exist_ok=True)
    os.replace(d, gone)
    shutil.rmtree(gone, ignore_errors=True)
    return meta


def _lang_or_fail(lang: str) -> str:
    p = primary(lang)
    if p is None:
        raise DictionaryError("invalid_language", "Use a language code such as en or pt-BR.")
    return p


# --- import ------------------------------------------------------------------------------------


def import_path(
    settings: Settings,
    source: Path,
    *,
    lang: str,
    to: str | None = None,
    name: str | None = None,
) -> Dictionary:
    """Install the StarDict dictionary in ``source`` (a folder, an ``.ifo``, or an archive)."""
    src_lang = _lang_or_fail(lang)
    dst_lang = _lang_or_fail(to) if to else src_lang
    if not _import_lock.acquire(blocking=False):
        raise DictionaryError("import_running", "Another dictionary is being imported.")
    try:
        base = root(settings)
        (base / STAGING).mkdir(parents=True, exist_ok=True)
        _sweep_staging(base / STAGING)
        work = Path(tempfile.mkdtemp(prefix="import-", dir=base / STAGING))
        try:
            folder = _unpack(source, work / "src")
            meta = _build(folder, work / "out", lang=src_lang, to=dst_lang, name=name)
            final = base / meta.id
            os.replace(work / "out", final)
            log.info(
                "dictionary installed: %s (%s→%s, %d entries)",
                meta.name,
                meta.lang,
                meta.to,
                meta.entries,
            )
            return meta
        finally:
            shutil.rmtree(work, ignore_errors=True)
    finally:
        _import_lock.release()


def _sweep_staging(staging: Path) -> None:
    """Remove scratch a killed import or upload left behind (older than a day, so a CLI import
    running beside the server keeps its own)."""
    cutoff = time.time() - STALE_STAGING_S
    for p in staging.iterdir():
        with contextlib.suppress(OSError):
            if p.lstat().st_mtime < cutoff:
                shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()


def _too_large(name: str) -> DictionaryError:
    return DictionaryError("dictionary_too_large", f"{name} unpacks too large.")


def _open(path: Path):
    return gzip.open(path, "rb") if path.name.endswith((".gz", ".dz")) else open(path, "rb")


def _read_capped(path: Path, cap: int) -> bytes:
    """A whole (possibly gzipped) file, refusing more than ``cap`` bytes once decompressed."""
    buf = bytearray()
    try:
        with _open(path) as f:
            while chunk := f.read(1 << 20):
                buf += chunk
                if len(buf) > cap:
                    raise _too_large(path.name)
    except (OSError, EOFError, zlib.error):
        raise DictionaryError("corrupt", f"{path.name} could not be read.") from None
    return bytes(buf)


def _copy_capped(src: Path, dest: Path, cap: int) -> int:
    total = 0
    try:
        with _open(src) as fin, open(dest, "wb") as fout:
            while chunk := fin.read(1 << 20):
                total += len(chunk)
                if total > cap:
                    raise _too_large(src.name)
                fout.write(chunk)
    except (OSError, EOFError, zlib.error):
        raise DictionaryError("corrupt", f"{src.name} could not be read.") from None
    return total


def _unpack(source: Path, dest: Path) -> Path:
    if source.is_dir():
        return source
    if source.suffix.lower() == ".ifo" and source.is_file():
        return source.parent
    if not source.is_file():
        raise DictionaryError("not_found", f"{source} does not exist.")
    dest.mkdir(parents=True)
    total = 0
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as z:
            members = [m for m in z.infolist() if not m.is_dir()]
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise DictionaryError("archive_too_large", "That archive has too many files.")
            for m in members:
                total += m.file_size
                if total > MAX_UNPACKED_BYTES:
                    raise DictionaryError("archive_too_large", "That archive unpacks too large.")
                target = _safe_target(dest, m.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as fin, open(target, "wb") as fout:
                    shutil.copyfileobj(fin, fout, 1 << 20)
        return dest
    try:
        tar = tarfile.open(source)
    except tarfile.TarError:
        raise DictionaryError(
            "unsupported_format",
            "Upload a StarDict dictionary as a .zip or .tar.gz with its .ifo, .idx, and .dict files.",
        ) from None
    with tar:
        n = 0
        for m in tar:
            if m.isdir():
                continue
            if not m.isfile():  # links, devices, fifos: never followed
                continue
            n += 1
            total += m.size
            if n > MAX_ARCHIVE_MEMBERS or total > MAX_UNPACKED_BYTES:
                raise DictionaryError("archive_too_large", "That archive unpacks too large.")
            target = _safe_target(dest, m.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            fin = tar.extractfile(m)
            if fin is None:
                continue
            with fin, open(target, "wb") as fout:
                shutil.copyfileobj(fin, fout, 1 << 20)
    return dest


def _safe_target(dest: Path, name: str) -> Path:
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or name.startswith(("/", "\\")):
        raise DictionaryError("unsafe_archive", "That archive has files outside its folder.")
    return dest.joinpath(*parts)


def _find(stem: Path, *suffixes: str) -> Path | None:
    for s in suffixes:
        p = stem.with_name(stem.name + s)
        if p.is_file():
            return p
    return None


def parse_ifo(path: Path) -> dict[str, str]:
    text = path.read_text("utf-8", errors="replace").lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or not lines[0].strip().startswith("StarDict's dict ifo file"):
        raise DictionaryError("unsupported_format", f"{path.name} is not a StarDict .ifo file.")
    out: dict[str, str] = {}
    for line in lines[1:]:
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _build(folder: Path, out: Path, *, lang: str, to: str, name: str | None) -> Dictionary:
    ifos = sorted(p for p in folder.rglob("*.ifo") if p.is_file())
    if not ifos:
        raise DictionaryError("no_ifo", "No StarDict .ifo file was found.")
    if len(ifos) > 1:
        raise DictionaryError("several_dictionaries", "Import one dictionary at a time.")
    ifo_path = ifos[0]
    ifo = parse_ifo(ifo_path)
    stem = ifo_path.with_suffix("")
    idx_path = _find(stem, ".idx", ".idx.gz")
    dict_path = _find(stem, ".dict", ".dict.dz")
    if idx_path is None or dict_path is None:
        raise DictionaryError("incomplete", "The .idx or .dict file next to the .ifo is missing.")
    syn_path = _find(stem, ".syn", ".syn.gz")
    offset_bits = 64 if ifo.get("idxoffsetbits") == "64" else 32
    types = ifo.get("sametypesequence", "")

    out.mkdir(parents=True)
    plain = out / "dict.tmp"
    dict_size = _copy_capped(dict_path, plain, MAX_UNPACKED_BYTES)
    idx_bytes = _read_capped(idx_path, MAX_INDEX_BYTES)

    db = sqlite3.connect(out / INDEX)
    try:
        db.executescript(
            """
            PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
            CREATE TABLE entries(id INTEGER PRIMARY KEY, headword TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE keys(key TEXT NOT NULL, entry INTEGER NOT NULL);
            """
        )
        count = 0
        with open(plain, "rb") as data:
            batch_e, batch_k = [], []
            for n, (word, offset, size) in enumerate(_idx_entries(idx_bytes, offset_bits)):
                if offset + size > dict_size:
                    raise DictionaryError(
                        "corrupt", f"{idx_path.name} points past the end of {dict_path.name}."
                    )
                data.seek(offset)
                body = _render(data.read(size), types)[:MAX_BODY_CHARS]
                batch_e.append((n, word, body))
                batch_k.append((fold(word), n))
                count += 1
                if len(batch_e) >= 5000:
                    db.executemany("INSERT INTO entries VALUES (?,?,?)", batch_e)
                    db.executemany("INSERT INTO keys VALUES (?,?)", batch_k)
                    batch_e, batch_k = [], []
            db.executemany("INSERT INTO entries VALUES (?,?,?)", batch_e)
            db.executemany("INSERT INTO keys VALUES (?,?)", batch_k)
        if count == 0:
            raise DictionaryError("empty", "That dictionary has no entries.")
        if syn_path is not None:
            syn = _read_capped(syn_path, MAX_INDEX_BYTES)
            db.executemany(
                "INSERT INTO keys VALUES (?,?)",
                ((fold(w), i) for w, i in _syn_entries(syn) if 0 <= i < count),
            )
        db.execute("CREATE INDEX keys_key ON keys(key)")
        db.commit()
        db.execute("VACUUM")
    except (struct.error, ValueError, MemoryError):  # truncated, or a word without its NUL
        raise DictionaryError("corrupt", f"{idx_path.name} is truncated or malformed.") from None
    finally:
        db.close()
    plain.unlink()

    licences = []
    for p in sorted(ifo_path.parent.iterdir()):
        if p.is_file() and LICENCE_FILES.match(p.name) and p.stat().st_size < 2**20:
            shutil.copyfile(p, out / p.name)
            licences.append(p.name)
    title = (name or ifo.get("bookname") or stem.name).strip()[:120] or stem.name
    slug = re.sub(
        r"[^a-z0-9]+",
        "-",
        unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower(),
    ).strip("-")[:48]
    meta = Dictionary(
        id=f"{slug or 'dictionary'}-{secrets.token_hex(3)}",
        name=title,
        lang=lang,
        to=to,
        entries=count,
        size_bytes=(out / INDEX).stat().st_size,
        enabled=True,
        author=_clean(ifo.get("author", "")),
        description=_clean(ifo.get("description", "")),
        copyright=_clean(ifo.get("copyright", "")),
        website=_clean(ifo.get("website", "")),
        licence_files=tuple(licences),
        installed_at=_now(),
    )
    _write_meta(out, meta)
    return meta


def _now() -> str:
    from lectern.models import utcnow

    return utcnow().isoformat()


def _clean(s: str) -> str:
    return _strip_markup(s.replace("<br>", "\n"))[:2000]


def _idx_entries(buf: bytes, offset_bits: int):
    fmt = ">QI" if offset_bits == 64 else ">II"
    width = struct.calcsize(fmt)
    i = 0
    while i < len(buf):
        end = buf.index(b"\0", i)
        word = buf[i:end].decode("utf-8", errors="replace")
        offset, size = struct.unpack_from(fmt, buf, end + 1)
        i = end + 1 + width
        yield word, offset, size


def _syn_entries(buf: bytes):
    i = 0
    while i < len(buf):
        end = buf.index(b"\0", i)
        (n,) = struct.unpack_from(">I", buf, end + 1)
        yield buf[i:end].decode("utf-8", errors="replace"), n
        i = end + 5


MARKUP = set("hgxkw")  # HTML, Pango, XDXF, KingSoft XML, MediaWiki (as text)
TEXT = set("mltyp") | MARKUP


def _render(data: bytes, types: str) -> str:
    """One entry's fields as plain text (StarDict's data formats)."""
    parts: list[str] = []
    i = 0
    seq = list(types)
    while i < len(data):
        if seq:
            t = seq.pop(0)
            last = not seq
        else:
            if types:
                break
            t = chr(data[i])
            i += 1
            last = False
        if t.isupper():  # binary: a size, then the data (or the rest)
            if last:
                break
            (size,) = struct.unpack_from(">I", data, i)
            i += 4 + size
            continue
        end = len(data) if last else data.find(b"\0", i)
        if end < 0:
            end = len(data)
        raw = data[i:end].decode("utf-8", errors="replace")
        i = end + 1
        if t not in TEXT:
            continue
        if t in MARKUP:
            raw = _strip_markup(raw)
        if t == "t":
            raw = f"/{raw.strip()}/" if raw.strip() else ""
        if raw.strip():
            parts.append(raw.strip())
    return "\n".join(parts)


class _Text(HTMLParser):
    BREAKS = {
        "br",
        "p",
        "div",
        "li",
        "tr",
        "dd",
        "dt",
        "h1",
        "h2",
        "h3",
        "h4",
        "blockquote",
        "k",
        "def",
        "ex",
    }
    SKIP = {"script", "style", "rref", "img"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += tag != "img"
        elif tag in self.BREAKS:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and tag != "img":
            self.skip = max(0, self.skip - 1)
        elif tag in self.BREAKS:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def _strip_markup(s: str) -> str:
    p = _Text()
    try:
        p.feed(s)
        p.close()
        text = "".join(p.out)
    except Exception:  # malformed markup: fall back to dropping anything tag-like
        text = html.unescape(re.sub(r"<[^>]*>", " ", s))
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


# --- matching ----------------------------------------------------------------------------------

_COMBINING = re.compile(r"[\u0300-\u036f]")
_EDGE = re.compile(r"^[\W_]+|[\W_]+$")
_WORD = re.compile(r"[\w][\w'’\-]*", re.UNICODE)
_ELISION = re.compile(
    r"^(?:l|d|j|m|n|s|t|c|qu|lorsqu|puisqu|jusqu|un|dell|all|dall|nell|sull)['’]", re.I
)
VOWELS = set("aeiouy")


def fold(s: str) -> str:
    """Case and accent folding (Latin, Greek, Cyrillic diacritics; other scripts unchanged)."""
    s = unicodedata.normalize("NFKD", s.replace("’", "'"))
    s = _COMBINING.sub("", s)
    s = unicodedata.normalize("NFC", s).casefold()
    return re.sub(r"\s+", " ", s).strip()


def clean_query(q: str) -> str:
    q = re.sub(r"\s+", " ", q or "").strip()[: MAX_QUERY_CHARS * 2]
    return _EDGE.sub("", q)[:MAX_QUERY_CHARS]


def lemmas(word: str, lang: str | None) -> list[str]:
    """Simple stemming fallbacks for a folded word, most likely first ("running" → "run")."""
    w = word
    out: list[str] = []
    add = out.append
    if _ELISION.match(w):  # l'homme → homme
        w = _ELISION.sub("", w)
        add(w)
    if w.endswith("'s"):
        w = w[:-2]
        add(w)
    elif w.endswith("s'"):
        add(w[:-1])
    lang = lang or "en"
    n = len(w)

    def undouble(b: str) -> None:
        if len(b) > 2 and b[-1] == b[-2] and b[-1] not in VOWELS and b[-1] not in "lsz":
            add(b[:-1])

    if lang == "en":
        if n > 4 and w.endswith("ies"):
            add(w[:-3] + "y")
        if n > 4 and w.endswith("ied"):
            add(w[:-3] + "y")
        if n > 4 and w.endswith("ying"):
            add(w[:-4] + "ie")
        if n > 4 and w.endswith("ing"):
            b = w[:-3]
            undouble(b)
            add(b)
            add(b + "e")
        if n > 3 and w.endswith("ed"):
            b = w[:-2]
            undouble(b)
            add(b)
            add(w[:-1])
        if n > 4 and w.endswith("ves"):
            add(w[:-3] + "f")
            add(w[:-3] + "fe")
        if n > 3 and w.endswith("men"):
            add(w[:-3] + "man")
        if n > 4 and (w.endswith("iest") or w.endswith("ier")):
            add(w[: -4 if w.endswith("iest") else -3] + "y")
        for suf in ("est", "er"):
            if n > len(suf) + 2 and w.endswith(suf):
                b = w[: -len(suf)]
                undouble(b)
                add(b)
                add(b + "e")
        if n > 4 and w.endswith("ily"):
            add(w[:-3] + "y")
        if n > 4 and w.endswith("ly"):
            add(w[:-2])
        if n > 3 and w.endswith("es") and w[-3] in "sxzh":
            add(w[:-2])
        if n > 2 and w.endswith("s") and not w.endswith("ss"):
            add(w[:-1])
    elif lang == "fr":
        if n > 4 and w.endswith("aux"):
            add(w[:-3] + "al")
        if n > 2 and w[-1] in "sx":
            w2 = w[:-1]
            add(w2)
        else:
            w2 = w
        for suf, rep in (
            ("euse", "eur"),
            ("ive", "if"),
            ("enne", "en"),
            ("elle", "el"),
            ("ee", "e"),
            ("e", ""),
        ):
            if len(w2) > len(suf) + 2 and w2.endswith(suf):
                add(w2[: -len(suf)] + rep)
                break
        for suf in ("ait", "aient", "ais", "ant", "ons", "ez", "ent", "e", "es", "a"):
            if len(w2) > len(suf) + 2 and w2.endswith(suf):
                add(w2[: -len(suf)] + "er")
    elif lang in ("es", "pt"):
        if n > 4 and w.endswith("ces"):
            add(w[:-3] + "z")
        if n > 4 and w.endswith("es"):
            add(w[:-2])
        if n > 3 and w.endswith("s"):
            add(w[:-1])
        if lang == "pt" and n > 4 and w.endswith("oes"):
            add(w[:-3] + "ao")
        base = w[:-1] if w.endswith("s") else w
        if len(base) > 3 and base.endswith("a"):
            add(base[:-1] + "o")
    elif lang == "it":
        if n > 3 and w[-1] in "ie":
            add(w[:-1] + "o")
            add(w[:-1] + "a")
            add(w[:-1] + "e")
    elif lang == "de":
        for suf in ("ern", "en", "er", "es", "e", "n", "s"):
            if n > len(suf) + 2 and w.endswith(suf):
                add(w[: -len(suf)])
    elif n > 3 and w.endswith("s"):
        add(w[:-1])
    return [x for x in dict.fromkeys(out) if len(x) >= 2 and x != word]


def candidates(q: str, lang: str | None) -> list[tuple[str, list[str]]]:
    """``[(shown word, [keys to try in order])]``: the phrase first, then each of its words."""
    q = clean_query(q)
    if not q:
        return []
    words = [_EDGE.sub("", w) for w in _WORD.findall(q)][:MAX_WORDS]
    words = [w for w in words if w]
    groups: list[tuple[str, list[str]]] = []
    if len(words) != 1:
        groups.append((q, [fold(q)]))
    for w in words:
        f = fold(w)
        groups.append((w, [f, *lemmas(f, lang)]))
    return groups


# --- lookup ------------------------------------------------------------------------------------

_POS = {
    "n": "noun", "noun": "noun", "nm": "noun", "nf": "noun", "s": "noun", "sm": "noun", "sf": "noun",
    "v": "verb", "verb": "verb", "vt": "verb", "vi": "verb", "vb": "verb",
    "adj": "adjective", "adjective": "adjective", "a": "adjective",
    "adv": "adverb", "adverb": "adverb",
    "pron": "pronoun", "pronoun": "pronoun",
    "prep": "preposition", "preposition": "preposition",
    "conj": "conjunction", "conjunction": "conjunction",
    "interj": "interjection", "interjection": "interjection",
    "art": "article", "article": "article", "det": "determiner", "determiner": "determiner",
    "abbr": "abbreviation", "abbreviation": "abbreviation", "num": "numeral", "numeral": "numeral",
}  # fmt: skip
_POS_RE = re.compile(r"^\s*[\(\[]?\s*([a-z]{1,12})\.?\s*[\)\]]?(?=[\s,.;:]|$)", re.I)
_PRON_RE = re.compile(r"^\s*(/[^/\n]{1,60}/|\[[^\]\n]{1,60}\])\s*")


def _shape(headword: str, body: str) -> dict:
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if lines and fold(lines[0]) == fold(headword):  # many dictionaries repeat the headword
        lines = lines[1:]
    pron = None
    if lines and (m := _PRON_RE.match(lines[0])):
        pron = m.group(1)
        lines[0] = lines[0][m.end() :].strip()
        if not lines[0]:
            lines = lines[1:]
    pos = None
    if lines and (m := _POS_RE.match(lines[0])) and m.group(1).lower() in _POS:
        word = m.group(1).lower()
        # a one-letter tag only counts with its full stop or brackets ("n." or "(v)")
        if len(word) > 1 or re.match(r"^\s*([\(\[]\s*[a-z]|[a-z]\.)", lines[0], re.I):
            pos = _POS[word]
            lines[0] = lines[0][m.end() :].lstrip(" .,;:)]")
            if not lines[0]:
                lines = lines[1:]
    more = max(0, len(lines) - MAX_DEFINITIONS)
    return {
        "headword": headword,
        "pos": pos,
        "pronunciation": pron,
        "definitions": [ln[:600] for ln in lines[:MAX_DEFINITIONS]],
        "more": more,
    }


def _probe(
    settings: Settings, d: Dictionary, keys: list[str]
) -> tuple[str, list[tuple[str, str]]] | None:
    path = root(settings) / d.id / INDEX
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return None
    try:
        for key in keys:
            rows = db.execute(
                "SELECT DISTINCT e.headword, e.body FROM keys k JOIN entries e ON e.id = k.entry "
                "WHERE k.key = ? ORDER BY e.id LIMIT ?",
                (key, MAX_ENTRIES),
            ).fetchall()
            if rows:
                return key, rows
    except sqlite3.Error:
        log.warning("dictionary %s could not be read", d.id)
    finally:
        db.close()
    return None


def lookup(settings: Settings, q: str, lang: str | None = None, to: str | None = None) -> dict:
    """Definitions (monolingual dictionaries in the book's language) and translations
    (bilingual ones from it into ``to``), phrase first, then word by word."""
    src, dst = primary(lang), primary(to)
    every = [d for d in installed(settings) if d.enabled]
    same = [d for d in every if src is None or d.lang == src]
    mono = [d for d in same if not d.bilingual]
    bi = [d for d in same if d.bilingual and (dst is None or d.to == dst) and d.to != src]
    groups = candidates(q, src)
    result: dict = {
        "query": clean_query(q),
        "lang": src,
        "to": dst,
        "installed": bool(every),  # any dictionary turned on
        "dictionaries": len(mono) + len(bi),
        "languages": sorted({d.lang for d in every}),
        "targets": sorted({d.to for d in same if d.bilingual}),
        "entries": [],
        "translations": [],
    }
    if not groups or not (mono or bi):
        return result
    phrase = len(groups) > 1  # candidates() put the whole selection first
    for i, (shown, keys) in enumerate(groups):
        found = False
        for kind, dicts in (("entries", mono), ("translations", bi)):
            for d in dicts:
                hit = _probe(settings, d, keys)
                if hit is None:
                    continue
                key, rows = hit
                found = True
                for headword, body in rows:
                    result[kind].append(
                        {
                            **_shape(headword, body),
                            "dictionary": {"id": d.id, "name": d.name, "lang": d.lang, "to": d.to},
                            "query": shown,
                            "lemma": key != keys[0],
                        }
                    )
        if phrase and i == 0 and found:
            break  # the phrase is in a dictionary: its words are not looked up on their own
    return result


@contextlib.contextmanager
def staging_file(settings: Settings):
    """A scratch file for an upload, on the same disk as the dictionaries."""
    base = root(settings) / STAGING
    base.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="upload-", dir=base)
    os.close(fd)
    try:
        yield Path(name)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(name)
