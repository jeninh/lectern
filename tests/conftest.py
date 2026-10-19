from __future__ import annotations

import hashlib
import logging
import re
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lectern.config import Settings
from lectern.main import create_app

ORIGIN = "http://testserver"
OWNER_PASSWORD = "correct horse battery staple"


def make_epub(
    path: Path,
    title: str = "A Test Book",
    authors: list[str] | None = None,
    isbn: str | None = None,
    *,
    cover: bool = False,
    padding: int = 0,
    language: str = "en",
) -> Path:
    """Build a minimal but valid EPUB 3 with the given OPF metadata."""
    authors = authors if authors is not None else ["Test Author"]
    creators = "".join(f'<dc:creator id="c{i}">{a}</dc:creator>' for i, a in enumerate(authors))
    ident = f'<dc:identifier id="isbn">urn:isbn:{isbn}</dc:identifier>' if isbn else ""
    manifest_cover = (
        '<item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/>'
        if cover
        else ""
    )
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:{hashlib.md5(title.encode(), usedforsecurity=False).hexdigest()}</dc:identifier>
    {ident}
    <dc:title>{title}</dc:title>
    {creators}
    <dc:language>{language}</dc:language>
    <dc:description>About {title}.</dc:description>
    <dc:publisher>Test Press</dc:publisher>
    <dc:date>2020-01-02</dc:date>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    {manifest_cover}
  </manifest>
  <spine><itemref idref="ch1"/></spine>
</package>"""
    container = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
    body = "<p>Hello.</p>" + ("<p>" + "x" * padding + "</p>" if padding else "")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED
        )
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr(
            "OEBPS/nav.xhtml",
            "<html xmlns='http://www.w3.org/1999/xhtml'><body><nav epub:type='toc' xmlns:epub='http://www.idpf.org/2007/ops'><ol><li><a href='ch1.xhtml'>1</a></li></ol></nav></body></html>",
        )
        zf.writestr(
            "OEBPS/ch1.xhtml",
            f"<html xmlns='http://www.w3.org/1999/xhtml'><body>{body}</body></html>",
        )
        if cover:
            from io import BytesIO

            from PIL import Image

            buf = BytesIO()
            Image.new("RGB", (40, 60), (200, 30, 30)).save(buf, "PNG")
            zf.writestr("OEBPS/cover.png", buf.getvalue())
    return path


class _TokenCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.token: str | None = None

    def emit(self, record: logging.LogRecord) -> None:
        m = re.search(r"/setup\?token=(\S+)", record.getMessage())
        if m:
            self.token = m.group(1)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        base_url=ORIGIN,
        insecure_http=True,
        inbox_poll_seconds=0.05,
    )


@pytest.fixture
def app(settings: Settings):
    return create_app(settings, start_watcher=False)


@pytest.fixture
def setup_token(app):
    cap = _TokenCapture()
    logging.getLogger("lectern.setup").addHandler(cap)
    yield cap
    logging.getLogger("lectern.setup").removeHandler(cap)


@pytest.fixture
def client(app, setup_token):
    with TestClient(app) as c:
        c.setup_token = setup_token.token  # type: ignore[attr-defined]
        yield c


def post_form(client: TestClient, url: str, data: dict, **kw):
    return client.post(url, data=data, headers={"origin": ORIGIN}, **kw)


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


def secret_of(html: str) -> str:
    m = re.search(r'class="secret">([^<]+)<', html)
    assert m, "no secret in page"
    return m.group(1)


@pytest.fixture
def owner(client: TestClient):
    """Complete first-run setup; returns a dict with the csrf token for the logged-in owner."""
    r = post_form(
        client,
        "/setup",
        {
            "token": client.setup_token,
            "username": "owner",
            "password": OWNER_PASSWORD,
            "password_confirm": OWNER_PASSWORD,
        },
    )
    assert r.status_code == 200, r.text
    return {"username": "owner", "csrf": csrf_of(client.get("/account").text)}


def make_sync_key(client: TestClient, csrf: str, label: str) -> str:
    r = post_form(client, "/account/sync-keys", {"label": label, "csrf_token": csrf})
    assert r.status_code == 200, r.text
    return secret_of(r.text)


def make_app_password(client: TestClient, csrf: str, label: str) -> str:
    r = post_form(client, "/account/app-passwords", {"label": label, "csrf_token": csrf})
    assert r.status_code == 200, r.text
    return secret_of(r.text)


def ko_headers(username: str, secret: str) -> dict[str, str]:
    """What KOReader sends: the username and md5(password)."""
    return {
        "x-auth-user": username,
        "x-auth-key": hashlib.md5(secret.encode(), usedforsecurity=False).hexdigest(),
    }
