"""Optional metadata suggestions: off by default, owner-controlled, fixed upstreams, limits,
cache, provenance, and never a silent write. Upstreams are faked; nothing leaves the test."""

from __future__ import annotations

import json

import pytest
from tests.conftest import ORIGIN, api_post, drain_inbox, login, make_epub, make_user, put_json

from lectern import lookup
from lectern.db import session_factory
from lectern.models import AuditLog

OL_SEARCH = {
    "docs": [
        {
            "key": "/works/OL59863W",
            "title": "The Left Hand of Darkness",
            "author_name": ["Ursula K. Le Guin"],
            "first_publish_year": 1969,
            "publisher": ["Ace Books", "Gollancz"],
            "isbn": ["0441478123", "9780441478125"],
            "language": ["eng"],
            "subject": ["Science fiction", "Gender"],
            "series": ["Hainish Cycle"],
        },
        {"key": "javascript:alert(1)", "title": "Bad key"},
    ]
}
OL_WORK = {"description": {"type": "/type/text", "value": "Genly Ai is sent to <i>Gethen</i>."}}
GB = {
    "items": [
        {
            "id": "pKBdEAAAQBAJ",
            "volumeInfo": {
                "title": "The Left Hand of Darkness",
                "authors": ["Ursula K. Le Guin"],
                "publisher": "Penguin",
                "publishedDate": "2019-09-03",
                "description": "<p><b>Winner</b> of the Hugo &amp; Nebula awards.</p>",
                "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780441478125"}],
                "language": "en",
                "categories": ["Fiction"],
            },
        }
    ]
}


class FakeUpstream:
    def __init__(self, fail: dict[str, str] | None = None):
        self.urls: list[str] = []
        self.fail = fail or {}

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        for host, code in self.fail.items():
            if host in url:
                raise lookup.LookupError_(code)
        if url.startswith("https://openlibrary.org/search.json"):
            return json.dumps(OL_SEARCH).encode()
        if url == "https://openlibrary.org/works/OL59863W.json":
            return json.dumps(OL_WORK).encode()
        if url.startswith("https://www.googleapis.com/books/v1/volumes"):
            return json.dumps(GB).encode()
        raise AssertionError(f"unexpected upstream {url}")


@pytest.fixture(autouse=True)
def _fresh():
    lookup.clear_cache()
    yield
    lookup.clear_cache()


@pytest.fixture
def fake(app):
    f = FakeUpstream()
    app.state.lookup_fetch = f
    return f


def enable(client, csrf, on=True):
    r = put_json(client, "/api/library-settings", csrf, {"metadata_lookup_enabled": on})
    assert r.status_code == 200, r.text


def test_off_by_default_and_only_the_owner_turns_it_on(client, owner, fake):
    csrf = owner["csrf"]
    assert client.get("/api/me").json()["library_settings"]["metadata_lookup_enabled"] is False
    r = api_post(client, "/api/metadata/lookup", csrf, {"isbn": "9780441478125"})
    assert r.status_code == 403 and r.json()["detail"] == "lookup_disabled"
    assert fake.urls == []  # nothing left the server
    make_user(client, csrf, "mem", "member")
    mcsrf = login(client, "mem", "mem-correct horse")
    assert (
        put_json(
            client, "/api/library-settings", mcsrf, {"metadata_lookup_enabled": True}
        ).status_code
        == 403
    )
    assert client.get("/api/library-settings").json() == {
        "settings": {"metadata_lookup_enabled": False, "data_saver_enabled": True},
        "can_change": False,
    }
    csrf = login(client, "owner", "correct horse battery staple")
    enable(client, csrf)
    with session_factory()() as db:
        row = db.query(AuditLog).filter_by(action="settings.change").one()
        assert (row.target, row.detail) == ("metadata_lookup_enabled", "on")


def test_isbn_lookup_sends_only_the_isbn_and_normalises_suggestions(client, owner, fake, settings):
    csrf = owner["csrf"]
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    make_epub(settings.inbox_path / "b.epub", title="Left Hand", authors=["U. Le Guin"])
    drain_inbox(settings)
    before = client.get("/api/works/1").json()
    enable(client, csrf)
    r = api_post(
        client, "/api/metadata/lookup", csrf, {"isbn": "0-441-47812-3", "title": "Left Hand"}
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["sent"] == {"isbn": "9780441478125"}
    # Fixed origins, the ISBN as the only query value, no title or author sent.
    assert all(
        u.startswith(("https://openlibrary.org/", "https://www.googleapis.com/")) for u in fake.urls
    )
    assert all("Left" not in u for u in fake.urls) and any("9780441478125" in u for u in fake.urls)
    ol = [s for s in out["suggestions"] if s["provider"] == "openlibrary"]
    gb = [s for s in out["suggestions"] if s["provider"] == "googlebooks"]
    assert (
        ol[0]["source_url"] == "https://openlibrary.org/works/OL59863W"
        and ol[0]["provider_name"] == "Open Library"
    )
    f = ol[0]["fields"]
    assert f["language"] == "en" and f["isbn"] == "9780441478125" and f["published"] == "1969"
    assert f["series"] == "Hainish Cycle" and f["description"] == "Genly Ai is sent to Gethen."
    assert f["authors"] == [{"name": "Ursula K. Le Guin", "sort": None}]
    assert ol[1]["source_url"] == "https://openlibrary.org/"  # an odd key never becomes a link
    g = gb[0]["fields"]
    assert (
        g["description"] == "Winner of the Hugo & Nebula awards." and g["published"] == "2019-09-03"
    )
    assert gb[0]["source_url"] == "https://books.google.com/books?id=pKBdEAAAQBAJ"
    # Suggestions only: the work is untouched until a person applies fields in the editor.
    assert client.get("/api/works/1").json()["title"] == before["title"]
    with session_factory()() as db:
        row = db.query(AuditLog).filter_by(action="metadata.lookup").one()
        assert "by isbn" in row.detail and "Left" not in (row.detail + (row.target or ""))


def test_cache_limits_and_per_provider_errors(client, owner, app):
    csrf = owner["csrf"]
    enable(client, csrf)
    fake = FakeUpstream(fail={"googleapis": "timeout"})
    app.state.lookup_fetch = fake
    body = {"title": "The Left Hand of Darkness", "author": "Le Guin"}
    out = api_post(client, "/api/metadata/lookup", csrf, body).json()
    assert [e for e in out["errors"]] == [{"provider": "googlebooks", "error": "timeout"}]
    assert out["suggestions"] and out["sent"] == body
    n = len(fake.urls)
    out = api_post(
        client, "/api/metadata/lookup", csrf, {**body, "providers": ["openlibrary"]}
    ).json()
    assert out["cached"] is True and len(fake.urls) == n  # served from the cache
    for _ in range(8):
        api_post(client, "/api/metadata/lookup", csrf, body)
    r = api_post(client, "/api/metadata/lookup", csrf, body)
    assert r.status_code == 429 and r.json()["detail"] == "rate_limited"
    assert api_post(client, "/api/metadata/lookup", csrf, {"author": "x"}).status_code == 422
    assert api_post(client, "/api/metadata/lookup", csrf, {"isbn": "123"}).status_code == 422
    assert (
        api_post(
            client, "/api/metadata/lookup", csrf, {"title": "x", "providers": ["evil"]}
        ).status_code
        == 422
    )
    assert (
        client.post("/api/metadata/lookup", json=body, headers={"origin": ORIGIN}).status_code
        == 403
    )


class _Resp:
    def __init__(self, body: bytes, ctype="application/json", status=200, length=None):
        self.body, self.status = body, status
        self.headers = {"content-type": ctype}
        if length is not None:
            self.headers["content-length"] = str(length)

    def read(self, n):
        out, self.body = self.body[:n], self.body[n:]
        return out

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_transport_refuses_other_hosts_big_bodies_and_non_json(monkeypatch):
    with pytest.raises(lookup.LookupError_) as e:
        lookup.http_get("https://evil.example/search.json")
    assert e.value.code == "bad_upstream"
    with pytest.raises(lookup.LookupError_):
        lookup.http_get("https://openlibrary.org.evil.example/x")
    with pytest.raises(lookup.LookupError_):
        lookup.http_get("http://openlibrary.org/search.json")  # https only

    class Opener:
        def __init__(self, resp):
            self.resp, self.timeout = resp, None

        def open(self, req, timeout):
            self.timeout = timeout
            assert req.full_url.startswith("https://openlibrary.org/")
            return self.resp

    big = Opener(_Resp(b"x" * (lookup.MAX_BODY_BYTES + 10)))
    monkeypatch.setattr(lookup, "_OPENER", big)
    with pytest.raises(lookup.LookupError_) as e:
        lookup.http_get("https://openlibrary.org/search.json?q=x")
    assert e.value.code == "too_large" and big.timeout == lookup.TIMEOUT_S
    monkeypatch.setattr(lookup, "_OPENER", Opener(_Resp(b"{}", length=lookup.MAX_BODY_BYTES * 5)))
    with pytest.raises(lookup.LookupError_) as e:
        lookup.http_get("https://openlibrary.org/search.json?q=x")
    assert e.value.code == "too_large"
    monkeypatch.setattr(lookup, "_OPENER", Opener(_Resp(b"<html>", ctype="text/html")))
    with pytest.raises(lookup.LookupError_) as e:
        lookup.http_get("https://openlibrary.org/search.json?q=x")
    assert e.value.code == "bad_response"
    monkeypatch.setattr(lookup, "_OPENER", Opener(_Resp(b'{"docs": []}')))
    assert lookup.http_get("https://openlibrary.org/search.json?q=x") == b'{"docs": []}'
    # Redirects are refused rather than followed.
    with pytest.raises(lookup.LookupError_) as e:
        lookup._NoRedirect().redirect_request(
            None, None, 302, "Found", {}, "http://169.254.169.254/"
        )
    assert e.value.code == "redirect_refused"


def test_malformed_upstream_json_is_an_error_not_a_crash():
    q = lookup.Query(title="x")
    for body in (
        b"not json",
        b"[1,2]",
        json.dumps({"docs": "nope"}).encode(),
        json.dumps({"items": [1, {"volumeInfo": 5}]}).encode(),
    ):
        try:
            res = lookup.lookup(q, lambda url, b=body: b)
        finally:
            lookup.clear_cache()
        assert res["suggestions"] == []
