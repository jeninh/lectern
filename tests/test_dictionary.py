"""Offline dictionaries for word lookup in the reader (issue #78): import, index, lookup,
folding and lemma fallbacks, who may do what, and that a lookup never leaves the machine."""

from __future__ import annotations

import gzip
import io
import socket
import sqlite3
import struct
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from tests.conftest import ORIGIN, api_delete, login, make_user

from lectern import dictionary, egress


def stardict(
    entries: list[tuple[str, str]],
    *,
    name: str = "Test Dictionary",
    types: str | None = "m",
    syn: dict[str, str] | None = None,
    dz: bool = True,
    extra: dict[str, str] | None = None,
) -> dict[str, bytes]:
    """A StarDict dictionary's files (entries sorted the way StarDict sorts them)."""
    entries = sorted(entries, key=lambda e: (e[0].casefold(), e[0]))
    data, idx = b"", b""
    for word, body in entries:
        payload = body.encode() if types else b"m" + body.encode() + b"\0"
        idx += word.encode() + b"\0" + struct.pack(">II", len(data), len(payload))
        data += payload
    ifo = ["StarDict's dict ifo file", "version=2.4.2", f"bookname={name}"]
    ifo += [f"wordcount={len(entries)}", f"idxfilesize={len(idx)}"]
    if types:
        ifo.append(f"sametypesequence={types}")
    ifo += ["copyright=CC BY-SA 4.0", "description=A <b>tiny</b> test dictionary"]
    files = {"t.ifo": ("\n".join(ifo) + "\n").encode(), "t.idx": idx}
    files["t.dict.dz" if dz else "t.dict"] = gzip.compress(data) if dz else data
    if syn:
        index = {w: i for i, (w, _) in enumerate(entries)}
        files["t.syn"] = b"".join(
            s.encode() + b"\0" + struct.pack(">I", index[w]) for s, w in sorted(syn.items())
        )
        ifo.append(f"synwordcount={len(syn)}")
        files["t.ifo"] = ("\n".join(ifo) + "\n").encode()
    files.update({k: v.encode() for k, v in (extra or {}).items()})
    return files


def zipped(files: dict[str, bytes], folder: str = "dict/") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, b in files.items():
            z.writestr(folder + n, b)
    return buf.getvalue()


ENGLISH = [
    ("run", "v. To move swiftly on foot.\nTo operate or function."),
    ("fly", "n. A small winged insect.\nv. To travel through the air."),
    ("café", "n. A small restaurant selling light meals."),
    ("ice cream", "n. A frozen dessert."),
    ("ice", "n. Frozen water."),
    ("apple", "n. The round fruit of a tree."),
    ("red", "adj. Of the colour of blood."),
    ("colour", "/ˈkʌlə/ noun The property of reflecting light of a wavelength."),
    ("wolf", "n. A wild carnivorous mammal."),
]


def install(client, csrf, files, *, lang="en", to=None, name=None, archive="zip"):
    if archive == "zip":
        body = zipped(files)
    else:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for n, b in files.items():
                info = tarfile.TarInfo(f"d/{n}")
                info.size = len(b)
                t.addfile(info, io.BytesIO(b))
        body = buf.getvalue()
    params = {"lang": lang, **({"to": to} if to else {}), **({"name": name} if name else {})}
    return client.post(
        "/api/admin/dictionaries",
        params=params,
        content=body,
        headers={
            "origin": ORIGIN,
            "x-csrf-token": csrf,
            "content-type": "application/octet-stream",
        },
    )


def look(client, q, lang="en", to=None):
    params = {"q": q, **({"lang": lang} if lang else {}), **({"to": to} if to else {})}
    r = client.get("/api/dictionary/lookup", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def heads(result, kind="entries"):
    return [e["headword"] for e in result[kind]]


@pytest.fixture
def english(client, owner):
    files = stardict(
        ENGLISH,
        syn={"colours": "colour", "Ran": "run"},
        extra={"LICENSE.txt": "CC BY-SA 4.0 — attribution required"},
    )
    r = install(client, owner["csrf"], files, name="Tiny English")
    assert r.status_code == 201, r.text
    return r.json()["dictionary"]


def test_import_lists_language_size_entries_and_keeps_the_licence(client, owner, english, settings):
    listed = client.get("/api/admin/dictionaries").json()["dictionaries"]
    assert [d["name"] for d in listed] == ["Tiny English"]
    d = listed[0]
    assert (d["lang"], d["to"], d["bilingual"], d["enabled"]) == ("en", "en", False, True)
    assert d["entries"] == len(ENGLISH)
    assert d["size_bytes"] > 0
    assert d["copyright"] == "CC BY-SA 4.0"
    assert d["description"] == "A tiny test dictionary"
    folder = settings.data_dir / "dictionaries" / d["id"]
    assert d["licence_files"] == ["LICENSE.txt"]
    assert (folder / "LICENSE.txt").read_text().startswith("CC BY-SA")
    # Nothing but the index, its description, and the licence: no staging left behind.
    assert sorted(p.name for p in folder.iterdir()) == [
        "LICENSE.txt",
        "dictionary.json",
        "index.sqlite",
    ]
    assert list((settings.data_dir / "dictionaries" / ".staging").iterdir()) == []


def test_lookups_use_the_index_not_a_scan(english, settings):
    db = sqlite3.connect(settings.data_dir / "dictionaries" / english["id"] / "index.sqlite")
    plan = " ".join(
        str(row)
        for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT e.headword FROM keys k JOIN entries e ON e.id = k.entry "
            "WHERE k.key = ?",
            ("run",),
        )
    )
    assert "keys_key" in plan and "SCAN k" not in plan, plan


def test_lookup_shapes_an_entry(client, english):
    r = look(client, "fly")
    assert r["installed"] and r["dictionaries"] == 1
    [e] = r["entries"]
    assert e["headword"] == "fly"
    assert e["pos"] == "noun"
    assert e["definitions"] == ["A small winged insect.", "v. To travel through the air."]
    assert e["dictionary"]["name"] == "Tiny English"
    assert e["lemma"] is False
    colour = look(client, "colour")["entries"][0]
    assert (colour["pronunciation"], colour["pos"]) == ("/ˈkʌlə/", "noun")


@pytest.mark.parametrize(
    ("q", "want"),
    [
        ("Café", "café"),  # case
        ("CAFE", "café"),  # accents folded both ways
        ("running", "run"),  # doubled consonant + ing
        ("runs", "run"),
        ("flies", "fly"),  # ies → y
        ("wolves", "wolf"),  # ves → f
        ("apples.", "apple"),  # trailing punctuation
        ("“Red”", "red"),
        ("colours", "colour"),  # a .syn synonym
        ("ran", "run"),  # irregular, through .syn
    ],
)
def test_folding_and_lemma_fallbacks(client, english, q, want):
    assert heads(look(client, q))[:1] == [want]


def test_a_lemma_is_marked_and_the_exact_word_wins(client, english):
    assert look(client, "running")["entries"][0]["lemma"] is True
    assert look(client, "ice")["entries"][0]["lemma"] is False


def test_phrase_first_then_its_words(client, english):
    assert heads(look(client, "ice cream")) == ["ice cream"]  # the phrase, not "ice"
    both = look(client, "red apples")
    assert heads(both) == ["red", "apple"]
    assert [e["query"] for e in both["entries"]] == ["red", "apples"]
    assert look(client, "zzz qqq")["entries"] == []


def test_book_language_picks_the_dictionaries(client, english):
    assert look(client, "run", lang="en-GB")["entries"]
    fr = look(client, "run", lang="fr")
    assert fr["entries"] == [] and fr["dictionaries"] == 0 and fr["installed"] is True
    assert fr["languages"] == ["en"]
    assert look(client, "run", lang=None)["entries"]  # no language: every dictionary


def test_bilingual_dictionaries_translate_into_the_chosen_language(client, owner, english):
    files = stardict([("run", "courir"), ("apple", "pomme")], name="English–French")
    r = install(client, owner["csrf"], files, lang="en", to="fr", archive="tar")
    assert r.status_code == 201, r.text
    assert r.json()["dictionary"]["bilingual"] is True
    r = look(client, "running", to="fr")
    assert heads(r) == ["run"]
    assert [t["definitions"] for t in r["translations"]] == [["courir"]]
    assert r["targets"] == ["fr"]
    assert look(client, "run", to="de")["translations"] == []


def test_markup_becomes_text(client, owner):
    html_dict = stardict(
        [
            (
                "serendipity",
                "<b>n.</b> luck<br>finding <i>good</i> things&nbsp;by chance<script>x()</script>",
            )
        ],
        types="h",
    )
    assert install(client, owner["csrf"], html_dict).status_code == 201
    [e] = look(client, "serendipity")["entries"]
    assert e["pos"] == "noun"
    assert e["definitions"] == ["luck", "finding good things by chance"]
    untyped = stardict([("tea", "n. A hot drink.")], types=None, dz=False, name="Untyped")
    assert install(client, owner["csrf"], untyped).status_code == 201
    assert look(client, "tea")["entries"][0]["definitions"] == ["A hot drink."]


def test_disable_and_remove(client, owner, english, settings):
    url = f"/api/admin/dictionaries/{english['id']}"
    r = client.patch(
        url, json={"enabled": False}, headers={"origin": ORIGIN, "x-csrf-token": owner["csrf"]}
    )
    assert r.status_code == 200 and r.json()["dictionary"]["enabled"] is False
    off = look(client, "run")
    assert off["entries"] == [] and off["installed"] is False
    r = client.patch(
        url,
        json={"enabled": True, "lang": "en-US"},
        headers={"origin": ORIGIN, "x-csrf-token": owner["csrf"]},
    )
    assert r.json()["dictionary"]["lang"] == "en"
    assert look(client, "run")["entries"]
    assert api_delete(client, url, owner["csrf"]).status_code == 200
    assert client.get("/api/admin/dictionaries").json()["dictionaries"] == []
    assert not (settings.data_dir / "dictionaries" / english["id"]).exists()
    assert api_delete(client, url, owner["csrf"]).status_code == 404


def test_without_dictionaries_the_reader_is_told(client, owner):
    r = look(client, "anything")
    assert r == {**r, "installed": False, "entries": [], "translations": [], "languages": []}


@pytest.mark.parametrize(
    ("files", "code"),
    [
        ({"readme.txt": b"no dictionary here"}, "no_ifo"),
        ({"t.ifo": b"StarDict's dict ifo file\nbookname=x\n"}, "incomplete"),
        ({"t.ifo": b"not an ifo"}, "unsupported_format"),
    ],
)
def test_bad_archives_are_refused(client, owner, files, code):
    r = install(client, owner["csrf"], files)
    assert r.status_code == 422 and r.json()["detail"] == code, r.text


def _with(files: dict[str, bytes], **changes: bytes) -> dict[str, bytes]:
    out = {k: v for k, v in files.items() if k not in changes}
    out.update(changes)
    return out


@pytest.mark.parametrize(
    "broken",
    [
        {"t.idx": b"run-without-a-nul"},  # a headword with no terminator
        {"t.idx": b"run\0\0\0"},  # truncated offsets
        {"t.idx": b"run\0" + struct.pack(">II", 0, 0xFFFFFFFF)},  # past the end of .dict
        {"t.idx.gz": b"not gzip at all"},
        {"t.dict.dz": gzip.compress(b"x" * 100)[:-12]},  # cut short
    ],
)
def test_corrupt_files_are_refused_not_crashed(client, owner, settings, broken):
    base = stardict(ENGLISH)
    if "t.idx.gz" in broken:
        base.pop("t.idx")
    r = install(client, owner["csrf"], _with(base, **broken))
    assert r.status_code == 422 and r.json()["detail"] == "corrupt", r.text
    assert dictionary.installed(settings) == []
    assert list((settings.data_dir / "dictionaries" / ".staging").iterdir()) == []


def test_decompressed_size_is_capped(client, owner, settings, monkeypatch):
    """A small upload whose .idx.gz or .dict.dz inflates past the caps is refused while reading."""
    monkeypatch.setattr(dictionary, "MAX_INDEX_BYTES", 64 * 1024)
    bomb = stardict(ENGLISH)
    bomb.pop("t.idx")
    bomb["t.idx.gz"] = gzip.compress(b"\0" * (1 << 20))
    r = install(client, owner["csrf"], bomb)
    assert r.status_code == 413 and r.json()["detail"] == "dictionary_too_large", r.text
    monkeypatch.setattr(dictionary, "MAX_UNPACKED_BYTES", 256 * 1024)
    r = install(
        client,
        owner["csrf"],
        _with(stardict(ENGLISH), **{"t.dict.dz": gzip.compress(b"\0" * (1 << 20))}),
    )
    assert r.status_code == 413 and r.json()["detail"] == "dictionary_too_large", r.text
    assert dictionary.installed(settings) == []


def test_one_entry_reads_at_most_the_entry_cap(client, owner, settings, monkeypatch):
    """An .idx entry claiming most of a large .dict reads only MAX_ENTRY_BYTES of it."""
    monkeypatch.setattr(dictionary, "MAX_ENTRY_BYTES", 4096)
    reads: list[int] = []
    real = dictionary._render
    monkeypatch.setattr(
        dictionary, "_render", lambda data, types: reads.append(len(data)) or real(data, types)
    )
    body = b"a" * (1 << 20)
    files = stardict([("big", "x")])
    files["t.idx"] = b"big\0" + struct.pack(">II", 0, len(body))
    files["t.dict.dz"] = gzip.compress(body)
    r = install(client, owner["csrf"], files)
    assert r.status_code == 201, r.text
    assert reads == [4096]
    [e] = look(client, "big")["entries"]
    assert e["definitions"] == ["a" * 600]  # one line, shown cut short


def test_a_typed_entry_cut_at_the_cap_still_renders(client, owner, monkeypatch):
    monkeypatch.setattr(dictionary, "MAX_ENTRY_BYTES", 10)
    # "m" text then a binary field whose size word is cut off by the cap
    assert dictionary._render(b"hello\0\0\0", "mW") == "hello"


@pytest.mark.parametrize("kind", ["zip", "tar.gz"])
def test_a_corrupt_archive_is_refused_not_crashed(client, owner, settings, kind):
    files = stardict(ENGLISH, dz=False, extra={"pad.txt": "".join(str(i) for i in range(200_000))})
    buf = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for n, b in files.items():
                z.writestr(f"d/{n}", b)
        raw = bytearray(buf.getvalue())
        mid = len(raw) // 3
        raw[mid : mid + 64] = b"\xff" * 64  # corrupt compressed data, directory intact
        body = bytes(raw)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for n, b in files.items():
                info = tarfile.TarInfo(f"d/{n}")
                info.size = len(b)
                t.addfile(info, io.BytesIO(b))
        body = buf.getvalue()[: len(buf.getvalue()) // 2]  # truncated
    r = client.post(
        "/api/admin/dictionaries?lang=en",
        content=body,
        headers={"origin": ORIGIN, "x-csrf-token": owner["csrf"]},
    )
    assert r.status_code == 422 and r.json()["detail"] == "corrupt", r.text
    assert list((settings.data_dir / "dictionaries" / ".staging").iterdir()) == []


def test_archive_paths_cannot_escape(client, owner, settings):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../../escaped.ifo", b"StarDict's dict ifo file\n")
    r = client.post(
        "/api/admin/dictionaries?lang=en",
        content=buf.getvalue(),
        headers={"origin": ORIGIN, "x-csrf-token": owner["csrf"]},
    )
    assert r.status_code == 422 and r.json()["detail"] == "unsafe_archive"
    assert not list(settings.data_dir.parent.rglob("escaped.ifo"))


def test_cli_imports_a_folder_already_on_the_machine(tmp_path, settings, monkeypatch, capsys):
    from lectern import cli

    folder = tmp_path / "wordnet"
    folder.mkdir()
    for n, b in stardict(ENGLISH, dz=False).items():
        (folder / n).write_bytes(b)
    monkeypatch.setenv("LECTERN_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("LECTERN_LIBRARY_DIR", str(settings.library_dir))
    cli.main(["dictionary-import", str(folder / "t.ifo"), "--lang", "en"])
    assert "9 entries" in capsys.readouterr().out
    cli.main(["dictionary-list"])
    assert "Test Dictionary" in capsys.readouterr().out
    assert dictionary.lookup(settings, "runs", "en")["entries"][0]["headword"] == "run"


def test_who_may_look_up_and_who_may_install(client, owner, english):
    reader = make_user(client, owner["csrf"], "rosa", "reader")
    csrf = login(client, "rosa", reader["password"])
    assert heads(look(client, "run")) == ["run"]
    assert client.get("/api/admin/dictionaries").status_code == 403
    assert install(client, csrf, stardict(ENGLISH)).status_code == 403
    r = client.patch(
        f"/api/admin/dictionaries/{english['id']}",
        json={"enabled": False},
        headers={"origin": ORIGIN, "x-csrf-token": csrf},
    )
    assert r.status_code == 403
    client.cookies.clear()
    assert client.get("/api/dictionary/lookup", params={"q": "run"}).status_code == 401


def test_lookups_are_rate_limited(client, english, app):
    from lectern.security.ratelimit import SlidingWindowLimiter

    app.state.limiters["dictionary"] = SlidingWindowLimiter(limit=2, window_seconds=60)
    look(client, "run")
    look(client, "fly")
    r = client.get("/api/dictionary/lookup", params={"q": "ice"})
    assert r.status_code == 429


_net: list[str] = []
_watching = False


def _watch(event: str, args) -> None:
    if _watching and event.startswith("socket.") and event != "socket.__new__":
        _net.append(event)


sys.addaudithook(_watch)


def test_a_lookup_makes_no_outbound_request(client, english, settings):
    """The egress guard (#31) is on and a lookup still answers; and no socket call happens."""
    global _watching
    egress.enforce(True)
    with pytest.raises(egress.OfflineError):  # the guard really is on
        socket.create_connection(("example.com", 80), timeout=1)
    _net.clear()
    _watching = True
    try:
        r = look(client, "running", to="fr")
        direct = dictionary.lookup(settings, "flies", "en", "fr")
    finally:
        _watching = False
    assert heads(r) == ["run"] and heads(direct) == ["fly"]
    assert _net == []


def test_candidates_and_lemmas():
    assert dictionary.candidates("  “Ice cream!” ", "en")[0] == ("Ice cream", ["ice cream"])
    assert [w for w, _ in dictionary.candidates("red apples", "en")] == [
        "red apples",
        "red",
        "apples",
    ]
    assert "homme" in dictionary.lemmas("l'hommes", "fr")
    assert "cheval" in dictionary.lemmas("chevaux", "fr")
    assert "haus" in dictionary.lemmas("hauses", "de")
    assert "die" in dictionary.lemmas("dying", "en")
    assert "hope" in dictionary.lemmas("hoping", "en")
    assert dictionary.lemmas("is", "en") == []
    assert dictionary.fold("Ça Va") == "ca va"
    assert dictionary.fold("Москва́") == "москва"
    assert dictionary.fold("नमस्ते") == "नमस्ते"  # other scripts keep their marks
    assert dictionary.primary("pt-BR") == "pt" and dictionary.primary("not a tag") is None


def test_sixty_four_bit_offsets(tmp_path, settings):
    word, body = "zed", b"the letter z"
    folder = tmp_path / "d64"
    folder.mkdir()
    idx = word.encode() + b"\0" + struct.pack(">QI", 0, len(body))
    (folder / "x.ifo").write_text(
        "StarDict's dict ifo file\nversion=3.0.0\nbookname=Z\nwordcount=1\n"
        f"idxfilesize={len(idx)}\nidxoffsetbits=64\nsametypesequence=m\n"
    )
    (folder / "x.idx").write_bytes(idx)
    (folder / "x.dict").write_bytes(body)
    meta = dictionary.import_path(settings, folder, lang="en")
    assert meta.entries == 1
    assert dictionary.lookup(settings, "Zed", "en")["entries"][0]["definitions"] == ["the letter z"]


def test_staging_is_on_the_data_disk(settings):
    with dictionary.staging_file(settings) as p:
        assert Path(p).parent == settings.data_dir / "dictionaries" / ".staging"
    assert not Path(p).exists()
