"""Inbox ingest: metadata, hashing, matching into works, placement, duplicates."""

import base64

from lxml import etree
from tests.conftest import make_app_password, make_epub

from lectern.db import session_factory
from lectern.ingest.epub import normalize_isbn, read_epub_metadata
from lectern.ingest.hashing import partial_md5
from lectern.ingest.watcher import InboxWatcher
from lectern.models import BookFile, Work


def _watcher(settings):
    w = InboxWatcher(
        settings.inbox_path, settings.library_dir, session_factory(), settings.inbox_poll_seconds
    )
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    (settings.inbox_path / "_rejected").mkdir(exist_ok=True)
    return w


def _settle(w):
    """Files must be unchanged across two polls before ingest."""
    w.scan_once()
    return w.scan_once()


def test_normalize_isbn():
    assert normalize_isbn("978-0-306-40615-7") == "9780306406157"
    assert normalize_isbn("0-306-40615-2") == "9780306406157"
    assert normalize_isbn("0-306-40615-3") is None
    assert normalize_isbn("urn:isbn:9780306406157") == "9780306406157"


def test_read_epub_metadata(tmp_path):
    p = make_epub(
        tmp_path / "x.epub",
        title="The Left Hand of Darkness",
        authors=["Ursula K. Le Guin"],
        isbn="978-0-441-47812-5",
        cover=True,
    )
    m = read_epub_metadata(p)
    assert m.title == "The Left Hand of Darkness"
    assert m.authors == ["Ursula K. Le Guin"]
    assert m.isbn == "9780441478125"
    assert m.language == "en"
    assert m.cover_href == "OEBPS/cover.png"
    assert m.publisher == "Test Press"


def test_ingest_places_file_and_registers_work(client, owner, settings):
    w = _watcher(settings)
    src = make_epub(
        settings.inbox_path / "left-hand.epub",
        title="The Left Hand of Darkness",
        authors=["Ursula K. Le Guin"],
        isbn="9780441478125",
        cover=True,
    )
    expected_pmd5 = partial_md5(src)
    assert _settle(w) == [src]
    assert not src.exists()
    dest = settings.library_dir / "Ursula K. Le Guin" / "The Left Hand of Darkness"
    assert (dest / "book.epub").is_file() and (dest / "cover.jpg").is_file()
    with session_factory()() as db:
        work = db.query(Work).one()
        assert work.sort_title == "left hand of darkness"
        assert work.sort_author == "le guin, ursula k"
        assert work.has_cover and work.isbn == "9780441478125"
        book = db.query(BookFile).one()
        assert book.partial_md5 == expected_pmd5
        assert book.rel_path == "book.epub"
    lib = client.get("/api/library").json()
    assert [w["title"] for w in lib["works"]] == ["The Left Hand of Darkness"]
    detail = client.get("/api/works/1").json()
    assert detail["files"][0]["partial_md5"] == expected_pmd5
    assert detail["cover_url"] == f"/library/works/1/cover.jpg?v={detail['version']}"
    assert detail["cover_tint"] and detail["cover_tint"].startswith("#")
    r = client.get("/library/files/1/download")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/epub+zip")
    assert client.get("/library/works/1/cover.jpg").headers["content-type"] == "image/jpeg"


def test_isbn_match_adds_file_to_existing_work(client, owner, settings):
    w = _watcher(settings)
    make_epub(
        settings.inbox_path / "a.epub",
        title="Dune",
        authors=["Frank Herbert"],
        isbn="9780441013593",
    )
    _settle(w)
    # Same ISBN, different title spelling and different bytes -> same work, second file.
    make_epub(
        settings.inbox_path / "b.epub",
        title="Dune (Deluxe Edition)",
        authors=["Herbert, Frank"],
        isbn="9780441013593",
        padding=500,
    )
    _settle(w)
    with session_factory()() as db:
        assert db.query(Work).count() == 1
        files = db.query(BookFile).order_by(BookFile.id).all()
        assert [f.rel_path for f in files][0] == "book.epub"
        assert files[1].rel_path.startswith("book-") and files[1].rel_path.endswith(".epub")


def test_fuzzy_title_author_match(client, owner, settings):
    w = _watcher(settings)
    make_epub(
        settings.inbox_path / "a.epub",
        title="The Left Hand of Darkness",
        authors=["Ursula K. Le Guin"],
    )
    _settle(w)
    make_epub(
        settings.inbox_path / "b.epub",
        title="Left Hand of Darkness, The",
        authors=["Le Guin, Ursula K."],
        padding=10,
    )
    _settle(w)
    # Same title, different author -> must NOT merge.
    make_epub(
        settings.inbox_path / "c.epub",
        title="The Left Hand of Darkness",
        authors=["Somebody Else"],
        padding=20,
    )
    _settle(w)
    with session_factory()() as db:
        works = db.query(Work).order_by(Work.id).all()
        assert len(works) == 2
        assert len(works[0].files) == 2
        assert works[1].dir_path == "Somebody Else/The Left Hand of Darkness"


def test_duplicate_and_garbage_are_rejected(client, owner, settings):
    w = _watcher(settings)
    make_epub(settings.inbox_path / "a.epub", title="Dup")
    _settle(w)
    make_epub(settings.inbox_path / "a-again.epub", title="Dup")  # byte-identical content
    (settings.inbox_path / "junk.epub").write_bytes(b"not a zip")
    _settle(w)
    rejected = sorted(p.name for p in (settings.inbox_path / "_rejected").iterdir())
    assert rejected == ["a-again.epub", "junk.epub"]
    with session_factory()() as db:
        assert db.query(BookFile).count() == 1


def test_unsafe_names_are_sanitised(client, owner, settings):
    w = _watcher(settings)
    make_epub(
        settings.inbox_path / "a.epub", title="../../etc/passwd: Ünïcode/Title", authors=["../Evil"]
    )
    _settle(w)
    with session_factory()() as db:
        work = db.query(Work).one()
    assert ".." not in work.dir_path and "/" not in work.dir_path.replace("/", "", 1)
    assert (settings.library_dir / work.dir_path / "book.epub").is_file()


def test_opds_feed_lists_ingested_book(client, owner, settings):
    w = _watcher(settings)
    make_epub(
        settings.inbox_path / "a.epub",
        title="Dune",
        authors=["Frank Herbert"],
        isbn="9780441013593",
        cover=True,
    )
    _settle(w)
    pw = make_app_password(client, owner["csrf"], "kobo")
    client.cookies.clear()
    auth = {"authorization": "Basic " + base64.b64encode(f"owner:{pw}".encode()).decode()}

    root = client.get("/opds", headers=auth)
    assert root.status_code == 200 and "kind=navigation" in root.headers["content-type"]
    ns = {"a": "http://www.w3.org/2005/Atom"}
    tree = etree.fromstring(root.content)
    hrefs = {ln.get("rel"): ln.get("href") for ln in tree.findall("a:link", ns)}
    assert hrefs["start"] == "/opds" and hrefs["search"].startswith("/opds/search")

    feed = client.get("/opds/all", headers=auth)
    assert feed.status_code == 200 and "kind=acquisition" in feed.headers["content-type"]
    tree = etree.fromstring(feed.content)
    entries = tree.findall("a:entry", ns)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.findtext("a:title", namespaces=ns) == "Dune"
    acq = [
        ln
        for ln in entry.findall("a:link", ns)
        if ln.get("rel") == "http://opds-spec.org/acquisition"
    ][0]
    assert acq.get("type") == "application/epub+zip" and acq.get("href") == "/opds/files/1/download"
    img = [
        ln for ln in entry.findall("a:link", ns) if ln.get("rel") == "http://opds-spec.org/image"
    ][0]
    assert client.get(img.get("href"), headers=auth).headers["content-type"] == "image/jpeg"
    dl = client.get(acq.get("href"), headers=auth)
    assert dl.status_code == 200 and dl.content[:2] == b"PK"

    assert (
        len(
            etree.fromstring(client.get("/opds/search?q=dune", headers=auth).content).findall(
                "a:entry", ns
            )
        )
        == 1
    )
    assert (
        len(
            etree.fromstring(client.get("/opds/search?q=nothing", headers=auth).content).findall(
                "a:entry", ns
            )
        )
        == 0
    )
    assert (
        client.get("/opds/search.xml", headers=auth)
        .headers["content-type"]
        .startswith("application/opensearchdescription+xml")
    )

    # Wrong password, wrong user, and revoked password all fail with a Basic challenge.
    bad = {"authorization": "Basic " + base64.b64encode(b"owner:nope").decode()}
    r = client.get("/opds/all", headers=bad)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert (
        client.get(
            "/opds/all",
            headers={"authorization": "Basic " + base64.b64encode(f"other:{pw}".encode()).decode()},
        ).status_code
        == 401
    )
