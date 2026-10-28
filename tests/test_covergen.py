"""Generated covers: palettes meet WCAG contrast, designs are deterministic, every script draws
with a font that has its glyphs, right-to-left titles align right, long titles fit, and the
server serves, caches, and redraws them while cover art keeps precedence."""

from __future__ import annotations

import base64
import dataclasses
import os
import unicodedata
from io import BytesIO

import pytest
from lxml import etree
from tests.conftest import ORIGIN, api_post, make_app_password
from tests.test_editing import editor, one_book, patch, png

from lectern import covergen as cg

CJK_REQUIRED = os.environ.get("CJK_FONTS_REQUIRED") == "1"


@pytest.fixture(autouse=True)
def _inbox(settings):
    settings.inbox_path.mkdir(parents=True, exist_ok=True)


SAMPLES = {
    "coptic": ("ⲁⲓⲝⲟⲙ ⲡⲓϫⲱⲙ ⲉⲑⲟⲩⲁⲃ", "ⲡⲓⲣⲉⲙⲛ̀ⲭⲏⲙⲓ"),
    "arabic": ("الكتاب المقدس", "جمعية الكتاب المقدس"),
    "hebrew": ("ספר הספרים", "מחבר"),
    "devanagari": ("महाभारत", "वेदव्यास"),
    "greek": ("Ἰλιάς", "Ὅμηρος"),
    "cyrillic": ("Война и мир", "Лев Толстой"),
    "japanese": ("吾輩は猫である", "夏目漱石"),
}


# --- contrast ------------------------------------------------------------------------------------


def test_contrast_matches_wcag_reference_values():
    assert cg.contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert cg.contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)  # famous near-miss
    assert cg.contrast("#ffffff", "#ffffff") == pytest.approx(1.0)


@pytest.mark.parametrize("p", cg.PALETTES, ids=lambda p: p.name)
def test_every_palette_meets_wcag_under_the_lighting(p):
    # Text sits on the ground (all styles) or the colour block (modern); the lighting overlay
    # lightens one corner and shades the other, so check both extremes of each.
    for surface in (p.ground, p.block):
        for lit in (
            cg.mix(surface, "#ffffff", cg.LIGHT_OVERLAY),
            cg.mix(surface, "#000000", cg.SHADE_OVERLAY),
        ):
            assert cg.contrast(p.ink, lit) >= 4.5, (p.name, surface, lit)
    assert cg.contrast(p.accent, p.ground) >= 3.0, p.name  # rules and frames (graphics)


# --- deterministic design ------------------------------------------------------------------------


def test_design_is_stable_distinct_and_shuffle_visits_every_palette():
    a = cg.design_for("Middlemarch", "George Eliot")
    assert a == cg.design_for(" Middlemarch ", "George  Eliot")  # whitespace-normalised
    titles = [f"Book number {i}" for i in range(60)]
    designs = [cg.design_for(t, "Someone") for t in titles]
    assert len({d.palette.name for d in designs}) >= len(cg.PALETTES) - 2
    assert {d.style for d in designs} == set(cg.STYLES)
    shuffled = [
        cg.design_for("Middlemarch", "George Eliot", seed=s).palette
        for s in range(len(cg.PALETTES))
    ]
    assert sorted(p.name for p in shuffled) == sorted(p.name for p in cg.PALETTES)
    assert cg.design_for("Middlemarch", "George Eliot", style="minimal").style == "minimal"
    # The author is part of the hash: same title, different author, different key.
    assert cg.design_for("Poems", "A").key != cg.design_for("Poems", "B").key
    # Even when two titles hash to the same look, the cached image must not be reused.
    assert dataclasses.replace(a, title="Other").key != a.key
    assert dataclasses.replace(a, author="Other").key != a.key


def test_render_is_byte_identical_across_calls():
    d = cg.design_for("The Gift of the Magi", "O. Henry")
    assert cg.render_png(d, width=200) == cg.render_png(d, width=200)


# --- scripts -------------------------------------------------------------------------------------


@pytest.mark.parametrize("script", SAMPLES)
def test_every_character_gets_a_font_that_has_it(script):
    if script == "japanese" and not cg._cjk_font_files():
        if CJK_REQUIRED:
            pytest.fail("Noto CJK fonts are required (fonts-noto-cjk) but none were found")
        pytest.skip("no Noto CJK font installed")
    for text in SAMPLES[script]:
        for chain in (cg._chain(cg.DISPLAY, "ja"), cg._chain(cg.TEXT, "ja")):
            for face, run in cg.runs(text, chain):
                for ch in run:
                    if ch.isspace() or unicodedata.category(ch).startswith("M"):
                        continue
                    assert ord(ch) in cg._cmap(face), (script, ch, face.family)


def test_coptic_letters_stay_in_one_face():
    # ϫ (U+03EB) sits in the Greek block; it must not switch to the Greek face mid-word.
    faces = {face for face, _ in cg.runs("ⲡⲓϫⲱⲙ", cg._chain(cg.DISPLAY, None))}
    assert faces == {cg.COPTIC}


def test_kinsoku_keeps_closing_punctuation_off_line_starts():
    toks = [t.text for t in cg.tokens("吾輩は猫である。名前はまだ無い。")]
    assert all(t[0] not in "。、" for t in toks)
    assert "る。" in toks


def _ink_columns(design: cg.Design, top: int, bottom: int) -> tuple[int, int]:
    from PIL import Image

    img = Image.open(BytesIO(cg.render_png(design))).convert("RGB")
    ink = cg._rgb(design.palette.ink)
    xs = []
    for y in range(top, bottom, 3):
        for x in range(60, cg.WIDTH, 2):
            px = img.getpixel((x, y))
            if sum(abs(px[i] / 255 - ink[i]) for i in range(3)) < 0.25:
                xs.append(x)
    return min(xs), max(xs)


def test_rtl_titles_align_right_and_ltr_titles_left():
    arabic = cg.design_for(*SAMPLES["arabic"], style="modern")
    left, right = _ink_columns(arabic, 140, 780)
    assert right > cg.WIDTH - 110 and left > 200, (left, right)
    english = cg.design_for("The Waves", "Virginia Woolf", style="modern")
    left, right = _ink_columns(english, 140, 780)
    assert left < 130 and right < cg.WIDTH - 110, (left, right)
    assert 'direction="rtl"' in cg.svg(arabic) and 'text-anchor="end"' in cg.svg(arabic)


# --- fitting -------------------------------------------------------------------------------------


def test_long_titles_shrink_and_wrap_inside_the_box_and_short_ones_stay_big():
    chain = cg._chain(cg.DISPLAY, None)
    long = (
        "The Remarkable, Unabridged and Entirely Truthful History of the Lighthouse Keepers "
        "of the Northern Isles"
    )
    box = dict(width=540, height=560, max_size=150, min_size=40, max_lines=6)
    b = cg.fit(long, chain, **box)
    assert 1 < len(b.lines) <= 6 and b.height <= 560
    assert all(cg.measure(ln, chain, b.size) <= 540 for ln in b.lines)
    assert " ".join(b.lines) == long  # nothing dropped
    one = cg.fit("Dune", chain, **box)
    assert one.lines == ["Dune"] and one.size == 150 and one.size > b.size * 2
    # Balanced: no stranded last word.
    two = cg.fit("The Gift of the Magi", chain, **box)
    widths = [cg.measure(ln, chain, two.size) for ln in two.lines]
    assert len(widths) == 1 or min(widths) >= 0.45 * max(widths)


def test_absurd_titles_are_cut_with_an_ellipsis_not_overflowed():
    chain = cg._chain(cg.DISPLAY, None)
    b = cg.fit("word " * 400, chain, width=540, height=560, max_size=150, min_size=40, max_lines=6)
    assert len(b.lines) <= 6 and b.lines[-1].endswith("…")
    assert all(cg.measure(ln, chain, b.size) <= 540 for ln in b.lines)


# --- the cache in derived/ --------------------------------------------------------------------


def test_a_folder_moved_during_rendering_is_not_recreated(tmp_path, monkeypatch):
    # A title edit or trash moves the work folder while the cover is being drawn.
    old, new = tmp_path / "Old Author" / "Old Title", tmp_path / "New Author" / "New Title"
    old.mkdir(parents=True)
    d = cg.design_for("Old Title", "Old Author")
    real = cg.render_jpeg

    def render_while_moving(design, **kw):
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)
        old.parent.rmdir()
        return real(design, **kw)

    monkeypatch.setattr(cg, "render_jpeg", render_while_moving)
    data = cg.cached_cover(old, d)
    assert data[:3] == b"\xff\xd8\xff"  # still served
    assert not (tmp_path / "Old Author").exists()  # no orphan folder
    assert not (new / "derived").exists()  # nothing written into the moved folder either
    monkeypatch.setattr(cg, "render_jpeg", real)
    assert cg.cached_cover(new, d) == data and cg.cache_path(new, d).is_file()


def test_concurrent_misses_never_serve_another_designs_image(tmp_path, monkeypatch):
    # Request A (old title) misses and renders slowly; request B (new title) misses, renders,
    # and stores while A is still drawing; then A stores. B's design must still get B's image.
    a, b = cg.design_for("Persuasion", "Jane Austen"), cg.design_for("Emma", "Jane Austen")
    real = cg.render_jpeg
    want_a, want_b = real(a), real(b)
    done = []

    def render(design, **kw):
        if design == a and not done:
            done.append(1)
            assert cg.cached_cover(tmp_path, b) == want_b  # B completes inside A's render
        return real(design, **kw)

    monkeypatch.setattr(cg, "render_jpeg", render)
    assert cg.cached_cover(tmp_path, a) == want_a
    assert cg.cached_cover(tmp_path, b) == want_b
    assert cg.cached_cover(tmp_path, a) == want_a
    assert len(list((tmp_path / "derived").glob("cover-*.jpg"))) == 1


def test_cjk_font_file_and_content_are_part_of_the_key(tmp_path, monkeypatch):
    font = tmp_path / "NotoSerifCJK-Regular.ttc"
    jp, en = cg.design_for("吾輩は猫である", "夏目漱石"), cg.design_for("Dune", "Frank Herbert")

    def keys(files):
        monkeypatch.setattr(cg, "_cjk_font_files", lambda: files)
        cg._cjk_fonts_digest.cache_clear()
        return jp.key, en.key

    font.write_bytes(b"serif v1")
    v1 = keys((font,))
    font.write_bytes(b"serif v2")
    v2 = keys((font,))
    sans = tmp_path / "NotoSansCJK-Regular.ttc"
    sans.write_bytes(b"serif v2")
    v3 = keys((sans,))
    none = keys(())
    cg._cjk_fonts_digest.cache_clear()
    assert len({v1[0], v2[0], v3[0], none[0]}) == 4  # content, file, and absence all count
    assert v1[1] == v2[1] == v3[1] == none[1]  # Latin covers do not depend on the CJK font


# --- serving, caching, precedence ----------------------------------------------------------------


def _jpeg(resp) -> bytes:
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"
    assert resp.content[:3] == b"\xff\xd8\xff"
    return resp.content


def test_generated_cover_is_served_cached_and_redrawn_when_the_title_changes(
    client, owner, settings
):
    csrf = owner["csrf"]
    wid = one_book(settings, title="Persuasion", authors=["Jane Austen"])
    ed = editor(client, wid)
    assert ed["work"]["cover_generated"] is True and ed["work"]["cover_url"]
    assert ed["work"]["cover_tint"] in {p.ground for p in cg.PALETTES}
    first = _jpeg(client.get(ed["work"]["cover_url"]))
    derived = settings.library_dir / ed["work"]["dir_path"] / "derived"
    [cached] = derived.glob("cover-*.jpg")
    assert cached.read_bytes() == first
    stamp = cached.stat().st_mtime_ns
    assert _jpeg(client.get(f"/library/works/{wid}/cover.jpg")) == first
    assert cached.stat().st_mtime_ns == stamp  # served from the cache, not redrawn
    r = patch(
        client,
        csrf,
        wid,
        {"version": ed["version"], "metadata": {**ed["metadata"], "title": "Emma"}},
    )
    assert r.status_code == 200, r.text
    work = r.json()["editor"]["work"]
    redrawn = _jpeg(client.get(work["cover_url"]))
    assert redrawn != first
    # Only the current design's file remains: earlier titles' covers are cleaned up.
    [now] = (settings.library_dir / work["dir_path"] / "derived").glob("cover-*.jpg")
    assert now.read_bytes() == redrawn
    # Same for the author.
    ed = editor(client, wid)
    meta = {**ed["metadata"], "authors": [{"name": "Someone Else"}]}
    r = patch(client, csrf, wid, {"version": ed["version"], "metadata": meta})
    assert _jpeg(client.get(r.json()["editor"]["work"]["cover_url"])) != redrawn


def test_cover_art_wins_and_generate_is_undoable(client, owner, settings):
    csrf = owner["csrf"]
    wid = one_book(settings, title="Orlando", authors=["Virginia Woolf"], cover=True)
    ed = editor(client, wid)
    work_dir = settings.library_dir / ed["work"]["dir_path"]
    art = (work_dir / "cover.jpg").read_bytes()
    assert ed["work"]["cover_generated"] is False
    assert _jpeg(client.get(ed["work"]["cover_url"])) == art
    assert not list(work_dir.glob("derived/cover-*.jpg"))  # nothing generated for it
    # Choosing a generated cover in the editor sets its look and drops the art (to history).
    body = {
        "version": ed["version"],
        "cover": {"action": "generate", "style": "minimal", "seed": 3},
    }
    r = patch(client, csrf, wid, body)
    assert r.status_code == 200, r.text
    gen = r.json()["editor"]["cover"]["generated"]
    assert gen["style"] == "minimal" and gen["seed"] == 3 and gen["effective_style"] == "minimal"
    got = _jpeg(client.get(f"/library/works/{wid}/cover.jpg"))
    assert got != art and not (work_dir / "cover.jpg").exists()
    want = cg.design_for("Orlando", "Virginia Woolf", style="minimal", seed=3, lang="en")
    assert got == cg.cached_cover(work_dir, want)
    # Undo brings the art and the previous (hashed) style back.
    r = api_post(client, f"/api/works/{wid}/undo", csrf, {"revision_id": r.json()["revision_id"]})
    assert r.status_code == 200, r.text
    ed = editor(client, wid)
    assert ed["cover"]["has_cover"] is True and ed["cover"]["generated"]["style"] is None
    assert _jpeg(client.get(f"/library/works/{wid}/cover.jpg")) == art
    # An uploaded image replaces a generated cover as usual.
    patch(client, csrf, wid, {"version": ed["version"], "cover": {"action": "remove"}})
    h = {"origin": ORIGIN, "x-csrf-token": csrf}
    token = client.post(f"/api/works/{wid}/cover", content=png(), headers=h).json()["token"]
    v = editor(client, wid)["version"]
    r = patch(client, csrf, wid, {"version": v, "cover": {"action": "upload", "token": token}})
    assert r.json()["editor"]["work"]["cover_generated"] is False
    bad = {
        "version": r.json()["editor"]["version"],
        "cover": {"action": "generate", "style": "baroque"},
    }
    assert patch(client, csrf, wid, bad).status_code == 422


def test_editor_preview_draws_the_draft_without_storing(client, owner, settings):
    wid = one_book(settings, title="Persuasion", authors=["Jane Austen"])
    work_dir = settings.library_dir / editor(client, wid)["work"]["dir_path"]
    url = f"/api/works/{wid}/cover/generated.jpg"
    r = client.get(
        url, params={"title": "الكتاب المقدس", "author": "", "style": "modern", "seed": 2}
    )
    _jpeg(r)
    assert r.headers["x-cover-style"] == "modern"
    want = cg.design_for("الكتاب المقدس", "", style="modern", seed=2)
    assert r.headers["x-cover-palette"] == want.palette.name
    assert not list(work_dir.glob("derived/cover-*.jpg"))
    assert client.get(url, params={"title": "x" * 600}).status_code == 422
    assert client.get(url, params={"style": "baroque"}).status_code == 422
    assert client.get(f"/api/works/{wid + 99}/cover/generated.jpg").status_code == 404


def test_opds_lists_and_serves_the_generated_cover(client, owner, settings):
    wid = one_book(settings, title="Persuasion", authors=["Jane Austen"])
    pw = make_app_password(client, owner["csrf"], "kobo")
    client.cookies.clear()
    auth = {"authorization": "Basic " + base64.b64encode(f"owner:{pw}".encode()).decode()}
    tree = etree.fromstring(client.get("/opds/all", headers=auth).content)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    rels = {ln.get("rel"): ln.get("href") for ln in tree.find("a:entry", ns).findall("a:link", ns)}
    assert rels["http://opds-spec.org/image"] == f"/opds/works/{wid}/cover.jpg"
    _jpeg(client.get(rels["http://opds-spec.org/image/thumbnail"], headers=auth))


# --- font variants (issue #85) ---------------------------------------------------------------

# The key of a default-font design. Hard-coded: changing it re-renders every cached cover and
# changes every ?v=, so a key change must be deliberate (bump RENDERER_VERSION and this value).
GOLDEN_DEFAULT_KEY = "60af652b8c0fdd42678d2c63aa264548"


def test_default_font_key_is_golden_and_other_fonts_change_it():
    d = cg.design_for("Middlemarch", "George Eliot", lang="en")
    assert d.font == "display" and d.key == GOLDEN_DEFAULT_KEY
    assert cg.design_for("Middlemarch", "George Eliot", lang="en", font="display").key == d.key
    keys = {
        f: cg.design_for("Middlemarch", "George Eliot", lang="en", font=f).key for f in cg.FONTS
    }
    assert len(set(keys.values())) == len(cg.FONTS)


def test_unknown_font_is_refused():
    with pytest.raises(ValueError):
        cg.design_for("Middlemarch", "George Eliot", font="comic")


@pytest.mark.parametrize("style", cg.STYLES)
@pytest.mark.parametrize("font", cg.FONTS)
def test_every_font_renders_in_every_style(font, style):
    d = cg.design_for("Middlemarch", "George Eliot", style=style, font=font)
    img = cg.render_png(d, width=200)
    assert img.startswith(b"\x89PNG")
    others = [
        cg.render_png(cg.design_for("Middlemarch", "George Eliot", style=style, font=f), width=200)
        for f in cg.FONTS
        if f != font
    ]
    assert img not in others  # the face actually changes the drawing
