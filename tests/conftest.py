from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
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


def csrf_of(client: TestClient) -> str:
    r = client.get("/api/me")
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def api_post(client: TestClient, url: str, csrf: str, json: dict | None = None, **kw):
    """A write from the SPA: same-origin Origin header plus the per-session CSRF token."""
    return client.post(
        url,
        json=json if json is not None else {},
        headers={"origin": ORIGIN, "x-csrf-token": csrf},
        **kw,
    )


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
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/app"), r.text
    return {"username": "owner", "csrf": csrf_of(client)}


def make_sync_key(client: TestClient, csrf: str, label: str) -> str:
    r = api_post(client, "/api/devices/sync-keys", csrf, {"label": label})
    assert r.status_code == 201, r.text
    return r.json()["secret"]


def make_sync_key_full(client: TestClient, csrf: str, label: str) -> dict:
    r = api_post(client, "/api/devices/sync-keys", csrf, {"label": label})
    assert r.status_code == 201, r.text
    return r.json()


def make_app_password(client: TestClient, csrf: str, label: str) -> str:
    r = api_post(client, "/api/devices/app-passwords", csrf, {"label": label})
    assert r.status_code == 201, r.text
    return r.json()["secret"]


def login(client: TestClient, username: str, password: str, totp: str = "") -> str:
    r = post_form(
        client,
        "/login",
        {"username": username, "password": password, "totp": totp},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return csrf_of(client)


def ko_headers(username: str, secret: str) -> dict[str, str]:
    """What KOReader sends: the username and md5(password)."""
    return {
        "x-auth-user": username,
        "x-auth-key": hashlib.md5(secret.encode(), usedforsecurity=False).hexdigest(),
    }


# --- audio fixtures ------------------------------------------------------------------------

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
MAGI = Path(__file__).parent / "fixtures" / "magi"


def make_mp3(
    path: Path,
    seconds: float = 2.0,
    *,
    title: str | None = None,
    album: str | None = None,
    artist: str | None = None,
    track: int | None = None,
    freq: int = 440,
) -> Path:
    """A short tagged MP3 (sine tone, so every file has a distinct hash)."""
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
         f"sine=frequency={freq}:duration={seconds}", "-ac", "1", "-b:a", "32k", str(path)],
        check=True,
    )  # fmt: skip
    from mutagen.easyid3 import EasyID3

    tags = EasyID3()
    for k, v in {"title": title, "album": album, "artist": artist}.items():
        if v:
            tags[k] = v
    if track:
        tags["tracknumber"] = str(track)
    tags.save(path)
    return path


def make_m4b(
    path: Path,
    chapters: list[tuple[str, float]],
    seconds: float,
    *,
    title: str,
    artist: str,
    comment: str | None = None,
) -> Path:
    """An AAC audiobook with QuickTime chapters, the way M4B files usually arrive."""
    meta = path.with_suffix(".ffmeta")
    lines = [";FFMETADATA1", f"title={title}", f"album={title}", f"artist={artist}"]
    if comment:
        lines.append(f"comment={comment}")
    for i, (name, start) in enumerate(chapters):
        end = chapters[i + 1][1] if i + 1 < len(chapters) else seconds
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start * 1000)}",
                  f"END={int(end * 1000)}", f"title={name}"]  # fmt: skip
    meta.write_text("\n".join(lines) + "\n")
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
         "-i", str(meta), "-map_metadata", "1", "-map_chapters", "1", "-c:a", "aac",
         "-b:a", "32k", "-f", "mp4", str(path)],
        check=True,
    )  # fmt: skip
    meta.unlink()
    return path


def drain_inbox(settings, *, settings_aware: bool = True) -> list[Path]:
    """Run the inbox watcher until nothing is left to ingest."""
    from lectern.db import session_factory
    from lectern.ingest.watcher import InboxWatcher

    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    w = InboxWatcher(
        settings.inbox_path,
        settings.library_dir,
        session_factory(),
        0.01,
        settings if settings_aware else None,
    )
    done: list[Path] = []
    for _ in range(4):
        done += w.scan_once()
    return done


def put_json(client: TestClient, url: str, csrf: str, json: dict, **kw):
    return client.put(url, json=json, headers={"origin": ORIGIN, "x-csrf-token": csrf}, **kw)


def api_delete(client: TestClient, url: str, csrf: str, **kw):
    return client.delete(url, headers={"origin": ORIGIN, "x-csrf-token": csrf}, **kw)


def make_user(client: TestClient, owner_csrf: str, username: str, role: str) -> dict:
    """Create a user as the owner; returns the creation payload plus the password."""
    password = f"{username}-correct horse"
    r = api_post(
        client,
        "/api/admin/users",
        owner_csrf,
        {
            "username": username,
            "display_name": username.title(),
            "role": role,
            "password": password,
        },
    )
    assert r.status_code == 201, r.text
    return {**r.json()["user"], "password": password}


@pytest.fixture(autouse=True)
def _reset_process_caches():
    """Per-process tallies keyed by ids that repeat across test databases."""
    from lectern.routes.api import works
    from lectern.security import downloads

    downloads._streamed.clear()
    works._rewind_cache.clear()
    yield
