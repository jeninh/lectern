"""Library filters and smart shelves (lectern.filters, docs/filters.md).

The library below is built row by row, so every expected set comes from how it was built,
never from the code under test. Rex is a reader who sees the "Shared" shelf (and "Inner"
inside it); everything else is hidden from him.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from fastapi.testclient import TestClient
from tests.conftest import (
    ORIGIN,
    OWNER_PASSWORD,
    api_post,
    ko_headers,
    login,
    make_sync_key,
    make_user,
)
from tests.test_mcp import MCP, Person

from lectern import filters
from lectern.db import make_engine, session_factory
from lectern.migrate import alembic_config, upgrade_to_head
from lectern.models import (
    Alignment,
    AlignmentStatus,
    AppPassword,
    AudioTrack,
    BookFile,
    DeviceQueueItem,
    FileKind,
    GeneratedAudiobook,
    Import,
    ImportStatus,
    PdfText,
    ProgressSource,
    QueueStatus,
    ReadingProgress,
    ReadingState,
    User,
    Work,
    WorkProgress,
)

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "filters.json").read_text())["cases"]
MB = 1024**2
EPUB, PDF, CBZ = filters.EPUB, filters.PDF, filters.CBZ


def patch(client, url: str, csrf: str, json: dict):
    return client.patch(url, json=json, headers={"origin": ORIGIN, "x-csrf-token": csrf})


@pytest.mark.parametrize("case", VECTORS, ids=[c["input"] or "<empty>" for c in VECTORS])
def test_syntax_vectors(case):
    """The cases the web app's parser is held to as well (frontend/src/lib/filters.ts)."""
    if "error" in case:
        with pytest.raises(filters.FilterError) as exc:
            filters.parse(case["input"])
        assert exc.value.code == case["error"]
    else:
        out = filters.format_query(filters.parse(case["input"]))
        assert out == case["canonical"]
        assert filters.format_query(filters.parse(out)) == out  # canonical is a fixed point


def test_field_catalog_matches_the_web_app():
    """frontend/src/lib/filters.ts hard-codes the same catalog; both are held to this file."""
    catalog = json.loads((Path(__file__).parent / "fixtures" / "filter_fields.json").read_text())
    assert catalog["fields"] == filters.describe()
    assert catalog["sorts"] == [
        {"key": k, "label": lab, "desc": d} for k, (lab, d) in filters.SORTS.items()
    ]
    assert catalog["aliases"] == {
        k: {c: list(a) for c, (_lab, a) in f.options.items()}
        for k, f in filters.FIELDS.items()
        if f.options
    }


def test_size_limits():
    with pytest.raises(filters.FilterError, match="at most 1000"):
        filters.parse("x" * 1001)
    with pytest.raises(filters.FilterError) as exc:
        filters.parse(" ".join(f"year:{1900 + i}" for i in range(25)))
    assert exc.value.code == "too_many"
    with pytest.raises(filters.FilterError):
        filters.parse("author:" + ",".join(str(i) for i in range(21)))


# --- the library -------------------------------------------------------------------------------

_n = iter(range(1, 10_000))


def _file(work, media_type, size_mb, pages=None):
    n = next(_n)
    return BookFile(
        work_id=work.id, kind=FileKind.EBOOK, rel_path=f"book{n}", media_type=media_type,
        size=int(size_mb * MB), sha256=f"{n:064x}", partial_md5=f"{n:032x}", pages=pages,
    )  # fmt: skip


def _audio(work, hours, size_mb, tracks=1):
    return [
        AudioTrack(
            work_id=work.id,
            position=i,
            rel_path=f"audio/{i}.mp3",
            media_type="audio/mpeg",
            codec="mp3",
            duration_s=hours * 3600 / tracks,
            offset_s=i * hours * 3600 / tracks,
            size=int(size_mb * MB / tracks),
            sha256=f"a{next(_n):063x}",
        )  # fmt: skip
        for i in range(tracks)
    ]


@pytest.fixture
def lib(client, owner, app):
    csrf = owner["csrf"]
    mia = make_user(client, csrf, "mia", "member")
    rex = make_user(client, csrf, "rex", "reader")
    shared = api_post(client, "/api/shelves", csrf, {"name": "Shared"}).json()["id"]
    inner = api_post(client, "/api/shelves", csrf, {"name": "Inner", "parent_id": shared}).json()[
        "id"
    ]
    private = api_post(client, "/api/shelves", csrf, {"name": "Private"}).json()["id"]
    r = client.put(
        f"/api/shelves/{shared}/grants/{rex['id']}",
        json={"permission": "view"},
        headers={"origin": ORIGIN, "x-csrf-token": csrf},
    )
    assert r.status_code == 200, r.text
    key = make_sync_key(client, csrf, "Kobo")
    pw = api_post(client, "/api/devices/app-passwords", csrf, {"label": "Kindle"}).json()
    now = datetime.now(UTC)
    with session_factory()() as db:
        owner_id = db.scalar(sa.select(User.id).where(User.username == "owner"))

        def work(title, author, *, created, **kw):
            w = Work(
                title=title, sort_title=title.lower(), author=author, sort_author=author.lower(),
                dir_path=f"{author}/{title}/{next(_n)}", created_at=created, **kw,
            )  # fmt: skip
            db.add(w)
            db.flush()
            return w

        d = lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC)  # noqa: E731
        war = work("War and Peace", "Leo Tolstoy", created=d("2026-01-10"), language="ru",
                   published="1869-01-01", series="Epics", series_index=1,
                   tags=["Classic", "Russian"], publisher="Russkii Vestnik", has_cover=True,
                   narrators=["Frederick Davidson"], authors=[{"name": "Leo Tolstoy"}])  # fmt: skip
        anna = work("Anna Karenina", "Lev Tolstoï", created=d("2026-02-01"), language="ru-RU",
                    published="1878", tags=["classic"])  # fmt: skip
        moby = work("Moby-Dick", "Herman Melville", created=d("2026-03-01"), language="en",
                    published="1851")  # fmt: skip
        moby2 = work("Moby-Dick", "herman  melville", created=d("2026-03-02"), language="en",
                     published="1851")  # fmt: skip
        scan = work("Ledger", "Unknown Author", created=d("2026-04-01"))
        comic = work("Spaceman", "Ann Artist", created=d("2026-05-01"), series="Spaceman",
                     series_index=2, has_cover=True)  # fmt: skip
        night = work("Night Sounds", "Ann Artist", created=now - timedelta(days=3),
                     narrators=["Jim Dale"])  # fmt: skip
        files = {
            "war": _file(war, EPUB, 2),
            "anna": _file(anna, EPUB, 1),
            "moby": _file(moby, EPUB, 1.5),
            "moby2": _file(moby2, EPUB, 1),
            "scan": _file(scan, PDF, 30, pages=120),
            "comic": _file(comic, CBZ, 50, pages=24),
        }
        db.add_all(files.values())
        db.add_all(_audio(war, 61, 500, tracks=2) + _audio(moby, 20, 300) + _audio(night, 3, 150))
        db.flush()
        db.add_all([
            Alignment(work_id=war.id, model="m", status=AlignmentStatus.SUCCEEDED,
                      epub_sha256="x", audio_fingerprint="y"),
            Alignment(work_id=moby.id, model="m", status=AlignmentStatus.RUNNING,
                      epub_sha256="x", audio_fingerprint="y"),
            GeneratedAudiobook(work_id=moby.id, engine="piper", model="m", voice="v",
                               epub_sha256="x", audio_fingerprint="y"),
            PdfText(file_id=files["scan"].id, work_id=scan.id, pdf_sha256=files["scan"].sha256,
                    status="needs_ocr", pages=120),
        ])  # fmt: skip
        for uid, w in ((owner_id, war), (mia["id"], comic)):
            db.add(Import(public_id=f"i{w.id}", user_id=uid, status=ImportStatus.FILED,
                          work_id=w.id, expires_at=now))  # fmt: skip
        pw_id = db.scalar(sa.select(AppPassword.id).where(AppPassword.label == "Kindle"))
        db.add_all([
            DeviceQueueItem(app_password_id=pw_id, user_id=owner_id, work_id=war.id,
                            status=QueueStatus.PENDING, position=1),
            DeviceQueueItem(app_password_id=pw_id, user_id=owner_id, work_id=anna.id,
                            status=QueueStatus.DELIVERED, position=2, delivered_at=now),
        ])  # fmt: skip
        # Reading: Anna half way on KOReader, Moby-Dick finished there, Night Sounds half
        # listened in the web player (no text: seconds over its 3 hours).
        db.add_all([
            ReadingProgress(user_id=owner_id, document=files["anna"].partial_md5,
                            progress="x", percentage=0.5, timestamp=1),
            ReadingProgress(user_id=owner_id, document=files["moby"].partial_md5,
                            progress="x", percentage=0.99, timestamp=1),
            WorkProgress(user_id=owner_id, work_id=night.id, sentence_n=0, raw="5400",
                         source=ProgressSource.AUDIO),
        ])  # fmt: skip
        db.commit()
        ids = {
            "war": war.id, "anna": anna.id, "moby": moby.id, "moby2": moby2.id,
            "scan": scan.id, "comic": comic.id, "night": night.id,
        }  # fmt: skip
    for k in ("war", "moby"):
        r = api_post(client, f"/api/shelves/{shared}/items", csrf, {"work_id": ids[k]})
        assert r.status_code == 201, r.text
    api_post(client, f"/api/shelves/{inner}/items", csrf, {"work_id": ids["anna"]})
    api_post(client, f"/api/shelves/{private}/items", csrf, {"work_id": ids["moby2"]})
    rex_http = TestClient(app)
    rex_csrf = login(rex_http, "rex", rex["password"])
    return {
        "ids": ids, "csrf": csrf, "shared": shared, "inner": inner, "private": private,
        "rex": rex_http, "rex_csrf": rex_csrf, "rex_pw": rex["password"], "sync_key": key,
        "app_password": pw["secret"], "app": app,
    }  # fmt: skip


def found(client, f: str = "", **params) -> set[str]:
    r = client.get("/api/library", params={"f": f, **params})
    assert r.status_code == 200, (f, r.text)
    return names(r.json()["works"])


def names(works) -> set[str]:
    return {_NAME[w["id"]] for w in works}


_NAME: dict[int, str] = {}


@pytest.fixture
def ids(lib):
    _NAME.clear()
    _NAME.update({v: k for k, v in lib["ids"].items()})
    return lib["ids"]


ALL = {"war", "anna", "moby", "moby2", "scan", "comic", "night"}

CASES = [
    # Files
    ("format:epub", {"war", "anna", "moby", "moby2"}),
    ("format:pdf", {"scan"}),
    ("format:cbz", {"comic"}),
    ("format:audiobook", {"war", "moby", "night"}),
    ("format:epub,pdf", {"war", "anna", "moby", "moby2", "scan"}),
    ("-format:epub", {"scan", "comic", "night"}),
    ("format:epub format:audiobook", {"war", "moby"}),
    ("kind:ebook", {"anna", "moby2", "scan", "comic"}),
    ("kind:audio", {"night"}),
    ("kind:both", {"war", "moby"}),
    ("size:>100MB", {"war", "moby", "night"}),
    ("size:<10MB", {"anna", "moby2"}),
    ("size:30MB", {"scan"}),
    ("size:20mb..60mb", {"scan", "comic"}),
    # Audio & read-along
    ("has:audio", {"war", "moby", "night"}),
    ("-has:audio", {"anna", "moby2", "scan", "comic"}),
    ("audio:generated", {"moby"}),
    ("audio:narrated", {"war", "night"}),
    ("align:aligned", {"war"}),
    ("align:running", {"moby"}),
    ("align:failed", set()),
    ("align:none", {"night"}),
    ("length:>10h", {"war", "moby"}),
    ("length:3h", {"night"}),
    ("length:180m", {"night"}),
    ("length:<5", {"night"}),
    # "Is not" is everything else, books with no audio (no length at all) included.
    ("-length:>10h", {"anna", "moby2", "scan", "comic", "night"}),
    ("narrator:dale", {"night"}),
    ("narrator:davidson,dale", {"war", "night"}),
    # Reading (the owner's own)
    ("status:reading", {"anna", "night"}),
    ("status:finished", {"moby"}),
    ("status:unread", {"war", "moby2", "scan", "comic"}),
    ("status:unread,finished", {"war", "moby", "moby2", "scan", "comic"}),
    ("progress:<50%", {"war", "moby2", "scan", "comic"}),
    ("progress:50%", {"anna", "night"}),
    ("progress:>90", {"moby"}),
    ("progress:40..60%", {"anna", "night"}),
    ("read:<1d", {"anna", "moby", "night"}),
    ("read:>30d", set()),
    ("started:1w", {"anna", "moby", "night"}),
    ("finished:<1d", {"moby"}),
    ("-finished:<1d", ALL - {"moby"}),
    # Book details (accents and case ignored)
    ("author:tolstoy", {"war"}),
    ("author:TOLSTOI", {"anna"}),
    ("author:tolsto", {"war", "anna"}),
    ("series:epics", {"war"}),
    ("-has:series", ALL - {"war", "comic"}),
    ("lang:ru", {"war", "anna"}),
    ("lang:ru-ru", {"anna"}),
    ("lang:en", {"moby", "moby2"}),
    ("tag:CLASSIC", {"war", "anna"}),
    ("tag:class", set()),
    ("has:tags", {"war", "anna"}),
    ("publisher:vestnik", {"war"}),
    ("year:1850..1870", {"war", "moby", "moby2"}),
    ("year:<1860", {"moby", "moby2"}),
    ("year:1869", {"war"}),
    ("-year:1869", ALL - {"war"}),
    ("pages:>100", {"scan"}),
    ("pages:24", {"comic"}),
    ("pages:..50", {"comic"}),
    # Library
    ("added:<7d", {"night"}),
    ("added:2026-03", {"moby", "moby2"}),
    ("added:2026-01..2026-02", {"war", "anna"}),
    ("added:>=2026-05-01", {"comic", "night"}),
    ("shelf:Shared", {"war", "moby", "anna"}),
    ("shelf:shared/inner", {"anna"}),
    ("shelf:inner", {"anna"}),
    ("shelf:none", {"scan", "comic", "night"}),
    ("shelf:any", {"war", "anna", "moby", "moby2"}),
    ("-shelf:Shared", {"moby2", "scan", "comic", "night"}),
    ("by:mia", {"comic"}),
    ("by:@OWNER", {"war"}),
    # Devices
    ("queue:any", {"war"}),
    ("queue:kindle", {"war"}),
    # Cleanup
    ("-has:cover", ALL - {"war", "comic"}),
    ("-has:author", {"scan"}),
    ("needs:ocr", {"scan"}),
    ("is:duplicate", {"moby", "moby2"}),
    # Words
    ("moby", {"moby", "moby2"}),
    ("-moby", ALL - {"moby", "moby2"}),
    ("epics", {"war"}),
    ('"war and"', {"war"}),
    ("tolstoi", {"anna"}),
    ("format:epub -has:audio author:tolsto", {"anna"}),
]


def test_every_field(client, ids):
    for f, want in CASES:
        assert found(client, f) == want, f


def test_offline_ids_come_from_the_device(client, ids):
    assert found(client, "is:offline") == set()
    held = f"{ids['scan']},{ids['war']},99999"
    assert found(client, "is:offline", offline=held) == {"scan", "war"}
    assert found(client, "-is:offline", offline=held) == ALL - {"scan", "war"}


def test_errors_name_the_field(client, ids):
    for f, code in (
        ("format:mobi", "unknown_value"),
        ("shelf:Nowhere", "unknown_shelf"),
        ("queue:Kobo", "unknown_device"),  # a sync key is not a Send-to-device queue
        ("by:nobody", "unknown_user"),
    ):
        r = client.get("/api/library", params={"f": f})
        assert r.status_code == 422, f
        assert r.json()["detail"] == code, r.text


def test_sorts(client, ids):
    def order(**p):
        r = client.get("/api/library", params=p)
        assert r.status_code == 200, r.text
        return [_NAME[w["id"]] for w in r.json()["works"]]

    # Works without the value come last, in title order.
    rest = ["anna", "scan", "moby2", "comic"]  # Anna, Ledger, Moby-Dick, Spaceman
    assert order(sort="length") == ["war", "moby", "night", *rest]
    assert order(sort="length", order="asc") == ["night", "moby", "war", *rest]
    assert order(sort="pages")[:2] == ["scan", "comic"]
    assert order(sort="year")[:4] == ["moby", "moby2", "war", "anna"]
    # Ties in title order; never-opened works (no reading state) last.
    assert order(sort="progress") == ["moby", "anna", "night", "scan", "moby2", "comic", "war"]
    assert order(sort="added")[:2] == ["night", "comic"]
    assert order(sort="size")[:3] == ["war", "moby", "night"]
    assert order(sort="title", f="format:epub") == ["anna", "moby", "moby2", "war"]
    assert client.get("/api/library", params={"sort": "bogus"}).status_code == 422


def test_shelf_subtree_and_counts(client, ids, lib):
    assert found(client, shelf=lib["shared"]) == {"war", "moby"}
    assert found(client, shelf=lib["shared"], subtree="true") == {"war", "moby", "anna"}
    assert found(client, "format:audiobook", shelf=lib["shared"], subtree="true") == {
        "war",
        "moby",
    }
    r = client.get(
        "/api/library/count", params={"f": "has:audio", "shelf": lib["shared"], "subtree": "true"}
    )
    assert r.json()["total"] == 2


def test_selection_resolves_the_filtered_view(client, ids, lib):
    view = {"f": "format:pdf,cbz"}
    token = client.get("/api/library/count", params=view).json()["view_token"]
    body = {
        "action": "add_tags",
        "tags": ["paged"],
        "selection": {"filter": view, "view_token": token, "exclude_ids": []},
    }
    r = api_post(client, "/api/works/bulk", lib["csrf"], body)
    assert r.status_code == 200, r.text
    assert {_NAME[x["work_id"]] for x in r.json()["results"]} == {"scan", "comic"}
    assert found(client, "tag:paged") == {"scan", "comic"}
    # The filtered view changed (they now match tag:paged -format:cbz differently): 409.
    view = {"f": "-tag:paged"}
    token = client.get("/api/library/count", params=view).json()["view_token"]
    with session_factory()() as db:
        db.get(Work, ids["night"]).tags = ["paged"]
        db.commit()
    body["selection"] = {"filter": view, "view_token": token}
    assert api_post(client, "/api/works/bulk", lib["csrf"], body).status_code == 409


def test_library_reports_reading_state(client, ids):
    works = {_NAME[w["id"]]: w for w in client.get("/api/library").json()["works"]}
    assert works["anna"]["reading"]["status"] == "reading"
    assert works["night"]["reading"]["fraction"] == pytest.approx(0.5)
    assert works["moby"]["reading"]["finished_at"] is not None
    assert works["war"]["reading"] == {
        "fraction": 0.0, "status": "unread", "last_read_at": None, "started_at": None,
        "finished_at": None,
    }  # fmt: skip


def test_facets_are_what_this_person_sees(client, ids, lib):
    def vals(c, f):
        r = c.get(f"/api/library/facets?field={f}")
        return {v["value"]: v["count"] for v in r.json()["values"]}

    assert vals(client, "author") == {
        "Leo Tolstoy": 1, "Lev Tolstoï": 1, "Herman Melville": 1, "herman  melville": 1,
        "Unknown Author": 1, "Ann Artist": 2,
    }  # fmt: skip
    assert vals(client, "tag") == {"Classic": 1, "Russian": 1, "classic": 1}
    assert vals(client, "by") == {"owner": 1, "mia": 1}
    rex = lib["rex"]
    assert vals(rex, "author") == {"Leo Tolstoy": 1, "Lev Tolstoï": 1, "Herman Melville": 1}
    assert vals(rex, "narrator") == {"Frederick Davidson": 1}
    assert rex.get("/api/library/facets?field=by").status_code == 403
    fields = {f["key"] for f in rex.get("/api/library/fields").json()["fields"]}
    assert "by" not in fields and "format" in fields


# --- what a reader sees ------------------------------------------------------------------------


def test_a_reader_filters_only_what_they_see(ids, lib):
    rex = lib["rex"]
    assert found(rex) == {"war", "anna", "moby"}
    for f, want in [
        ("format:epub", {"war", "anna", "moby"}),
        ("-format:epub", set()),
        ("has:audio", {"war", "moby"}),
        ("shelf:none", set()),
        ("shelf:Shared", {"war", "anna", "moby"}),
        ("moby", {"moby"}),
        # Moby-Dick's twin is on a shelf rex cannot see: no duplicate for him.
        ("is:duplicate", set()),
        # His own reading, not the owner's.
        ("status:unread", {"war", "anna", "moby"}),
        ("queue:any", set()),
    ]:
        assert found(rex, f) == want, f


def test_hidden_shelves_answer_like_missing_ones(ids, lib):
    rex = lib["rex"]
    for ref in (f"#{lib['private']}", "#999999", "Private", "Nowhere"):
        r = rex.get("/api/library", params={"f": f"shelf:{ref}"})
        assert r.status_code == 422
        body = r.json()
        assert body == {"detail": "unknown_shelf", "message": f"Shelf: no shelf “{ref}”.",
                        "field": "shelf"}  # fmt: skip
    # The count endpoint and selections behave the same.
    r = rex.get("/api/library/count", params={"f": f"shelf:#{lib['private']}"})
    assert r.status_code == 422


def test_uploader_is_for_owners_and_members(client, ids, lib):
    r = lib["rex"].get("/api/library", params={"f": "by:owner"})
    assert r.status_code == 422 and r.json()["detail"] == "field_not_allowed"
    # Nor through a smart shelf someone else shares: rex can see War, but not who added it.
    s = smart(client, lib["csrf"], "Mine", "by:owner", lib["shared"])
    assert found(client, shelf=s["id"]) == {"war"}
    assert shelf_json(lib["rex"], s["id"])["work_count"] == 0
    assert lib["rex"].get(f"/api/shelves/{s['id']}").json()["works"] == []


# --- smart shelves ------------------------------------------------------------------------------


def smart(client, csrf, name, f, parent=None, status=201, **kw):
    r = api_post(
        client, "/api/shelves", csrf, {"name": name, "parent_id": parent, "filter": f, **kw}
    )
    assert r.status_code == status, r.text
    return r.json()


def shelf_json(client, sid):
    return {s["id"]: s for s in client.get("/api/shelves").json()["shelves"]}.get(sid)


def opds(lib, user: str, password: str, path: str) -> str:
    secret = password
    basic = base64.b64encode(f"{user}:{secret}".encode()).decode()
    r = TestClient(lib["app"]).get(path, headers={"authorization": f"Basic {basic}"})
    assert r.status_code == 200, r.text
    return r.text


def test_smart_shelf_is_live_and_per_viewer(client, ids, lib):
    csrf, rex = lib["csrf"], lib["rex"]
    s = smart(client, csrf, "Ebooks", "format:EPUB", lib["shared"], sort="title")
    assert s["smart"] and s["filter"] == "format:epub" and s["sort"] == "title"
    assert s["work_count"] == 4
    detail = client.get(f"/api/shelves/{s['id']}").json()
    assert [_NAME[w["id"]] for w in detail["works"]] == ["anna", "moby", "moby2", "war"]
    assert found(client, shelf=s["id"]) == {"war", "anna", "moby", "moby2"}
    # Rex sees the shelf (it is inside Shared) with what it matches for him, not the rule.
    mine = shelf_json(rex, s["id"])
    assert mine["work_count"] == 3 and mine["smart"] and "filter" not in mine
    assert [_NAME[w["id"]] for w in rex.get(f"/api/shelves/{s['id']}").json()["works"]] == [
        "anna", "moby", "war",
    ]  # fmt: skip
    # Live: a new EPUB on a hidden shelf counts for the owner at once, never for rex.
    with session_factory()() as db:
        db.add(_file(db.get(Work, ids["scan"]), EPUB, 1))
        db.commit()
    assert shelf_json(client, s["id"])["work_count"] == 5
    assert shelf_json(rex, s["id"])["work_count"] == 3
    # Change the rule and the order.
    r = patch(client, f"/api/shelves/{s['id']}", csrf, {"filter": "has:audio", "sort": "length"})
    assert r.status_code == 200, r.text
    detail = client.get(f"/api/shelves/{s['id']}").json()
    assert [_NAME[w["id"]] for w in detail["works"]] == ["war", "moby", "night"]
    assert detail["filter"] == "has:audio" and detail["sort"] == "length"


def test_smart_shelf_rules(client, ids, lib):
    csrf = lib["csrf"]
    s = smart(client, csrf, "PDFs", "format:pdf")
    manual = lib["shared"]
    # Nothing is put on a smart shelf by hand, and nothing goes inside it.
    r = api_post(client, f"/api/shelves/{s['id']}/items", csrf, {"work_id": ids["war"]})
    assert r.status_code == 409 and r.json()["detail"] == "smart_shelf"
    r = api_post(client, "/api/shelves", csrf, {"name": "Sub", "parent_id": s["id"]})
    assert r.status_code == 409
    # A manual shelf does not become smart (its books would be lost).
    r = patch(client, f"/api/shelves/{manual}", csrf, {"filter": "format:pdf"})
    assert r.status_code == 409 and r.json()["detail"] == "not_smart"
    # A rule must run for whoever saves it.
    for f, code in (("format:mobi", "unknown_value"), ("", "empty_filter"),
                    ("shelf:Nowhere", "unknown_shelf")):  # fmt: skip
        r = api_post(client, "/api/shelves", csrf, {"name": "Bad", "filter": f})
        assert r.status_code == 422 and r.json()["detail"] == code, (f, r.text)
    # Deleting it never touches books.
    r = client.delete(f"/api/shelves/{s['id']}", headers={"origin": ORIGIN, "x-csrf-token": csrf})
    assert r.status_code == 204
    assert len(client.get("/api/library").json()["works"]) == 7


def test_a_grant_on_a_smart_shelf_never_widens_a_readers_library(client, ids, lib):
    csrf, rex = lib["csrf"], lib["rex"]
    rex_id = rex.get("/api/me").json()["user"]["id"]
    # The rule matches two works rex cannot see; a grant on the shelf shows him neither.
    s = smart(client, csrf, "Paged", "format:pdf,cbz")
    r = client.put(f"/api/shelves/{s['id']}/grants/{rex_id}", json={"permission": "view"},
                   headers={"origin": ORIGIN, "x-csrf-token": csrf})  # fmt: skip
    assert r.status_code == 200
    assert found(rex) == {"war", "anna", "moby"}
    mine = shelf_json(rex, s["id"])
    assert mine["parent_id"] is None and mine["work_count"] == 0
    assert rex.get(f"/api/shelves/{s['id']}").json()["works"] == []
    assert (
        found(rex, f"shelf:#{s['id']}") == set() and shelf_json(client, s["id"])["work_count"] == 2
    )


def test_a_rule_naming_a_shelf_the_viewer_cannot_see_matches_nothing_for_them(client, ids, lib):
    csrf, rex = lib["csrf"], lib["rex"]
    s = smart(client, csrf, "Private ones", f"shelf:#{lib['private']}", lib["shared"])
    assert shelf_json(client, s["id"])["work_count"] == 1
    assert shelf_json(rex, s["id"])["work_count"] == 0
    t = smart(client, csrf, "Not private", f"-shelf:#{lib['private']}", lib["shared"])
    assert shelf_json(rex, t["id"])["work_count"] == 3
    # A smart shelf named in a filter is what it shows; inside a rule, nothing (no loops).
    assert found(client, f"shelf:#{s['id']}") == {"moby2"}
    u = smart(client, csrf, "Loop", f"shelf:#{s['id']}")
    assert shelf_json(client, u["id"])["work_count"] == 0
    # Only the shelf's manager may change its rule.
    r = patch(rex, f"/api/shelves/{s['id']}", lib["rex_csrf"], {"filter": "format:pdf"})
    assert r.status_code == 403


def test_smart_shelves_over_opds(client, ids, lib):
    csrf = lib["csrf"]
    s = smart(client, csrf, "To listen", "has:audio -status:finished", lib["shared"],
              sort="length")  # fmt: skip
    owner_feed = opds(lib, "owner", lib["app_password"], f"/opds/shelves/{s['id']}/books")
    assert owner_feed.index("War and Peace") < owner_feed.index("Night Sounds")
    assert "Moby-Dick" not in owner_feed  # finished
    nav = opds(lib, "owner", lib["app_password"], f"/opds/shelves/{lib['shared']}")
    assert f'href="/opds/shelves/{s["id"]}/books"' in nav and "To listen" in nav
    rex_pw = api_post(lib["rex"], "/api/devices/app-passwords", lib["rex_csrf"], {"label": "K"})
    rex_feed = opds(lib, "rex", rex_pw.json()["secret"], f"/opds/shelves/{s['id']}/books")
    # Rex has finished nothing, and Night Sounds is hidden from him.
    assert "War and Peace" in rex_feed and "Moby-Dick" in rex_feed
    assert "Night Sounds" not in rex_feed and "<opensearch:totalResults>2<" in rex_feed


def test_mcp_search_library_takes_the_filter_syntax(app, ids, lib):
    person = Person(app, "owner", OWNER_PASSWORD)
    mcp = MCP(app, person.key("read"))
    out = mcp.ok("search_library", query="format:epub -has:audio tolsto")
    assert out["filter"] == "format:epub -has:audio tolsto"
    assert {b["title"] for b in out["books"]} == {"Anna Karenina"} and out["total"] == 1
    out = mcp.ok("search_library", query="has:audio", sort="length")
    assert [b["title"] for b in out["books"]] == ["War and Peace", "Moby-Dick", "Night Sounds"]
    ok, err = mcp.call("search_library", query="format:mobi")
    assert not ok and "invalid_filter" in err
    shelves = {s["name"]: s for s in mcp.ok("list_shelves")["shelves"]}
    assert shelves["Shared"]["smart"] is False


# --- reading state upkeep -----------------------------------------------------------------------


def state(work_id: int, username: str = "owner") -> ReadingState | None:
    with session_factory()() as db:
        uid = db.scalar(sa.select(User.id).where(User.username == username))
        return db.get(ReadingState, (uid, work_id))


def test_reading_state_follows_every_position_written(client, ids, lib):
    with session_factory()() as db:
        doc = db.scalar(sa.select(BookFile.partial_md5).where(BookFile.work_id == ids["war"]))
    assert state(ids["war"]) is None

    def sync(pct):
        r = client.put(
            "/syncs/progress",
            headers=ko_headers("owner", lib["sync_key"]),
            json={"document": doc, "progress": "1", "percentage": pct, "device": "Kobo"},
        )
        assert r.status_code == 200, r.text
        return state(ids["war"])

    s = sync(0.0)
    assert s.fraction == 0 and s.started_at is None and s.last_read_at is not None
    s = sync(0.3)
    started = s.started_at
    assert s.fraction == pytest.approx(0.3) and started is not None and s.finished_at is None
    assert found(client, "status:reading") == {"anna", "night", "war"}
    s = sync(1.0)
    assert s.finished_at is not None and s.started_at == started
    finished = s.finished_at
    s = sync(0.1)  # reading it again: reading, but it was finished
    assert s.fraction == pytest.approx(0.1) and s.finished_at == finished
    assert "war" in found(client, "status:reading finished:<1d")
    # The audio player (seconds, no text): half of Night Sounds' 3 hours, then three quarters.
    with session_factory()() as db:
        uid = db.scalar(sa.select(User.id).where(User.username == "owner"))
        row = db.scalar(sa.select(WorkProgress).where(WorkProgress.work_id == ids["night"]))
        row.raw = "8100"
        db.commit()
    assert state(ids["night"]).fraction == pytest.approx(0.75)
    # A KOReader sync flushes its own record, then projects onto the work in a second flush
    # (routes/kosync.py put_progress): KOReader's percentage stands, not the sentence's.
    with session_factory()() as db:
        db.get(ReadingProgress, db.scalar(sa.select(ReadingProgress.id).where(
            ReadingProgress.document == doc))).percentage = 0.6  # fmt: skip
        db.flush()
        db.add(WorkProgress(user_id=uid, work_id=ids["war"], sentence_n=0, raw="1",
                            source=ProgressSource.KOREADER))  # fmt: skip
        db.commit()
    assert state(ids["war"]).fraction == pytest.approx(0.6)


def test_migration_0018_backfills_states_and_pages(tmp_path):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from lectern.models import Base

    engine = make_engine(f"sqlite:///{tmp_path / 'm18.db'}")
    cfg = alembic_config(str(engine.url))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0017_data_saver")
    sql = [
        "INSERT INTO users (id, username, display_name, role, webauthn_user_handle, totp_enabled,"
        " is_active, created_at, show_technical_details, auto_file_imports, data_saver)"
        " VALUES (1, 'o', 'O', 'OWNER', x'00', 0, 1, '2026-01-01', 0, 0, 0)",
    ]
    for i, t in enumerate(("Text", "Audio", "Paged"), 1):
        sql.append(
            "INSERT INTO works (id, title, sort_title, author, sort_author, dir_path, has_cover,"
            " created_at, updated_at, version, readalong_enabled, data_saver_enabled,"
            f" cover_seed) VALUES ({i}, '{t}', '{t}', 'A', 'a', 'A/{t}', 0, '2026-01-01',"
            " '2026-01-01', 1, 1, 1, 0)"
        )
    sql += [
        "INSERT INTO book_files (id, work_id, kind, rel_path, media_type, size, sha256,"
        " partial_md5, added_at) VALUES (1, 1, 'EBOOK', 'b.epub', 'application/epub+zip', 1,"
        " 's1', 'doc1', '2026-01-01'), (3, 3, 'EBOOK', 'b.pdf', 'application/pdf', 1, 's3',"
        " 'doc3', '2026-01-01')",
        "INSERT INTO pdf_texts (file_id, work_id, pdf_sha256, version, status, pages,"
        " pages_with_text, chars, sentence_count, attempts, created_at, updated_at)"
        " VALUES (3, 3, 's3', 1, 'ready', 42, 42, 1, 0, 0, '2026-01-01', '2026-01-01')",
        "INSERT INTO audio_tracks (work_id, position, rel_path, media_type, codec, duration_s,"
        " offset_s, size, sha256, added_at) VALUES (2, 0, 'a.mp3', 'audio/mpeg', 'mp3', 100, 0,"
        " 1, 't', '2026-01-01')",
        # Text: an older web position at 20%, then KOReader at 99% (finished).
        "INSERT INTO sentences (work_id, n, spine_index, char_start, char_end, matched)"
        " VALUES " + ", ".join(f"(1, {n}, 0, {n}, {n + 1}, 0)" for n in range(10)),
        "INSERT INTO work_progress (user_id, work_id, sentence_n, sentence_offset, source, raw,"
        " device, received_at, updated_at) VALUES (1, 1, 2, 0, 'WEB', '2:0', '',"
        " '2026-02-01 10:00:00.000000', '2026-02-01 10:00:00.000000'),"
        " (1, 2, 0, 0, 'AUDIO', '25', '', '2026-03-01 10:00:00.000000',"
        " '2026-03-01 10:00:00.000000')",
        "INSERT INTO reading_progress (user_id, document, progress, percentage, device,"
        " device_id, timestamp, updated_at) VALUES (1, 'doc1', 'x', 0.99, '', '', 1,"
        " '2026-02-05 10:00:00.000000'),"
        # Paged: KOReader at 100%, then kosync's projection of it (no sentences: 0).
        " (1, 'doc3', '42', 1.0, '', '', 1, '2026-04-01 10:00:00.000000')",
        "INSERT INTO work_progress (user_id, work_id, sentence_n, sentence_offset, source, raw,"
        " device, received_at, updated_at) VALUES (1, 3, 0, 0, 'KOREADER', '42', '',"
        " '2026-04-01 10:00:00.000001', '2026-04-01 10:00:00.000001')",
    ]
    with engine.begin() as conn:
        for s in sql:
            conn.execute(sa.text(s))
    upgrade_to_head(engine)
    with engine.connect() as conn:
        rows = {
            r[0]: r[1:]
            for r in conn.execute(
                sa.text(
                    "SELECT work_id, fraction, last_read_at, started_at, finished_at"
                    " FROM reading_states ORDER BY work_id"
                )
            )
        }
        assert rows[1] == (
            0.99, "2026-02-05 10:00:00.000000", "2026-02-01 10:00:00.000000",
            "2026-02-05 10:00:00.000000",
        )  # fmt: skip
        assert rows[2] == (0.25, "2026-03-01 10:00:00.000000", "2026-03-01 10:00:00.000000", None)
        t = "2026-04-01 10:00:00.000000"
        assert rows[3] == (1.0, t, t, t)
        pages = dict(conn.execute(sa.text("SELECT id, pages FROM book_files")).all())
        assert pages == {1: None, 3: 42}
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0017_data_saver")
    insp = sa.inspect(engine)
    assert "reading_states" not in insp.get_table_names()
    assert "pages" not in {c["name"] for c in insp.get_columns("book_files")}
    upgrade_to_head(engine)

    # SQLite before 3.35 has no ALTER TABLE … DROP COLUMN: the downgrade still works, keeps
    # every file (no table rebuild cascading through foreign keys), and so does upgrading again.
    # Only 0018's own step runs as old SQLite; later revisions go down to it normally first.
    def old_sqlite(_conn, _cursor, statement, *_args):
        if "DROP COLUMN" in statement.upper():
            raise sa.exc.OperationalError(statement, None, Exception('near "DROP": syntax error'))

    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0018_library_filters")
    real_version = engine.dialect.server_version_info
    engine.dialect.server_version_info = (3, 34, 1)
    sa.event.listen(engine, "before_cursor_execute", old_sqlite)
    try:
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "0017_data_saver")
    finally:
        sa.event.remove(engine, "before_cursor_execute", old_sqlite)
        engine.dialect.server_version_info = real_version
    insp = sa.inspect(engine)
    assert "reading_states" not in insp.get_table_names()
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM book_files")).scalar() == 2
        assert conn.execute(sa.text("SELECT count(*) FROM pdf_texts")).scalar() == 1
        conn.execute(sa.text("UPDATE book_files SET pages = 7"))  # left stale while downgraded
        conn.commit()
    upgrade_to_head(engine)
    with engine.connect() as conn:
        pages = dict(conn.execute(sa.text("SELECT id, pages FROM book_files")).all())
        assert pages == {1: None, 3: 42}
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []


def test_page_counts_are_filled_in(settings, client, owner):
    import zipfile

    from PIL import Image

    from lectern.services import fill_page_counts

    with session_factory()() as db:
        w = Work(title="C", sort_title="c", author="A", sort_author="a", dir_path="A/C")
        db.add(w)
        db.flush()
        (settings.library_dir / "A" / "C").mkdir(parents=True)
        path = settings.library_dir / "A" / "C" / "c.cbz"
        with zipfile.ZipFile(path, "w") as zf:
            for i in range(3):
                img = settings.library_dir / f"p{i}.png"
                Image.new("RGB", (10, 10), (i * 40, 0, 0)).save(img)
                zf.write(img, f"{i:03d}.png")
        f = BookFile(work_id=w.id, kind=FileKind.EBOOK, rel_path="c.cbz", media_type=CBZ,
                     size=path.stat().st_size, sha256="c" * 64)  # fmt: skip
        db.add(f)
        db.commit()
        fid = f.id
    assert fill_page_counts(session_factory(), settings.library_dir) == 1
    with session_factory()() as db:
        assert db.get(BookFile, fid).pages == 3
    assert fill_page_counts(session_factory(), settings.library_dir) == 0
