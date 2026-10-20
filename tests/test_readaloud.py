"""Milestone 4 read-aloud: sentence wrapping, EPUB 3 Media Overlay export, cache, and API.

Uses the Gift of the Magi fixture (tests/fixtures/magi): an EPUB 2 file, a 77 s excerpt of
the LibriVox narration, and the hand-checked 15-sentence map.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from lxml import etree
from sqlalchemy import select
from tests.conftest import ORIGIN, api_post, login

from lectern.db import session_factory
from lectern.ingest.epub import read_epub_metadata
from lectern.ingest.hashing import partial_md5, sha256_file
from lectern.ingest.library import ingest_epub
from lectern.models import (
    Alignment,
    AlignmentStatus,
    AudioTrack,
    AuditLog,
    BookFile,
    Sentence,
    Work,
)
from lectern.positions import normalize_spine_text
from lectern.readaloud import cache as cache_mod
from lectern.readaloud.overlay import (
    OverlayError,
    OverlayTrack,
    build_read_aloud_epub,
    spine_texts,
    wrap_sentences,
)
from lectern.readaloud.text import SpineText
from lectern.sentence_map import SentenceEntry, audio_fingerprint, load_sentence_map

FIXTURE = Path(__file__).parent / "fixtures" / "magi"
EPUB = FIXTURE / "the-gift-of-the-magi.epub"
EXCERPT = FIXTURE / "excerpt.mp3"
MAP = FIXTURE / "sentence_map.json"
SMIL = "{http://www.w3.org/ns/SMIL}"
OPF = "{http://www.idpf.org/2007/opf}"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _sentences(
    text: str, parts: list[str], spine_index: int = 0, times=None
) -> list[SentenceEntry]:
    """Sentence entries located by searching the normalised text (independent of wrapping)."""
    out, pos = [], 0
    for n, part in enumerate(parts):
        start = text.index(part, pos)
        pos = start + len(part)
        a, b = times[n] if times else (None, None)
        out.append(
            SentenceEntry(
                n=n,
                spine_index=spine_index,
                char_start=start,
                char_end=pos,
                audio_start_s=a,
                audio_end_s=b,
                matched=a is not None,
            )
        )
    return out


def _span_text(root, span_id: str) -> str:
    el = root.find(f".//*[@id='{span_id}']")
    assert el is not None, span_id
    return " ".join("".join(el.itertext()).split())


# --- wrapping ------------------------------------------------------------------------------


def test_wrapping_splits_inline_markup_and_paragraphs_without_changing_text():
    xhtml = b"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body>
<p>Alpha <em id="e1" class="k">beta. Gamma</em> delta. <a href="#x"><b>Eps</b>ilon zeta.</a></p>
<p>Eta theta</p>
<p>iota. <span>Kappa</span>.</p><script>var x = "not text";</script>
</body></html>"""
    st = SpineText(xhtml)
    text = normalize_spine_text(xhtml)
    assert st.text == text
    parts = ["Alpha beta.", "Gamma delta.", "Epsilon zeta.", "Eta theta iota.", "Kappa."]
    wrapped = wrap_sentences(st, _sentences(text, parts), "s")
    root = st.tree.getroot()

    assert {n: [p.id for p in ps] for n, ps in wrapped.items()} == {
        0: ["s0"], 1: ["s1"], 2: ["s2"], 3: ["s3", "s3-2"], 4: ["s4"],
    }  # fmt: skip
    for n, want in enumerate(parts):
        got = " ".join(_span_text(root, p.id) for p in wrapped[n])
        assert got == want
    # The emphasis was split, not lost: "Gamma" is still emphasised inside s1, and only the
    # original element keeps its id.
    s1 = root.find(".//*[@id='s1']")
    em = s1.find("{http://www.w3.org/1999/xhtml}em")
    assert em is not None and em.text == "Gamma" and em.get("class") == "k" and em.get("id") is None
    assert len(root.findall(".//*[@id='e1']")) == 1
    # A sentence across two paragraphs gets one wrapper per paragraph, never a span around <p>.
    for span in root.iter("{http://www.w3.org/1999/xhtml}span"):
        assert span.find("{http://www.w3.org/1999/xhtml}p") is None
    out = etree.tostring(st.tree)
    assert normalize_spine_text(out) == text
    ids = [e.get("id") for e in root.iter() if isinstance(e.tag, str) and e.get("id")]
    assert len(ids) == len(set(ids))

    # A partial map (the fixture covers only the opening): text after the last sentence that
    # shares its inline element must stay outside the wrapper.
    st = SpineText(xhtml)
    wrap_sentences(st, _sentences(text, ["Alpha beta."]), "s")
    assert _span_text(st.tree.getroot(), "s0") == "Alpha beta."
    assert normalize_spine_text(etree.tostring(st.tree)) == text


def test_wrapping_rejects_a_span_beyond_the_text():
    st = SpineText(b"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>Short.</p></body></html>")
    with pytest.raises(ValueError):
        wrap_sentences(st, [SentenceEntry(n=0, spine_index=0, char_start=0, char_end=99)], "s")


# --- building --------------------------------------------------------------------------------


def _fixture_tracks(m) -> list[OverlayTrack]:
    return [OverlayTrack(EXCERPT, "audio/mpeg", t.offset_s, t.duration_s) for t in m.tracks]


def test_fixture_export_smil_timing_opf_and_text(tmp_path):
    m = load_sentence_map(MAP)
    out = tmp_path / "ra.epub"
    source_sha, source_pmd5 = sha256_file(EPUB), partial_md5(EPUB)
    report = build_read_aloud_epub(EPUB, out, m.sentences, _fixture_tracks(m), modified=NOW)

    assert (sha256_file(EPUB), partial_md5(EPUB)) == (source_sha, source_pmd5)
    assert report.upgraded_from == "2.0" and report.pars == 15 and report.overlays == 1
    assert spine_texts(out) == spine_texts(EPUB)
    with zipfile.ZipFile(out) as zf:
        assert zf.infolist()[0].filename == "mimetype"
        opf = etree.fromstring(zf.read("OEBPS/content.opf"))
        smil = etree.fromstring(zf.read("OEBPS/lectern/smil/0002.smil"))
        story = etree.fromstring(zf.read("OEBPS/5088469616467574847_7256-h-1.htm.html"))
        assert sha256_file_bytes(zf.read("OEBPS/lectern/audio/track-000.mp3")) == m.tracks[0].sha256
        # Untouched resources are byte-identical.
        with zipfile.ZipFile(EPUB) as src:
            for name in (
                "OEBPS/1201993274004875867_7256-cover.png",
                "OEBPS/0.css",
                "OEBPS/toc.ncx",
            ):
                assert zf.read(name) == src.read(name)

    assert opf.get("version") == "3.0"
    items = {i.get("id"): i for i in opf.iter(f"{OPF}item")}
    assert items["item4"].get("media-overlay") == "lectern-mo-0002"
    assert [i for i in items.values() if i.get("media-overlay")] == [items["item4"]]
    assert items["lectern-mo-0002"].get("media-type") == "application/smil+xml"
    assert "nav" in items["lectern-nav"].get("properties")
    assert "cover-image" in items["item1"].get("properties")
    assert len(opf.findall(".//{http://purl.org/dc/elements/1.1/}date")) == 1
    metas = {(e.get("property"), e.get("refines")): e.text for e in opf.iter(f"{OPF}meta")}
    expected_ms = sum(
        round(s.audio_end_s * 1000) - round(s.audio_start_s * 1000) for s in m.sentences
    )
    clock = (
        f"0:{expected_ms // 60000:02d}:{expected_ms % 60000 // 1000:02d}.{expected_ms % 1000:03d}"
    )
    assert metas[("media:duration", None)] == clock == metas[("media:duration", "#lectern-mo-0002")]
    assert metas[("dcterms:modified", None)] == "2026-10-20T12:00:00Z"

    pars = smil.findall(f".//{SMIL}par")
    assert len(pars) == 15
    for s, par in zip(m.sentences, pars, strict=True):
        text, audio = par.find(f"{SMIL}text"), par.find(f"{SMIL}audio")
        assert text.get("src") == f"../../5088469616467574847_7256-h-1.htm.html#s{s.n}"
        assert audio.get("src") == "../audio/track-000.mp3"
        assert audio.get("clipBegin") == f"{s.audio_start_s:.3f}s"
        assert audio.get("clipEnd") == f"{s.audio_end_s:.3f}s"
        assert _span_text(story, f"s{s.n}") == s.text


def sha256_file_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _two_chapter_epub(path: Path) -> Path:
    """An EPUB 3 with two chapters in a subdirectory, for chapter and track boundaries."""
    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:7c1f3f2e-0000-4000-8000-000000000001</dc:identifier>
    <dc:title>Two Chapters</dc:title><dc:language>en</dc:language>
    <meta property="dcterms:modified">2020-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="c1" href="text/one.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="text/two.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>"""
    container = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

    def doc(title: str, body: str) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en">'
            f"<head><title>{title}</title></head><body>{body}</body></html>"
        )

    nav = doc(
        "Contents",
        '<nav epub:type="toc"><ol><li><a href="text/one.xhtml">One</a></li>'
        '<li><a href="text/two.xhtml">Two</a></li></ol></nav>',
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OPS/package.opf", opf)
        zf.writestr("OPS/nav.xhtml", nav)
        zf.writestr(
            "OPS/text/one.xhtml", doc("One", "<h1>One</h1><p>First line. Second <i>line</i>.</p>")
        )
        zf.writestr("OPS/text/two.xhtml", doc("Two", "<h1>Two</h1><p>Third line. Fourth line.</p>"))
    return path


def test_chapter_and_track_boundaries(tmp_path):
    src = _two_chapter_epub(tmp_path / "two.epub")
    texts = spine_texts(src)
    s0 = _sentences(texts[0], ["One", "First line.", "Second line."], 0,
                    [(0.5, 1.0), (1.0, 4.0), (4.0, 9.5)])  # fmt: skip
    s1 = _sentences(texts[1], ["Two", "Third line.", "Fourth line."], 1,
                    [(10.5, 11.0), (11.0, 14.0), (75.0, 79.0)])  # fmt: skip
    for i, s in enumerate(s1, start=3):
        s.n = i
    # Track 0 is 10 s, track 1 is the fixture excerpt starting at 10 s on the work timeline.
    track0 = tmp_path / "t0.mp3"
    shutil.copy(EXCERPT, track0)
    tracks = [
        OverlayTrack(track0, "audio/mpeg", 0.0, 10.0),
        OverlayTrack(EXCERPT, "audio/mpeg", 10.0, 67.166),
    ]
    out = tmp_path / "out.epub"
    report = build_read_aloud_epub(src, out, s0 + s1, tracks, modified=NOW)
    assert report.overlays == 2 and report.pars == 6 and report.upgraded_from is None
    with zipfile.ZipFile(out) as zf:
        one = etree.fromstring(zf.read("OPS/lectern/smil/0000.smil"))
        two = etree.fromstring(zf.read("OPS/lectern/smil/0001.smil"))
        opf = etree.fromstring(zf.read("OPS/package.opf"))
        doc = zf.read("OPS/text/one.xhtml")
    assert doc.startswith(b"<?xml") and b"<!DOCTYPE html>" in doc  # EPUB 3 doctype kept

    def clips(smil):
        return [
            (p.find(f"{SMIL}text").get("src"), p.find(f"{SMIL}audio").get("src"),
             p.find(f"{SMIL}audio").get("clipBegin"), p.find(f"{SMIL}audio").get("clipEnd"))
            for p in smil.iter(f"{SMIL}par")
        ]  # fmt: skip

    assert clips(one) == [
        ("../../text/one.xhtml#s0", "../audio/track-000.mp3", "0.500s", "1.000s"),
        ("../../text/one.xhtml#s1", "../audio/track-000.mp3", "1.000s", "4.000s"),
        ("../../text/one.xhtml#s2", "../audio/track-000.mp3", "4.000s", "9.500s"),
    ]
    # Chapter two plays from the second file, with clip times relative to that file; the last
    # sentence runs past the end of the audio and is cut at the end of its track.
    assert clips(two) == [
        ("../../text/two.xhtml#s3", "../audio/track-001.mp3", "0.500s", "1.000s"),
        ("../../text/two.xhtml#s4", "../audio/track-001.mp3", "1.000s", "4.000s"),
        ("../../text/two.xhtml#s5", "../audio/track-001.mp3", "65.000s", "67.166s"),
    ]
    metas = {(e.get("property"), e.get("refines")): e.text for e in opf.iter(f"{OPF}meta")}
    assert metas[("media:duration", "#lectern-mo-0000")] == "0:00:09.000"
    assert metas[("media:duration", "#lectern-mo-0001")] == "0:00:05.666"
    assert metas[("media:duration", None)] == "0:00:14.666"


def test_unsupported_audio_and_unmatched_only_are_refused(tmp_path):
    m = load_sentence_map(MAP)
    with pytest.raises(OverlayError, match="cannot be embedded"):
        build_read_aloud_epub(
            EPUB, tmp_path / "x.epub", m.sentences,
            [OverlayTrack(EXCERPT, "audio/flac", 0.0, 77.0)], modified=NOW,
        )  # fmt: skip
    unmatched = [
        s.model_copy(update={"matched": False, "audio_start_s": None, "audio_end_s": None})
        for s in m.sentences
    ]
    with pytest.raises(OverlayError, match="no sentence has audio"):
        build_read_aloud_epub(
            EPUB, tmp_path / "y.epub", unmatched, _fixture_tracks(m), modified=NOW
        )


def test_epubcheck_accepts_read_aloud_exports(tmp_path):
    """Runs epubcheck when EPUBCHECK_JAR points at it. CI downloads a pinned release and also
    sets EPUBCHECK_REQUIRED, so this test cannot silently skip there."""
    jar = os.environ.get("EPUBCHECK_JAR")
    if not jar:
        if os.environ.get("EPUBCHECK_REQUIRED"):
            pytest.fail("EPUBCHECK_REQUIRED is set but EPUBCHECK_JAR is not")
        pytest.skip("set EPUBCHECK_JAR to run epubcheck")
    m = load_sentence_map(MAP)
    magi = tmp_path / "magi.epub"
    build_read_aloud_epub(EPUB, magi, m.sentences, _fixture_tracks(m), modified=NOW)
    src = _two_chapter_epub(tmp_path / "two.epub")
    texts = spine_texts(src)
    s = _sentences(texts[0], ["First line.", "Second line."], 0, [(0.5, 1.0), (1.0, 4.0)])
    two = tmp_path / "two-ra.epub"
    build_read_aloud_epub(src, two, s, _fixture_tracks(m), modified=NOW)
    for path in (magi, two):
        r = subprocess.run(
            ["java", "-jar", jar, str(path)],
            capture_output=True, text=True, timeout=300,
        )  # fmt: skip
        assert r.returncode == 0, r.stdout + r.stderr
        assert "No errors or warnings detected" in r.stdout, r.stdout


# --- cache, API, and download ------------------------------------------------------------------


def seed_magi(settings, tmp_path: Path) -> int:
    """What milestones 2 and 3 will produce: the work, its audio track, and its alignment."""
    m = load_sentence_map(MAP)
    staged = tmp_path / "staged.epub"
    shutil.copy(EPUB, staged)
    with session_factory()() as db:
        work, _book, _ = ingest_epub(
            db, settings.library_dir, staged, read_epub_metadata(staged),
            sha256_file(EPUB), partial_md5(EPUB),
        )  # fmt: skip
        audio_dir = settings.library_dir / work.dir_path / "audio"
        audio_dir.mkdir(parents=True)
        shutil.copy(EXCERPT, audio_dir / "excerpt.mp3")
        t = m.tracks[0]
        db.add(
            AudioTrack(
                work_id=work.id, position=0, rel_path="audio/excerpt.mp3", media_type="audio/mpeg",
                codec="mp3", duration_s=t.duration_s, offset_s=0.0, size=EXCERPT.stat().st_size,
                sha256=t.sha256,
            )
        )  # fmt: skip
        for s in m.sentences:
            db.add(Sentence(work_id=work.id, **s.model_dump(exclude={"text"})))
        db.add(
            Alignment(
                work_id=work.id, model=m.model, status=AlignmentStatus.SUCCEEDED,
                coverage=m.coverage, sentence_count=len(m.sentences),
                epub_sha256=m.epub_sha256, audio_fingerprint=m.audio_fingerprint,
            )
        )  # fmt: skip
        db.commit()
        return work.id


def _wait_ready(client, work_id: int) -> dict:
    for _ in range(200):
        st = client.get(f"/api/works/{work_id}/read-aloud").json()
        if st["status"] != "generating":
            return st
        time.sleep(0.05)
    raise AssertionError("build did not finish")


def _derived(settings, work_id: int) -> Path:
    with session_factory()() as db:
        return settings.library_dir / db.get(Work, work_id).dir_path / "derived"


def test_read_aloud_status_build_and_download(client, owner, settings, tmp_path):
    work_id = seed_magi(settings, tmp_path)
    with session_factory()() as db:
        book = db.scalar(select(BookFile).where(BookFile.work_id == work_id))
        book_path = settings.library_dir / db.get(Work, work_id).dir_path / book.rel_path
        before = (book.sha256, book.partial_md5, sha256_file(book_path), partial_md5(book_path))

    st = client.get(f"/api/works/{work_id}/read-aloud").json()
    assert (
        st["status"] == "not_generated" and st["readalong"] is True and st["download_url"] is None
    )
    # Building is a write: CSRF token and same-origin Origin are required.
    assert (
        client.post(f"/api/works/{work_id}/read-aloud", headers={"origin": ORIGIN}).status_code
        == 403
    )
    r = api_post(client, f"/api/works/{work_id}/read-aloud", owner["csrf"])
    assert r.status_code == 202 and r.json()["status"] in ("generating", "ready")
    st = _wait_ready(client, work_id)
    assert st["status"] == "ready", st
    assert st["download_url"] == f"/library/works/{work_id}/read-aloud.epub"

    derived = _derived(settings, work_id)
    assert sorted(p.name for p in derived.iterdir()) == ["read-aloud.epub", "read-aloud.json"]
    sidecar = json.loads((derived / "read-aloud.json").read_text())
    assert sidecar["key"]["epub_sha256"] == before[0]
    assert sidecar["key"]["audio_fingerprint"] == audio_fingerprint(
        [load_sentence_map(MAP).tracks[0].sha256]
    )
    assert sidecar["size"] == st["size"] == (derived / "read-aloud.epub").stat().st_size

    r = client.get(st["download_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/epub+zip"
    assert "read-aloud" in r.headers["content-disposition"]
    assert sha256_file_bytes(r.content) == sidecar["sha256"]
    with session_factory()() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.action == "download")).all()
        assert [a.target for a in rows] == [f"read-aloud:{work_id}"]
        book = db.scalar(select(BookFile).where(BookFile.work_id == work_id))
        # The plain EPUB is untouched, so KOReader's partial-MD5 document id is stable.
        assert (
            book.sha256,
            book.partial_md5,
            sha256_file(book_path),
            partial_md5(book_path),
        ) == before


def test_cache_invalidation_and_atomic_replacement(client, owner, settings, tmp_path, monkeypatch):
    work_id = seed_magi(settings, tmp_path)
    url = f"/library/works/{work_id}/read-aloud.epub"
    first = client.get(url)  # generated synchronously on first request
    assert first.status_code == 200
    derived = _derived(settings, work_id)
    out = derived / "read-aloud.epub"
    old_inode = out.stat().st_ino
    held = out.open("rb")  # a download in flight

    # Re-download: cache hit, same file.
    assert client.get(url).content == first.content and out.stat().st_ino == old_inode

    # New alignment of the same files (different timings) invalidates the cache.
    with session_factory()() as db:
        s = db.scalar(select(Sentence).where(Sentence.work_id == work_id, Sentence.n == 14))
        s.audio_end_s = 76.0
        db.commit()
    assert client.get(f"/api/works/{work_id}/read-aloud").json()["status"] == "not_generated"

    # A failing build leaves the previous file and sidecar alone and no temporary files.
    def boom(source, dest, *a, **kw):
        dest.write_bytes(b"partial")
        raise OverlayError("simulated failure")

    monkeypatch.setattr(cache_mod, "build_read_aloud_epub", boom)
    r = client.get(url)
    assert r.status_code == 422 and r.json() == {"detail": "simulated failure"}
    assert out.stat().st_ino == old_inode
    assert sorted(p.name for p in derived.iterdir()) == ["read-aloud.epub", "read-aloud.json"]
    monkeypatch.undo()

    second = client.get(url)
    assert second.status_code == 200 and second.content != first.content
    assert out.stat().st_ino != old_inode  # replaced by rename, not rewritten in place
    assert held.read() == first.content  # the open reader still sees the complete old file
    held.close()
    with zipfile.ZipFile(out) as zf:
        assert b'clipEnd="76.000s"' in zf.read("OEBPS/lectern/smil/0002.smil")

    # Changed inputs that break the alignment are reported, not served from the old cache.
    with session_factory()() as db:
        db.scalar(
            select(Alignment).where(Alignment.work_id == work_id)
        ).status = AlignmentStatus.STALE
        db.commit()
    assert client.get(url).status_code == 409 and client.get(url).json() == {
        "detail": "alignment_stale"
    }
    st = client.get(f"/api/works/{work_id}/read-aloud").json()
    assert (
        st["status"] == "unavailable"
        and st["reason"] == "alignment_stale"
        and st["readalong"] is False
    )


def test_read_aloud_requires_auth_and_visibility(client, owner, settings, tmp_path):
    work_id = seed_magi(settings, tmp_path)
    r = api_post(
        client, "/api/admin/users", owner["csrf"],
        {"username": "rita", "role": "reader", "password": "reader password 123"},
    )  # fmt: skip
    assert r.status_code == 201
    login(client, "rita", "reader password 123")
    for path in (
        f"/library/works/{work_id}/read-aloud.epub",
        f"/api/works/{work_id}/read-aloud",
        f"/api/works/{work_id}/sentences?from=0",
        f"/api/works/{work_id}/text/2",
        f"/api/works/{work_id}/tracks",
        f"/api/works/{work_id}/progress",
    ):
        assert client.get(path).status_code == 404, path  # not on a granted shelf
    client.cookies.clear()
    assert client.get(f"/library/works/{work_id}/read-aloud.epub").status_code == 401
    assert client.get(f"/api/works/{work_id}/read-aloud").status_code == 401


def test_read_aloud_states_without_alignment(client, owner, settings, tmp_path):
    work_id = seed_magi(settings, tmp_path)
    with session_factory()() as db:
        db.delete(db.scalar(select(Alignment).where(Alignment.work_id == work_id)))
        db.commit()
    st = client.get(f"/api/works/{work_id}/read-aloud").json()
    assert st["status"] == "unavailable" and st["reason"] == "no_alignment"
    r = api_post(client, f"/api/works/{work_id}/read-aloud", owner["csrf"])
    assert r.status_code == 409 and r.json() == {"detail": "no_alignment"}
    assert client.get(f"/library/works/{work_id}/read-aloud.epub").status_code == 409


# --- read-along API -------------------------------------------------------------------------


def test_sentences_text_tracks_and_stream(client, owner, settings, tmp_path):
    work_id = seed_magi(settings, tmp_path)
    m = load_sentence_map(MAP)
    r = client.get(f"/api/works/{work_id}/sentences?from=0&to=2000").json()
    assert r["count"] == 15 and r["alignment"]["status"] == "succeeded"
    assert [s["n"] for s in r["sentences"]] == list(range(15))
    assert "text" not in r["sentences"][0]
    # t=3.5 s is inside sentence 1 (3.28-4.01 s)
    r = client.get(f"/api/works/{work_id}/sentences?t=3.5&around=1").json()
    assert [s["n"] for s in r["sentences"]] == [0, 1, 2]
    r = client.get(f"/api/works/{work_id}/sentences?spine=2&char=40").json()
    assert r["sentences"][0]["n"] == 1
    assert client.get(f"/api/works/{work_id}/sentences?from=0&t=1").status_code == 400

    text = client.get(f"/api/works/{work_id}/text/2").json()
    assert text["length"] == 29283
    joined = {b["start"]: b["text"] for b in text["blocks"]}
    first = text["blocks"][0]
    assert first["start"] == 0 and first["text"].startswith("One dollar and eighty-seven cents.")
    for s in m.sentences:  # every sentence lies inside one block at the right offset
        block = max(
            (b for b in text["blocks"] if b["start"] <= s.char_start), key=lambda b: b["start"]
        )
        assert block["text"][s.char_start - block["start"] : s.char_end - block["start"]] == s.text
    assert len(joined) == len(text["blocks"])
    assert client.get(f"/api/works/{work_id}/text/99").status_code == 404

    tracks = client.get(f"/api/works/{work_id}/tracks").json()
    assert tracks["audio_fingerprint"] == m.audio_fingerprint
    assert tracks["total_duration_s"] == pytest.approx(77.166)
    stream = tracks["tracks"][0]["stream_url"]
    whole = client.get(stream)
    assert whole.status_code == 200 and whole.headers["accept-ranges"] == "bytes"
    assert whole.headers["etag"] == f'"{m.tracks[0].sha256}"'
    part = client.get(stream, headers={"range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == EXCERPT.read_bytes()[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{EXCERPT.stat().st_size}"
    assert client.get(stream, headers={"range": "bytes=999999999-"}).status_code == 416


def test_progress_put_and_get(client, owner, settings, tmp_path):
    work_id = seed_magi(settings, tmp_path)
    url = f"/api/works/{work_id}/progress"
    empty = client.get(url).json()
    assert empty["position"] is None and empty["percent"] is None

    def put(body):
        return client.put(url, json=body, headers={"origin": ORIGIN, "x-csrf-token": owner["csrf"]})

    assert (
        client.put(
            url, json={"source": "web", "sentence_n": 1}, headers={"origin": ORIGIN}
        ).status_code
        == 403
    )
    r = put({"source": "audio", "audio_s": 3.5, "device": "Phone"})
    assert r.status_code == 200
    p = r.json()
    assert p["position"]["sentence_n"] == 1 and p["source"] == "audio" and p["raw"] == "3.5"
    assert p["device"] == "Phone" and p["percent"] == pytest.approx(1 / 15)
    assert p["text"] == {"spine_index": 2, "char": 35 + p["position"]["offset"]}
    r = put({"source": "web", "sentence_n": 4, "offset": 999})
    assert r.json()["position"] == {"sentence_n": 4, "offset": 29}  # clamped to its 29 chars
    assert r.json()["audio_s"] == pytest.approx(22.68)  # end of sentence 4
    assert client.get(url).json()["position"]["sentence_n"] == 4  # last write wins
    assert put({"source": "koreader", "sentence_n": 1}).status_code == 422
    assert put({"source": "web", "sentence_n": 1, "audio_s": 2.0}).status_code == 422
    assert put({"source": "web", "sentence_n": 15}).status_code == 422
    with session_factory()() as db:
        for s in db.scalars(select(Sentence).where(Sentence.work_id == work_id)):
            db.delete(s)
        db.commit()
    assert put({"source": "web", "sentence_n": 0}).status_code == 409
