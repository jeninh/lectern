"""Cover sources from the work's own files (EPUB images, PDF and comic pages), staging, and the
server-side crop: every input is untrusted and refused or bounded before it is kept."""

from __future__ import annotations

import io
import os
import time
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from tests.conftest import ORIGIN, OWNER_PASSWORD, drain_inbox, login, make_epub, make_user
from tests.test_edit_hardening import image_bytes, png_claiming
from tests.test_editing import editor, one_book, patch, png
from tests.test_pdf_comics import make_cbz, make_pdf, works

from lectern import editing
from lectern.db import session_factory
from lectern.ingest import epub as epub_mod
from lectern.ingest import formats
from lectern.models import BookFile, Work


@pytest.fixture(autouse=True)
def _inbox(settings):
    settings.inbox_path.mkdir(parents=True, exist_ok=True)


def hdr(owner) -> dict:
    return {"origin": ORIGIN, "x-csrf-token": owner["csrf"]}


def stage(client, owner, wid, body):
    return client.post(f"/api/works/{wid}/cover/stage", json=body, headers=hdr(owner))


def big_png(size=(600, 800), color=(30, 90, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def epub_with_images(path: Path, images: dict[str, tuple[str, bytes]]) -> Path:
    """An EPUB whose manifest lists ``images`` (zip name -> (media type, bytes))."""
    make_epub(path, title="Pictures", authors=["Illustrator"])
    items = "".join(
        f'<item id="i{n}" href="{name}" media-type="{mt}"/>'
        for n, (name, (mt, _)) in enumerate(images.items())
    )
    with zipfile.ZipFile(path) as zf:
        members = {i.filename: zf.read(i.filename) for i in zf.infolist()}
    opf = members["OEBPS/content.opf"].decode().replace("</manifest>", items + "</manifest>")
    members["OEBPS/content.opf"] = opf.encode()
    for name, (_mt, data) in images.items():
        members[f"OEBPS/{name}"] = data
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        for name, data in members.items():
            if name != "mimetype":
                zf.writestr(name, data)
    return path


def file_of(wid: int) -> BookFile:
    with session_factory()() as db:
        return db.query(BookFile).filter_by(work_id=wid).one()


def pdf_work(settings, client, **kw) -> tuple[int, int]:
    make_pdf(settings.inbox_path / "rivers.pdf", **kw)
    drain_inbox(settings)
    wid = works(client)["The Atlas of Small Rivers"]["id"]
    return wid, file_of(wid).id


def epub_work(settings, client, images) -> tuple[int, int]:
    epub_with_images(settings.inbox_path / "pics.epub", images)
    drain_inbox(settings)
    wid = works(client)["Pictures"]["id"]
    return wid, file_of(wid).id


# --- EPUB images ------------------------------------------------------------------------------


def test_list_images_never_lists_svg_and_skips_oversized_and_hostile(tmp_path, monkeypatch):
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    book = epub_with_images(
        tmp_path / "b.epub",
        {
            "a.png": ("image/png", big_png()),
            "logo.svg": ("image/svg+xml", svg),
            "big.png": ("image/png", big_png() + b"\0" * 5000),
            "b.gif": ("image/gif", image_bytes("GIF")),
        },
    )
    with zipfile.ZipFile(book) as zf:
        assert any(n.endswith("logo.svg") for n in zf.namelist())  # it is in the archive
    monkeypatch.setattr(epub_mod, "MAX_COVER_BYTES", 5000)
    listed = epub_mod.list_images(book)
    names = [i.href.rsplit("/", 1)[1] for i in listed]
    assert names == ["a.png", "b.gif"] and [i.index for i in listed] == [0, 1]
    assert all(i.media_type != "image/svg+xml" and not i.href.endswith(".svg") for i in listed)
    assert all(i.size <= 5000 for i in listed)
    # Extraction only ever goes through the listing: the SVG has no index.
    assert epub_mod.read_image(book, len(listed)) is None
    assert epub_mod.read_image(book, -1) is None


def test_list_images_is_capped_at_200(tmp_path):
    imgs = {f"i{n}.png": ("image/png", image_bytes("PNG")) for n in range(205)}
    assert len(epub_mod.list_images(epub_with_images(tmp_path / "many.epub", imgs))) == 200


def test_epub_image_thumbnail_stage_and_crop(client, owner, settings):
    wid, fid = epub_work(settings, client, {"a.png": ("image/png", big_png())})
    r = client.get(f"/api/works/{wid}/cover/sources/epub/{fid}/0.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert max(Image.open(io.BytesIO(r.content)).size) <= 360
    r = stage(client, owner, wid, {"source": "epub_image", "file_id": fid, "index": 0})
    assert r.status_code == 200, r.text
    assert (r.json()["width"], r.json()["height"]) == (600, 800)
    token = r.json()["token"]
    # Crop a staged image: the result is a new token, the rectangle's size in pixels.
    r = stage(client, owner, wid, {"source": "staged", "token": token,
              "crop": {"x": 0.0, "y": 0.5, "w": 0.5, "h": 0.5}})  # fmt: skip
    assert r.status_code == 200, r.text
    assert (r.json()["width"], r.json()["height"]) == (300, 400)
    assert r.json()["token"] != token
    got = client.get(r.json()["preview_url"])
    assert Image.open(io.BytesIO(got.content)).size == (300, 400)


def test_epub_thumbnail_decodes_a_large_jpeg_reduced(client, owner, settings, monkeypatch):
    from PIL import JpegImagePlugin

    buf = io.BytesIO()
    Image.new("RGB", (4000, 3000), (200, 40, 40)).save(buf, "JPEG")
    wid, fid = epub_work(settings, client, {"big.jpg": ("image/jpeg", buf.getvalue())})
    seen = []
    real = JpegImagePlugin.JpegImageFile.draft

    def spy(self, mode, size):
        out = real(self, mode, size)
        seen.append(self.size)  # the size the decoder will now produce
        return out

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", spy)
    r = client.get(f"/api/works/{wid}/cover/sources/epub/{fid}/0.jpg")
    assert r.status_code == 200
    assert max(Image.open(io.BytesIO(r.content)).size) <= 360
    assert seen and max(seen[0]) < 4000  # decoded at a fraction of the 12 MP, not in full


def test_epub_index_out_of_range_and_other_works_file_are_404(client, owner, settings):
    wid, fid = epub_work(settings, client, {"a.png": ("image/png", big_png())})
    for idx in (1, 99, -1):
        r = client.get(f"/api/works/{wid}/cover/sources/epub/{fid}/{idx}.jpg")
        assert r.status_code == 404, idx
        r = stage(client, owner, wid, {"source": "epub_image", "file_id": fid, "index": idx})
        assert r.status_code == 404, idx
    other = one_book(settings, title="Other", authors=["Someone"])
    assert stage(client, owner, other, {"source": "epub_image", "file_id": fid, "index": 0}).status_code == 404  # fmt: skip
    assert client.get(f"/api/works/{other}/cover/sources/epub/{fid}/0.jpg").status_code == 404
    assert stage(client, owner, wid, {"source": "epub_image", "file_id": 99999, "index": 0}).status_code == 404  # fmt: skip


def test_epub_gif_is_accepted_and_a_bomb_is_refused(client, owner, settings):
    wid, fid = epub_work(
        settings,
        client,
        {
            "g.gif": ("image/gif", image_bytes("GIF")),
            "bomb.png": ("image/png", png_claiming(50_000, 50_000)),
            "junk.png": ("image/png", b"not an image at all"),
        },
    )
    assert stage(client, owner, wid, {"source": "epub_image", "file_id": fid, "index": 0}).status_code == 200  # fmt: skip
    r = stage(client, owner, wid, {"source": "epub_image", "file_id": fid, "index": 1})
    assert (r.status_code, r.json()["detail"]) == (413, "cover_too_large")
    r = stage(client, owner, wid, {"source": "epub_image", "file_id": fid, "index": 2})
    assert (r.status_code, r.json()["detail"]) == (422, "not_an_image")
    assert client.get(f"/api/works/{wid}/cover/sources/epub/{fid}/1.jpg").status_code == 413


# --- upload hardening ---------------------------------------------------------------------------


def test_upload_body_over_10_mib_is_413_before_buffering(client, owner, settings):
    wid = one_book(settings, title="Big", authors=["Writer"])
    h = hdr(owner)
    r = client.post(f"/api/works/{wid}/cover", content=b"\xff" * (10 * 1024 * 1024 + 1), headers=h)
    assert r.status_code == 413
    # A lying/declared Content-Length is refused from the header alone.
    r = client.post(
        f"/api/works/{wid}/cover",
        content=b"x",
        headers={**h, "content-length": str(11 * 1024 * 1024)},
    )
    assert r.status_code in (400, 413)


def test_non_image_uploads_are_422_not_an_image(client, owner, settings):
    wid = one_book(settings, title="Junk", authors=["Writer"])
    pdf = make_pdf(settings.inbox_path.parent / "x.pdf").read_bytes()
    for name, data in {
        "gif": image_bytes("GIF"),
        "svg": b"<svg xmlns='http://www.w3.org/2000/svg' width='9' height='9'/>",
        "html": b"<html><script>alert(1)</script></html>",
        "pdf": pdf,
    }.items():
        r = client.post(f"/api/works/{wid}/cover", content=data, headers=hdr(owner))
        assert (r.status_code, r.json()["detail"]) == (422, "not_an_image"), name
    assert not list(editing.staged_covers_dir(settings).glob("*"))  # nothing was kept


def test_bomb_header_is_413_and_truncated_jpeg_is_422(client, owner, settings):
    wid = one_book(settings, title="Bomb", authors=["Writer"])
    r = client.post(f"/api/works/{wid}/cover", content=png_claiming(50_000, 50_000), headers=hdr(owner))  # fmt: skip
    assert (r.status_code, r.json()["detail"]) == (413, "cover_too_large")
    buf = io.BytesIO()
    Image.new("RGB", (400, 600), (5, 5, 5)).save(buf, "JPEG")
    noisy = io.BytesIO()
    Image.effect_noise((400, 600), 80).convert("RGB").save(noisy, "JPEG")
    cut = noisy.getvalue()[: len(noisy.getvalue()) // 2]
    r = client.post(f"/api/works/{wid}/cover", content=cut, headers=hdr(owner))
    assert (r.status_code, r.json()["detail"]) == (422, "not_an_image")
    assert not list(editing.staged_covers_dir(settings).glob("*"))


# --- tokens ---------------------------------------------------------------------------------------


def test_foreign_traversal_and_malformed_tokens_are_404_even_as_crop_source(
    client, owner, settings
):
    wid = one_book(settings, title="Tokens", authors=["Writer"])
    token = client.post(f"/api/works/{wid}/cover", content=png(), headers=hdr(owner)).json()[
        "token"
    ]
    crop = {"x": 0, "y": 0, "w": 1, "h": 1}
    # Another user's token is a 404 for the stage call.
    make_user(client, owner["csrf"], "mem", "member")
    mcsrf = login(client, "mem", "mem-correct horse")
    r = client.post(f"/api/works/{wid}/cover/stage", json={"source": "staged", "token": token},
                    headers={"origin": ORIGIN, "x-csrf-token": mcsrf})  # fmt: skip
    assert r.status_code == 404 and r.json()["detail"] == "cover_not_found"
    csrf = login(client, "owner", OWNER_PASSWORD)
    owner = {**owner, "csrf": csrf}
    uid = token.split(".")[0]
    for bad in (
        f"{uid}.../../etc/passwd",
        f"{uid}.%2e%2e%2f%2e%2e%2fx",
        f"{uid}.{'a' * 24}/../../{token}",
        f"../{token}",
        token + ".json",
        token + "\n",  # `$` would let a trailing newline through
        token.upper(),
        "",
        "x" * 64,
        f"{uid}.{'0' * 24}",  # well-formed but absent
    ):
        for body in (
            {"source": "staged", "token": bad},
            {"source": "staged", "token": bad, "crop": crop},
        ):
            r = client.post(f"/api/works/{wid}/cover/stage", json=body, headers=hdr(owner))
            assert r.status_code in (404, 422), (bad, r.status_code)
            assert r.status_code != 200
        assert client.get(f"/api/staged-covers/{bad.replace(chr(10), '%0A')}.jpg").status_code in (
            404,
            422,
        )


def test_stage_request_shape_is_strict(client, owner, settings):
    wid = one_book(settings, title="Shape", authors=["Writer"])
    for body in (
        {"source": "page", "file_id": 1},  # missing page
        {"source": "page", "file_id": 1, "page": 1, "token": "x"},  # extra field for source
        {"source": "staged"},
        {"source": "epub_image", "file_id": 1, "index": 0, "path": "../../etc/passwd"},
        {"source": "file", "token": "x"},
    ):
        assert stage(client, owner, wid, body).status_code == 422, body


# --- crop ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "crop",
    [
        {"x": -0.1, "y": 0, "w": 0.5, "h": 0.5},
        {"x": 0.6, "y": 0, "w": 0.5, "h": 0.5},  # leaves the image on the right
        {"x": 0, "y": 0.6, "w": 0.5, "h": 0.5},
        {"x": 0, "y": 0, "w": 0, "h": 0.5},
        {"x": 0, "y": 0, "w": -0.5, "h": 0.5},
        {"x": 0, "y": 0, "w": 0.2, "h": 1},  # 120 px wide: under the 200 px minimum
        {"x": 0, "y": 0, "w": 1, "h": 0.2},  # 160 px tall
        {"x": 0, "y": 0, "w": 1e308, "h": 1e308},
        {"x": 0, "y": 0, "w": 1, "h": 1.001},
    ],
)
def test_bad_crops_are_422_bad_crop(client, owner, settings, crop):
    wid = one_book(settings, title="Crop", authors=["Writer"])
    token = client.post(f"/api/works/{wid}/cover", content=big_png(), headers=hdr(owner)).json()[
        "token"
    ]
    r = stage(client, owner, wid, {"source": "staged", "token": token, "crop": crop})
    assert (r.status_code, r.json()["detail"]) == (422, "bad_crop"), crop
    # The source token is untouched by a refused crop.
    assert stage(client, owner, wid, {"source": "staged", "token": token}).status_code == 200


def test_non_finite_crop_is_refused(client, owner, settings):
    wid = one_book(settings, title="Nan", authors=["Writer"])
    token = client.post(f"/api/works/{wid}/cover", content=big_png(), headers=hdr(owner)).json()[
        "token"
    ]
    r = client.post(
        f"/api/works/{wid}/cover/stage",
        content=f'{{"source":"staged","token":"{token}","crop":{{"x":NaN,"y":0,"w":1,"h":1}}}}',
        headers={**hdr(owner), "content-type": "application/json"},
    )
    assert r.status_code == 422


# --- PDF and comic pages --------------------------------------------------------------------------


def test_pdf_page_stage_and_save_records_page_source(client, owner, settings):
    wid, fid = pdf_work(settings, client, color=(10, 120, 200))
    r = stage(client, owner, wid, {"source": "page", "file_id": fid, "page": 1})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    ed = editor(client, wid)
    r = patch(client, owner["csrf"], wid, {"version": ed["version"], "cover": {"action": "upload", "token": token}})  # fmt: skip
    assert r.status_code == 200, r.text
    with session_factory()() as db:
        w = db.get(Work, wid)
        assert w.has_cover and w.cover_source == "page"
        from lectern.models import WorkRevision

        rev = db.query(WorkRevision).filter_by(work_id=wid).order_by(WorkRevision.id.desc()).first()
        assert rev.after["cover"]["origin"] == {"source": "page", "file_id": fid, "page": 1}
    cover = client.get(f"/library/works/{wid}/cover.jpg")
    red, green, blue = Image.open(io.BytesIO(cover.content)).convert("RGB").getpixel((5, 5))
    assert blue > 150 and red < 60  # the blue first page, not a white one
    # The token and its sidecar are consumed by the save.
    assert not list(editing.staged_covers_dir(settings).glob("*"))


def test_upload_token_keeps_source_upload_and_cropped_page_keeps_origin(client, owner, settings):
    wid, fid = pdf_work(settings, client)
    first = stage(client, owner, wid, {"source": "page", "file_id": fid, "page": 1}).json()
    cropped = stage(client, owner, wid, {"source": "staged", "token": first["token"],
                    "crop": {"x": 0, "y": 0, "w": 1, "h": 0.5}})  # fmt: skip
    assert cropped.status_code == 200, cropped.text
    ed = editor(client, wid)
    r = patch(client, owner["csrf"], wid, {"version": ed["version"],
              "cover": {"action": "upload", "token": cropped.json()["token"]}})  # fmt: skip
    assert r.status_code == 200
    with session_factory()() as db:
        assert db.get(Work, wid).cover_source == "page"
    plain = client.post(f"/api/works/{wid}/cover", content=png(), headers=hdr(owner)).json()[
        "token"
    ]
    ed = editor(client, wid)
    patch(client, owner["csrf"], wid, {"version": ed["version"], "cover": {"action": "upload", "token": plain}})  # fmt: skip
    with session_factory()() as db:
        assert db.get(Work, wid).cover_source == "upload"


def test_origin_of_another_work_is_ignored_on_save(client, owner, settings):
    wid, fid = pdf_work(settings, client)
    other = one_book(settings, title="Other", authors=["Someone"])
    token = stage(client, owner, wid, {"source": "page", "file_id": fid, "page": 1}).json()["token"]
    ed = editor(client, other)
    r = patch(client, owner["csrf"], other, {"version": ed["version"], "cover": {"action": "upload", "token": token}})  # fmt: skip
    assert r.status_code == 200
    with session_factory()() as db:
        assert db.get(Work, other).cover_source == "upload"


def test_pdf_page_bounds_and_foreign_file(client, owner, settings):
    wid, fid = pdf_work(settings, client, pages=3)
    for page in (0, -1, 4, 10**9):
        r = stage(client, owner, wid, {"source": "page", "file_id": fid, "page": page})
        assert (r.status_code, r.json()["detail"]) == (422, "bad_page"), page
    assert (
        stage(client, owner, wid, {"source": "page", "file_id": fid, "page": 3}).status_code == 200
    )
    other = one_book(settings, title="Other", authors=["Someone"])
    assert (
        stage(client, owner, other, {"source": "page", "file_id": fid, "page": 1}).status_code
        == 404
    )
    # An EPUB file is not a page source.
    with session_factory()() as db:
        epub_id = db.query(BookFile).filter_by(work_id=other).one().id
    r = stage(client, owner, other, {"source": "page", "file_id": epub_id, "page": 1})
    assert (r.status_code, r.json()["detail"]) == (422, "wrong_format")


def test_pdf_busy_is_503_and_encrypted_pdf_is_422(client, owner, settings, monkeypatch):
    wid, fid = pdf_work(settings, client)

    def busy(*a, **k):
        raise formats.ProbeBusy()

    monkeypatch.setattr("lectern.pdf2epub.probe.render", busy)
    r = stage(client, owner, wid, {"source": "page", "file_id": fid, "page": 1})
    assert (r.status_code, r.json()["detail"]) == (503, "pdf_busy")
    monkeypatch.undo()
    # Swap the stored file for an encrypted PDF: the child reports pdf_encrypted.
    with session_factory()() as db:
        w = db.get(Work, wid)
        path = settings.library_dir / w.dir_path / db.get(BookFile, fid).rel_path
    make_pdf(path, password="secret")
    r = stage(client, owner, wid, {"source": "page", "file_id": fid, "page": 1})
    assert (r.status_code, r.json()["detail"]) == (422, "pdf_encrypted")


def test_comic_page_stage(client, owner, settings, tmp_path):
    make_cbz(settings.inbox_path / "c.cbz", pages=3, cover_color=(0, 160, 60))
    drain_inbox(settings)
    with session_factory()() as db:
        wid = db.query(Work).one().id
    fid = file_of(wid).id
    for page in (0, 4):
        r = stage(client, owner, wid, {"source": "page", "file_id": fid, "page": page})
        assert (r.status_code, r.json()["detail"]) == (422, "bad_page"), page
    r = stage(client, owner, wid, {"source": "page", "file_id": fid, "page": 1})
    assert r.status_code == 200, r.text
    got = Image.open(io.BytesIO(client.get(r.json()["preview_url"]).content)).convert("RGB")
    red, green, _ = got.getpixel((5, 5))
    assert green > 120 and red < 40


def test_readers_cannot_stage_sources(client, owner, settings):
    wid, fid = pdf_work(settings, client)
    kid = make_user(client, owner["csrf"], "kid", "reader")
    rcsrf = login(client, "kid", kid["password"])
    r = client.post(f"/api/works/{wid}/cover/stage", json={"source": "page", "file_id": fid, "page": 1},
                    headers={"origin": ORIGIN, "x-csrf-token": rcsrf})  # fmt: skip
    assert r.status_code == 403


# --- sweep and caps ---------------------------------------------------------------------------------


def test_sweep_deletes_stale_sidecars_and_covers_but_keeps_fresh_ones(client, owner, settings):
    wid, fid = pdf_work(settings, client)
    token = stage(client, owner, wid, {"source": "page", "file_id": fid, "page": 1}).json()["token"]
    d = editing.staged_covers_dir(settings)
    fresh = [d / f"{token}.jpg", d / f"{token}.json"]
    assert all(p.is_file() for p in fresh)
    stale = [d / "1.aaaaaaaaaaaaaaaaaaaaaaaa.jpg", d / "1.aaaaaaaaaaaaaaaaaaaaaaaa.json"]
    for p in stale:
        p.write_text("{}")
        old = time.time() - editing.STAGED_COVER_TTL_S - 60
        os.utime(p, (old, old))
    with session_factory()() as db:
        editing.sweep(db, settings)
    assert not any(p.exists() for p in stale)
    assert all(p.is_file() for p in fresh)


def test_staged_covers_per_user_are_capped(settings, monkeypatch):
    monkeypatch.setattr(editing, "STAGED_PER_USER_MAX", 3)
    tokens = [editing.stage_cover(settings, 7, png()) for _ in range(5)]
    left = {p.stem for p in editing.staged_covers_dir(settings).glob("7.*.jpg")}
    assert len(left) == 3 and tokens[-1] in left


def test_token_with_trailing_newline_is_not_a_token(settings):
    token = f"7.{'a' * 24}"
    (editing.staged_covers_dir(settings) / f"{token}\n.jpg").write_bytes(b"x")
    with pytest.raises(editing.EditError) as exc:
        editing.staged_cover_path(settings, 7, token + "\n")
    assert exc.value.status == 404
