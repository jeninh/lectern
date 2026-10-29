"""More ways in (issue #67): saving a web article as an EPUB work, and email-in by IMAP polling."""

from __future__ import annotations

import base64
import gzip
import http.server
import io
import logging
import os
import socket
import subprocess
import threading
import zipfile
from email import policy
from email.message import EmailMessage
from pathlib import Path

import pytest
from PIL import Image
from tests.conftest import ORIGIN, api_post, make_epub

from lectern import egress, email_in
from lectern.article import build, fetch
from lectern.article import jobs as article_jobs
from lectern.config import Settings
from lectern.db import session_factory
from lectern.jobs import run_pending
from lectern.models import BookFile, Job, Work

PASSWORD = "not-a-real-imap-password"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        base_url=ORIGIN,
        insecure_http=True,
        inbox_poll_seconds=0.05,
        email_in="1",
        email_in_host="imap.example.net",
        email_in_user="books@example.net",
        email_in_password=PASSWORD,
    )


def png(color=(200, 40, 40), size=(320, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


ARTICLE_URL = "https://news.example.com/2026/lighthouse"
PAGE = b"""<!doctype html><html lang="en"><head><title>The Keeper | Example Times</title>
<meta property="og:title" content="The Lighthouse Keeper">
<meta property="og:site_name" content="Example Times">
<meta name="author" content="Ada Writer">
<meta property="article:published_time" content="2026-03-04T10:00:00Z">
<meta property="og:image" content="/img/lead.png">
<script>document.write('tracking')</script></head>
<body><nav class="menu"><a href="/">Home</a> <a href="/news">News</a></nav>
<div class="share-bar"><a href="https://social.example/share">Share this story</a></div>
<div class="layout"><article class="post"><h1>The Lighthouse Keeper</h1>
<p>The keeper climbed the stairs every evening, counting each of the one hundred and twelve
steps, as his father had done before him.</p>
<p>From the gallery he could see the whole bay, the fishing boats, the harbour wall, and the
town, its lights coming on one by one.</p>
<figure><img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" data-src="/img/bay.png" alt="The bay"
onerror="steal()"><figcaption>The bay at dusk</figcaption></figure>
<p>He wrote down the weather, the ships, and anything unusual, in a ledger that went back
<a href="/history">two hundred years</a> <a href="javascript:steal()">(more)</a>.</p>
<iframe src="https://ads.example/frame"></iframe><form action="/subscribe"><input name="email"></form>
<object data="/x.swf"></object><p onclick="steal()" style="background:url(https://t.example/p)">
Nothing unusual had happened in a long time, which was, he thought, exactly how it
should be, <svg onload="steal()"><script>steal()</script></svg>most of the time.</p>
</article>
<aside class="related"><a href="/other">Another story you might like</a></aside></div>
<div id="comments"><p>First! What a lovely story, thank you so much for writing it, really.</p></div>
<footer>Copyright Example Times</footer></body></html>"""


def fake_web(pages: dict[str, tuple[str, bytes]], calls: list | None = None):
    """A stand-in for lectern.article.fetch.get: ``pages`` maps URL -> (media type, body)."""

    def get(url, *, max_bytes, accept, deadline):
        if calls is not None:
            calls.append(url)
        if url not in pages:
            raise fetch.FetchError("http_error", 404)
        media, data = pages[url]
        if len(data) > max_bytes:
            raise fetch.FetchError("too_large")
        return fetch.Response(url, media, None, data)

    return get


def standard_web() -> dict[str, tuple[str, bytes]]:
    return {
        ARTICLE_URL: ("text/html", PAGE),
        "https://news.example.com/img/lead.png": ("image/png", png((20, 60, 120))),
        "https://news.example.com/img/bay.png": ("image/png", png((200, 120, 40))),
    }


def epubcheck(path: Path) -> None:
    jar = os.environ.get("EPUBCHECK_JAR")
    if not jar:
        if os.environ.get("EPUBCHECK_REQUIRED"):
            pytest.fail("EPUBCHECK_REQUIRED is set but EPUBCHECK_JAR is not")
        pytest.skip("set EPUBCHECK_JAR to run epubcheck")
    res = subprocess.run(
        ["java", "-jar", jar, str(path)], capture_output=True, text=True, timeout=120
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "0 errors / 0 warnings" in res.stdout, res.stdout


# --- fetching: public addresses only ------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/admin",
        "http://10.0.0.5/",
        "http://192.168.1.20:8080/opds",
        "http://100.100.100.100/",  # Tailscale's resolver (CGNAT range: every tailnet address)
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[fd7a:115c:a1e0::1]/",  # Tailscale IPv6
        "http://[::ffff:127.0.0.1]/",
        "http://[64:ff9b::a00:1]/",  # NAT64 to 10.0.0.1
        "http://[2002:c0a8:101::1]/",  # 6to4 of 192.168.1.1
        "http://0.0.0.0/",
    ],
)
def test_private_and_tailnet_addresses_are_refused(url):
    with pytest.raises(fetch.FetchError) as exc:
        fetch.check_url(url)
    assert exc.value.code == "blocked_address"


def test_a_name_that_resolves_to_loopback_is_refused():
    with pytest.raises(fetch.FetchError) as exc:
        fetch.public_addresses("localhost", 80)
    assert exc.value.code == "blocked_address"


@pytest.mark.parametrize(
    "url", ["ftp://example.com/x", "https://user:pw@example.com/", "file:///etc/passwd", "x"]
)
def test_only_plain_http_urls_are_fetched(url):
    with pytest.raises(fetch.FetchError) as exc:
        fetch.check_url(url)
    assert exc.value.code == "bad_url"


def test_a_name_with_any_private_answer_is_refused(monkeypatch):
    answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
               (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 443))]  # fmt: skip
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answers)
    with pytest.raises(fetch.FetchError) as exc:
        fetch.public_addresses("mixed.example", 443)
    assert exc.value.code == "blocked_address"


def test_the_checked_address_is_the_one_connected_to(monkeypatch):
    """DNS rebinding: the name is resolved once per hop, and the connection goes to that
    vetted address; a second answer (now private) is never asked for or used."""
    import time

    answers = iter(["93.184.216.34", "127.0.0.1"])
    lookups, connects = [], []

    def resolve(host, port, *a, **k):
        lookups.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port))]

    def connect(address, timeout=None, *a, **k):
        connects.append(address)
        raise ConnectionRefusedError("test")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "create_connection", connect)
    with pytest.raises(fetch.FetchError) as exc:
        fetch.get(
            "http://rebind.example/", max_bytes=100, accept="*/*", deadline=time.monotonic() + 5
        )
    assert exc.value.code == "unreachable"
    assert lookups == ["rebind.example"] and connects == [("93.184.216.34", 80)]


@pytest.fixture
def local_site(monkeypatch):
    """A real HTTP server on loopback that fetch may reach (only for these tests)."""
    routes: dict[str, tuple[int, dict, bytes]] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            status, headers, body = routes.get(self.path, (404, {}, b""))
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real = fetch.public_addresses
    # Only the test site's own name reaches loopback; every other name is checked for real.
    monkeypatch.setattr(
        fetch, "public_addresses",
        lambda host, port: ["127.0.0.1"] if host == "site.test" else real(host, port),
    )  # fmt: skip
    yield f"http://site.test:{server.server_port}", routes
    server.shutdown()


def test_fetch_follows_redirects_and_checks_each_hop(local_site):
    import time

    base, routes = local_site
    routes["/old"] = (301, {"Location": "/new"}, b"")
    routes["/new"] = (200, {"Content-Type": "text/html; charset=utf-8"}, b"<p>hi</p>")
    routes["/sneaky"] = (302, {"Location": "http://127.0.0.1:1/secret"}, b"")
    routes["/big"] = (200, {"Content-Type": "text/html"}, b"x" * 5000)
    routes["/to-name"] = (302, {"Location": "http://localhost:1/admin"}, b"")
    routes["/bomb"] = (
        200,
        {"Content-Type": "text/html", "Content-Encoding": "gzip"},
        gzip.compress(b"x" * 200_000),
    )  # 200 KB from a few hundred bytes
    routes["/loop"] = (302, {"Location": "/loop"}, b"")
    deadline = time.monotonic() + 10
    got = fetch.get(f"{base}/old", max_bytes=1000, accept="text/html", deadline=deadline)
    assert (got.url, got.media_type, got.charset, got.data) == (
        f"{base}/new", "text/html", "utf-8", b"<p>hi</p>",
    )  # fmt: skip
    for path, code in (
        ("/sneaky", "blocked_address"),
        ("/to-name", "blocked_address"),
        ("/big", "too_large"),
        ("/bomb", "too_large"),
        ("/loop", "too_many_redirects"),
    ):
        with pytest.raises(fetch.FetchError) as exc:
            fetch.get(f"{base}{path}", max_bytes=1000, accept="text/html", deadline=deadline)
        assert exc.value.code == code
    with pytest.raises(fetch.FetchError) as exc:
        fetch.get(f"{base}/missing", max_bytes=1000, accept="text/html", deadline=deadline)
    assert (exc.value.code, exc.value.status) == ("http_error", 404)


# --- extracting and building ----------------------------------------------------------------


def test_extract_keeps_the_article_and_drops_the_page_around_it():
    a = build.extract(PAGE, ARTICLE_URL)
    assert (a.title, a.byline, a.site, a.published, a.language) == (
        "The Lighthouse Keeper", "Ada Writer", "Example Times", "2026-03-04", "en",
    )  # fmt: skip
    assert a.lead_image == "https://news.example.com/img/lead.png"
    assert a.image_urls == ["https://news.example.com/img/bay.png"]  # the lazy image, not the gif
    text = a.content.text_content()
    assert "one hundred and twelve" in text and "exactly how it" in text
    for junk in ("Share this story", "Another story", "First!", "Copyright", "tracking", "News"):
        assert junk not in text
    links = [x.get("href") for x in a.content.iter("a")]
    assert "https://news.example.com/history" in links


def test_a_page_without_an_article_is_refused():
    with pytest.raises(build.ArticleError):
        build.extract(b"<html><body><div id='app'></div><script>x()</script></body></html>", ARTICLE_URL)  # fmt: skip


def test_normalise_image_refuses_non_images_and_reencodes():
    assert build.normalise_image(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is None
    assert build.normalise_image(png(size=(1, 1))) is None  # a tracking pixel
    data, media = build.normalise_image(png(size=(4000, 1000)))
    assert media == "image/jpeg"
    assert Image.open(io.BytesIO(data)).size == (1600, 400)


# --- the article job, end to end -------------------------------------------------------------


def _save(client, owner, settings, url=ARTICLE_URL, web=None, calls=None, monkeypatch=None):
    r = api_post(client, "/api/articles", owner["csrf"], {"url": url})
    assert r.status_code == 202, r.text
    monkeypatch.setattr(fetch, "get", fake_web(web or standard_web(), calls))
    assert run_pending(session_factory(), settings) == 1
    return r.json()["id"]


def test_saved_article_becomes_an_epub_work_on_opds(client, owner, settings, monkeypatch):
    job_id = _save(client, owner, settings, monkeypatch=monkeypatch)
    state = client.get("/api/articles").json()
    assert state["enabled"] is True
    row = next(j for j in state["jobs"] if j["id"] == job_id)
    assert row["status"] == "succeeded", row
    assert (row["title"], row["images"], row["skipped_images"]) == ("The Lighthouse Keeper", 1, 0)
    with session_factory()() as db:
        work = db.get(Work, row["work_id"])
        assert (work.title, work.author, work.has_cover) == (
            "The Lighthouse Keeper", "Ada Writer", True,
        )  # fmt: skip
        assert "Article" in (work.tags or [])
        book = db.query(BookFile).filter_by(work_id=work.id).one()
        epub = settings.library_dir / work.dir_path / book.rel_path
    with zipfile.ZipFile(epub) as zf:
        xhtml = zf.read("OEBPS/article.xhtml").decode()
        opf = zf.read("OEBPS/content.opf").decode()
    for active in ("steal", "<script", "<iframe", "<form", "<input", "<object", "onclick",
                   "onerror", "onload", "url(", "data:image", "ads.example", "t.example"):  # fmt: skip
        assert active not in xhtml, active
    assert 'src="images/' in xhtml and "<dc:source>https://news.example.com" in opf
    r = api_post(client, "/api/devices/app-passwords", owner["csrf"], {"label": "Kindle"})
    auth = {
        "authorization": "Basic "
        + base64.b64encode(f"owner:{r.json()['secret']}".encode()).decode()
    }
    feed = client.get("/opds/all", headers=auth)
    assert feed.status_code == 200 and "The Lighthouse Keeper" in feed.text
    assert "application/epub+zip" in feed.text


def test_saved_article_passes_epubcheck(client, owner, settings, monkeypatch):
    job_id = _save(client, owner, settings, monkeypatch=monkeypatch)
    with session_factory()() as db:
        work = db.get(Work, db.get(Job, job_id).result["work_id"])
        book = db.query(BookFile).filter_by(work_id=work.id).one()
        path = settings.library_dir / work.dir_path / book.rel_path
    epubcheck(path)


def test_the_same_article_twice_is_already_saved(client, owner, settings, monkeypatch):
    first = _save(client, owner, settings, monkeypatch=monkeypatch)
    second = _save(client, owner, settings, monkeypatch=monkeypatch)
    with session_factory()() as db:
        a, b = db.get(Job, first), db.get(Job, second)
        assert (b.status.value, b.error) == ("failed", "already_saved")
        assert b.result == {"work_id": a.result["work_id"]}
        assert db.query(Work).count() == 1


def test_images_beyond_the_cap_are_left_out(client, owner, settings, monkeypatch):
    monkeypatch.setattr(article_jobs, "MAX_IMAGES", 1)  # the cover takes the only slot
    job_id = _save(client, owner, settings, monkeypatch=monkeypatch)
    row = next(j for j in client.get("/api/articles").json()["jobs"] if j["id"] == job_id)
    assert (row["status"], row["images"], row["skipped_images"]) == ("succeeded", 0, 1)


def test_a_non_html_url_fails_with_its_reason(client, owner, settings, monkeypatch):
    web = {ARTICLE_URL: ("application/pdf", b"%PDF-1.7")}
    job_id = _save(client, owner, settings, web=web, monkeypatch=monkeypatch)
    row = next(j for j in client.get("/api/articles").json()["jobs"] if j["id"] == job_id)
    assert (row["status"], row["error"], row["work_id"]) == ("failed", "not_html", None)


def test_private_urls_are_refused_up_front(client, owner):
    r = api_post(client, "/api/articles", owner["csrf"], {"url": "http://192.168.1.1/"})
    assert (r.status_code, r.json()["detail"]) == (422, "blocked_address")


def test_article_saving_is_off_in_offline_mode(client, owner, settings, monkeypatch):
    r = api_post(client, "/api/articles", owner["csrf"], {"url": ARTICLE_URL})
    assert r.status_code == 202  # queued while online...
    egress.enforce(True)  # ...then offline mode goes on before it runs
    calls: list[str] = []
    monkeypatch.setattr(fetch, "get", fake_web(standard_web(), calls))
    assert run_pending(session_factory(), settings) == 1
    assert calls == []  # the network was never tried
    state = client.get("/api/articles").json()
    assert (state["enabled"], state["reason"]) == (False, "offline")
    assert state["jobs"][0]["error"] == "offline"
    r = api_post(client, "/api/articles", owner["csrf"], {"url": ARTICLE_URL})
    assert (r.status_code, r.json()["detail"]) == (409, "offline")
    features = {f["id"]: f for f in client.get("/api/admin/offline-mode").json()["features"]}
    assert features["article_saving"]["blocked"] is True
    assert features["email_in"]["blocked"] is True


# --- email-in ------------------------------------------------------------------------------------


class FakeIMAP:
    """The part of imaplib.IMAP4 the poller uses, over an in-memory mailbox with folders."""

    def __init__(self, messages: dict[int, bytes], uidvalidity: int = 7, move: bool = True):
        self.messages = dict(messages)  # what is still in the polled folder
        self.uidvalidity = uidvalidity
        self.capabilities = ("IMAP4REV1", "MOVE") if move else ("IMAP4REV1",)
        self.folders: dict[str, list[int]] = {}
        self.flags: dict[int, set[str]] = {}
        self.fetched: list[tuple[int, str]] = []
        self.logged_out = False

    def select(self, mailbox):
        return "OK", [str(len(self.messages)).encode()]

    def create(self, mailbox):
        name = mailbox.strip('"')
        if name in self.folders:
            return "NO", [b"[ALREADYEXISTS] Mailbox exists"]
        self.folders[name] = []
        return "OK", [b""]

    def response(self, code):
        return code, [str(self.uidvalidity).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            low = int(args[1].split()[1].split(":")[0])
            top = max(self.messages, default=0)
            uids = [u for u in sorted(self.messages) if u >= low or u == top]
            return "OK", [" ".join(map(str, uids)).encode()]
        uid = int(args[0])
        if command == "FETCH":
            what = args[1]
            self.fetched.append((uid, what))
            raw = self.messages[uid]
            if what == "(RFC822.SIZE)":
                return "OK", [f"{uid} (UID {uid} RFC822.SIZE {len(raw)})".encode()]
            part = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n" if "HEADER" in what else raw
            return "OK", [(f"{uid} (UID {uid} BODY[] {{{len(part)}}}".encode(), part), b")"]
        if command in ("MOVE", "COPY"):
            assert command == "COPY" or "MOVE" in self.capabilities
            self.folders[args[1].strip('"')].append(uid)
            if command == "MOVE":
                del self.messages[uid]
            return "OK", [b""]
        if command == "STORE":
            self.flags.setdefault(uid, set()).add(args[2])
            return "OK", [b""]
        raise AssertionError(command)

    def logout(self):
        self.logged_out = True


def mail(
    to: str,
    sender: str,
    attachments: list[tuple[str, bytes]],
    subject: str = "A book",
    auth_results: list[str] | None = None,
) -> bytes:
    msg = EmailMessage()
    for header in auth_results or []:
        msg["Authentication-Results"] = header
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content("Here it is.")
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    return msg.as_bytes(policy=policy.SMTP)


def put(client, url, csrf, json):
    return client.put(url, json=json, headers={"origin": ORIGIN, "x-csrf-token": csrf})


def set_filter(client, owner, mode="allowlist", entries=("alice@example.org",), ack=False):
    return put(client, "/api/email-in/senders", owner["csrf"],
               {"mode": mode, "entries": list(entries), "acknowledge_open": ack})  # fmt: skip


@pytest.fixture
def inbox(client, owner):
    """The owner's email-in address; the filter allows alice@example.org."""
    r = api_post(client, "/api/email-in/address", owner["csrf"])
    assert r.status_code == 200, r.text
    r = set_filter(client, owner, entries=["Alice <ALICE@Example.org>"])
    assert r.status_code == 200, r.text
    assert r.json()["filter"] == {
        "mode": "allowlist", "source": "app", "count": 1, "entries": ["alice@example.org"],
    }  # fmt: skip
    return r.json()["address"]


@pytest.fixture
def epub(tmp_path) -> bytes:
    return make_epub(tmp_path / "x.epub", title="Mailed Book", authors=["Pat"]).read_bytes()


def _poll(settings, imap):
    return email_in.poll_once(session_factory(), settings, connect=lambda _cfg: imap)


def test_an_emailed_epub_from_an_allowed_sender_waits_for_review(client, owner, settings, inbox, epub):  # fmt: skip
    assert inbox.startswith("books+") and inbox.endswith("@example.net")
    imap = FakeIMAP({3: mail(inbox, "Alice <alice@example.org>", [("Mailed Book.epub", epub)])})
    assert _poll(settings, imap) == {"accepted": 1}
    imps = client.get("/api/imports").json()["imports"]
    assert len(imps) == 1
    imp = imps[0]
    assert imp["status"] == "ready" and imp["metadata"]["title"] == "Mailed Book"
    assert [f["filename"] for f in imp["files"]] == ["Mailed Book.epub"]
    with session_factory()() as db:
        assert db.query(Work).count() == 0  # nothing is filed until the person decides
    notes = client.get("/api/notifications").json()["items"]
    assert notes[0]["kind"] == "email_in"
    assert imap.folders == {"Processed": [3], "Rejected": []} and imap.messages == {}
    assert imap.logged_out
    imap.messages[3] = b"never read again"  # a UID once handled is not handled twice
    assert _poll(settings, imap) == {}


@pytest.mark.parametrize(
    "case",
    ["stranger", "display_name_spoof", "spoofed_tag", "no_tag", "two_people", "two_senders",
     "dmarc_fail", "subdomain", "lookalike_domain"],
)  # fmt: skip
def test_mail_that_fails_a_check_is_rejected_unread(client, owner, settings, inbox, epub, case):
    domain = inbox.split("@")[1]
    to, sender, results = inbox, "alice@example.org", None
    if case == "stranger":
        sender = "mallory@example.org"
    elif case == "display_name_spoof":  # the name says alice; the address is what counts
        sender = '"alice@example.org" <mallory@evil.example>'
    elif case == "spoofed_tag":
        to = f"books+{'0' * 16}@{domain}"
    elif case == "no_tag":
        to = f"books@{domain}"
    elif case == "two_people":
        api_post(client, "/api/admin/users", owner["csrf"],
                 {"username": "mem", "role": "member", "password": "member password 123"})  # fmt: skip
        with session_factory()() as db:
            from lectern.models import EmailInbox, User

            mem = db.query(User).filter_by(username="mem").one()
            db.add(EmailInbox(user_id=mem.id, token="b" * 16))
            db.commit()
        to = f"{inbox}, books+{'b' * 16}@{domain}"
    elif case == "two_senders":
        sender = "alice@example.org, mallory@example.org"
    elif case == "dmarc_fail":  # the provider's own verdict on a forged From
        results = ["mx.example.net; spf=fail smtp.mailfrom=example.org; dmarc=fail header.from=example.org"]  # fmt: skip
    elif case == "subdomain":
        set_filter(client, owner, entries=["@example.org"])
        sender = "alice@mail.example.org"
    elif case == "lookalike_domain":
        set_filter(client, owner, entries=["@example.org"])
        sender = "alice@notexample.org"
    imap = FakeIMAP({1: mail(to, sender, [("x.epub", epub)], auth_results=results)})
    counts = _poll(settings, imap)
    assert sum(counts.values()) == 1 and "accepted" not in counts, counts
    assert client.get("/api/imports").json()["imports"] == []
    assert (1, "(BODY.PEEK[])") not in imap.fetched  # only the headers were read
    assert imap.folders["Rejected"] == [1]


def test_reject_reasons_are_logged_without_details(client, owner, settings, inbox, epub, caplog):
    imap = FakeIMAP({1: mail(inbox, "mallory@evil.example", [("x.epub", epub)])})
    with caplog.at_level(logging.INFO, logger="lectern.email_in"):
        assert _poll(settings, imap) == {"sender_not_allowed": 1}
    assert "rejected a message: sender_not_allowed" in caplog.text
    assert "mallory" not in caplog.text and "evil.example" not in caplog.text


def test_a_lower_authentication_results_header_is_not_trusted(client, owner, settings, inbox, epub):  # fmt: skip
    # Only the topmost header comes from the mailbox's servers; one below is the sender's.
    results = ["mx.example.net; dmarc=pass header.from=example.org", "evil; dmarc=fail"]
    imap = FakeIMAP({1: mail(inbox, "alice@example.org", [("x.epub", epub)], auth_results=results)})
    assert _poll(settings, imap) == {"accepted": 1}


def test_a_domain_entry_allows_the_whole_domain(client, owner, settings, inbox, epub):
    set_filter(client, owner, entries=["@Example.org"])
    imap = FakeIMAP({1: mail(inbox, "Bob <BOB@example.ORG>", [("x.epub", epub)])})
    assert _poll(settings, imap) == {"accepted": 1}


def test_blocklist_mode_is_an_explicit_opt_in(client, owner, settings, inbox, epub, tmp_path):
    r = set_filter(client, owner, mode="blocklist", entries=["@spam.example"])
    assert (r.status_code, r.json()["detail"]) == (400, "confirm_blocklist")
    r = set_filter(client, owner, mode="blocklist", entries=["@spam.example", "eve@x.example"], ack=True)  # fmt: skip
    assert r.status_code == 200 and r.json()["filter"]["mode"] == "blocklist"
    other = make_epub(tmp_path / "y.epub", title="Another").read_bytes()
    imap = FakeIMAP({
        1: mail(inbox, "stranger@anywhere.example", [("x.epub", epub)]),
        2: mail(inbox, "Promo <deals@SPAM.example>", [("y.epub", other)]),
        3: mail(inbox, "eve@x.example", [("y.epub", other)]),
    })  # fmt: skip
    assert _poll(settings, imap) == {"accepted": 1, "sender_blocked": 2}
    assert (imap.folders["Processed"], imap.folders["Rejected"]) == ([1], [2, 3])


def test_a_filter_set_in_env_wins_and_is_read_only(client, owner, settings, inbox, epub):
    settings.email_in_senders = "@example.org, bob@other.example"
    state = client.get("/api/email-in").json()
    assert state["filter"] == {"mode": "allowlist", "source": "env", "count": 2}  # no entries
    r = set_filter(client, owner, entries=["x@y.example"])
    assert (r.status_code, r.json()["detail"]) == (409, "set_in_env")
    imap = FakeIMAP({1: mail(inbox, "carol@example.org", [("x.epub", epub)])})
    assert _poll(settings, imap) == {"accepted": 1}


def test_oversized_mail_and_mail_without_books(client, owner, settings, inbox):
    settings.email_in_max_mb = "1"
    big = mail(inbox, "alice@example.org", [("big.epub", b"PK\x03\x04" + b"0" * 2 * 1024 * 1024)])
    notes = mail(inbox, "alice@example.org", [("notes.txt", b"hello"), ("fake.epub", b"not a zip")])
    imap = FakeIMAP({1: big, 2: notes}, move=False)  # a server without MOVE: copied, kept
    assert _poll(settings, imap) == {"too_large": 1, "no_books": 1}
    assert [f for f in imap.fetched if f[0] == 1] == [(1, "(RFC822.SIZE)")]
    assert client.get("/api/imports").json()["imports"] == []
    assert imap.folders["Rejected"] == [1, 2] and set(imap.messages) == {1, 2}  # never deleted
    assert imap.flags == {1: {"(\\Seen)"}, 2: {"(\\Seen)"}}


def test_mail_the_parser_chokes_on_is_rejected_and_does_not_block_the_rest(client, owner, settings, inbox, epub, caplog):  # fmt: skip
    # Anyone can send this (no tag needed): reading its To header raises IndexError inside
    # the stdlib email parser. It must not stop the allowed mail queued behind it.
    hostile = mail(inbox, "alice@example.org", [("x.epub", epub)]).replace(
        f"To: {inbox}".encode(), rb"To: <b, :b<,\@<\[c@ab(;=<b(]"
    )
    imap = FakeIMAP({1: hostile, 2: mail(inbox, "alice@example.org", [("Mailed Book.epub", epub)])})  # fmt: skip
    with caplog.at_level("INFO", logger="lectern"):
        assert _poll(settings, imap) == {"malformed": 1, "accepted": 1}
    assert imap.folders == {"Processed": [2], "Rejected": [1]} and imap.messages == {}
    assert "IndexError" in caplog.text and "c@ab" not in caplog.text
    assert _poll(settings, imap) == {}  # passed for good, not retried


def test_slow_drip_responses_are_cut_at_the_deadline(monkeypatch):
    """A server that sends a byte every 0.2 s never trips a per-recv timeout; the deadline
    must still end the request, whether it drips the headers or the body."""
    import socket as socketlib
    import time

    def serve(conn, head_fast: bool):
        try:
            conn.recv(4096)
            head = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 100000\r\n\r\n"
            if head_fast:
                conn.sendall(head)
                head = b"x" * 100000
            for byte in head:
                conn.sendall(bytes([byte]))
                time.sleep(0.2)
        except OSError:
            pass
        finally:
            conn.close()

    for head_fast in (True, False):
        listener = socketlib.create_server(("127.0.0.1", 0))
        port = listener.getsockname()[1]

        def accept(listener=listener, head_fast=head_fast):
            conn, _ = listener.accept()
            serve(conn, head_fast)

        threading.Thread(target=accept, daemon=True).start()
        monkeypatch.setattr(fetch, "public_addresses", lambda host, port: ["127.0.0.1"])
        started = time.monotonic()
        with pytest.raises(fetch.FetchError) as exc:
            fetch.get(f"http://drip.test:{port}/", max_bytes=1_000_000, accept="text/html",
                      deadline=started + 1.0)  # fmt: skip
        elapsed = time.monotonic() - started
        listener.close()
        assert exc.value.code == "timeout", head_fast
        assert elapsed < 1.6, (head_fast, elapsed)


def _shelf(client, owner, name, parent=None) -> int:
    r = api_post(client, "/api/shelves", owner["csrf"], {"name": name, "parent_id": parent})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_emailed_books_go_on_the_chosen_shelf(client, owner, settings, inbox, epub):
    parent = _shelf(client, owner, "Inboxes")
    shelf = _shelf(client, owner, "Inbox (email)", parent)
    r = put(client, "/api/email-in/shelf", owner["csrf"], {"shelf_id": shelf})
    assert r.json()["shelf"] == {"id": shelf, "name": "Inboxes / Inbox (email)"}
    assert _poll(settings, FakeIMAP({1: mail(inbox, "alice@example.org", [("x.epub", epub)])})) == {"accepted": 1}  # fmt: skip
    imp = client.get("/api/imports").json()["imports"][0]
    assert imp["shelf_id"] == shelf
    r = api_post(client, f"/api/imports/{imp['id']}/file", owner["csrf"], {})
    assert r.status_code == 200, r.text
    work_id = r.json()["work_id"]
    assert r.json()["report"]["shelf"] == shelf
    with session_factory()() as db:
        from lectern.models import ShelfItem

        assert db.get(ShelfItem, (shelf, work_id)) is not None


def test_email_in_destination_is_never_a_group_or_generated_shelf(client, owner, inbox):
    group = api_post(client, "/api/shelves", owner["csrf"],
                     {"name": "Authors", "auto": {"field": "author", "min_books": 1}}).json()  # fmt: skip
    r = put(client, "/api/email-in/shelf", owner["csrf"], {"shelf_id": group["id"]})
    assert r.status_code == 422 and r.json()["detail"] == "smart_shelf"


def test_a_deleted_shelf_falls_back_to_no_shelf_with_a_notice(client, owner, settings, inbox, epub, tmp_path):  # fmt: skip
    shelf = _shelf(client, owner, "Inbox (email)")
    put(client, "/api/email-in/shelf", owner["csrf"], {"shelf_id": shelf})
    assert _poll(settings, FakeIMAP({1: mail(inbox, "alice@example.org", [("x.epub", epub)])})) == {"accepted": 1}  # fmt: skip
    imp = client.get("/api/imports").json()["imports"][0]
    r = client.delete(f"/api/shelves/{shelf}", headers={"origin": ORIGIN, "x-csrf-token": owner["csrf"]})  # fmt: skip
    assert r.status_code in (200, 204), r.text
    # Filing still works: the book is filed, on no shelf, and the person is told why.
    r = api_post(client, f"/api/imports/{imp['id']}/file", owner["csrf"], {})
    assert r.status_code == 200, r.text
    with session_factory()() as db:
        from lectern.models import ShelfItem

        assert db.query(ShelfItem).count() == 0
    kinds = [n["kind"] for n in client.get("/api/notifications").json()["items"]]
    assert "email_in_shelf" in kinds
    state = client.get("/api/email-in").json()
    assert (state["shelf"], state["shelf_missing"]) == (None, True)
    # New mail meanwhile is imported without a shelf instead of failing.
    other = make_epub(tmp_path / "y.epub", title="Another").read_bytes()
    assert _poll(settings, FakeIMAP({2: mail(inbox, "alice@example.org", [("y.epub", other)])}, uidvalidity=8)) == {"accepted": 1}  # fmt: skip
    newest = client.get("/api/imports").json()["imports"][0]
    assert newest["shelf_id"] is None


def test_missing_or_invalid_keys_keep_it_off_and_are_named(client, owner, settings, caplog):
    settings.email_in_host = ""
    settings.email_in_port = "imaps"
    state = client.get("/api/email-in").json()
    assert (state["enabled"], state["reason"]) == (False, "not_configured")
    assert state["missing"] == ["LECTERN_EMAIL_IN_HOST", "LECTERN_EMAIL_IN_PORT"]
    r = api_post(client, "/api/email-in/address", owner["csrf"])
    assert (r.status_code, r.json()["detail"]) == (409, "not_configured")
    assert email_in.poll_once(session_factory(), settings, connect=pytest.fail) == {}
    with caplog.at_level(logging.WARNING, logger="lectern.email_in"):
        assert email_in.start(session_factory(), settings) is None
    assert "LECTERN_EMAIL_IN_HOST, LECTERN_EMAIL_IN_PORT" in caplog.text
    settings.email_in = ""
    assert client.get("/api/email-in").json()["reason"] == "off"
    # And a server started with such a .env starts normally.
    from lectern.main import create_app

    broken = settings.model_copy(update={"email_in": "1", "email_in_port": "x", "email_in_senders": "not an address"})  # fmt: skip
    create_app(broken, start_watcher=False, start_background=False)


def test_email_in_parsing_reads_delivery_headers(settings):
    cfg, _ = email_in.load(settings)
    msg = EmailMessage()
    msg["To"] = "undisclosed-recipients:;"
    msg["Delivered-To"] = "Books+ABCDEF0123456789@Example.NET"
    msg["From"] = '"Alice" <ALICE@example.org>'
    assert email_in.recipient_tokens(msg, cfg) == {"abcdef0123456789"}
    assert email_in.sender_of(msg) == "alice@example.org"


def test_email_in_is_off_in_offline_mode(client, owner, settings, inbox):
    state = client.get("/api/email-in").json()
    assert (state["enabled"], state["reason"], state["address"]) == (True, None, inbox)
    egress.enforce(True)
    assert email_in.poll_once(session_factory(), settings, connect=pytest.fail) == {}
    state = client.get("/api/email-in").json()
    assert (state["enabled"], state["reason"]) == (False, "offline")
    assert state["last_poll"]["error"] == "offline"


def test_a_new_address_retires_the_old_one(client, owner, settings, inbox, epub):
    r = api_post(client, "/api/email-in/address", owner["csrf"])
    assert r.json()["address"] != inbox
    imap = FakeIMAP({1: mail(inbox, "alice@example.org", [("x.epub", epub)])})
    assert _poll(settings, imap) == {"unknown_address": 1}


def test_email_in_logs_no_secrets_or_addresses(client, owner, settings, inbox, epub, caplog):
    import imaplib

    def refused(_cfg):
        raise imaplib.IMAP4.error(f"LOGIN failed for {settings.email_in_user} {PASSWORD}")

    imap = FakeIMAP({1: mail(inbox, "Alice <alice@example.org>", [("Secret Title.epub", epub)], subject="Private subject")})  # fmt: skip
    with caplog.at_level(logging.DEBUG):
        email_in.poll_once(session_factory(), settings, connect=refused)
        _poll(settings, imap)
    logged = caplog.text
    assert "could not sign in" in logged and "1 accepted" in logged
    for secret in (PASSWORD, "alice@example.org", inbox, "Secret Title", "Private subject",
                   "books@example.net", "imap.example.net"):  # fmt: skip
        assert secret not in logged
    assert PASSWORD not in repr(settings) and PASSWORD not in repr(email_in.load(settings)[0])
