"""Server-side multi-select: "All N in this view" resolves the Library's filter on the server
(lectern.routes.api.selection), for bulk edits, trash, send to device, and alignment.

Expected sets come from how each library was built here, never from the code under test.
"""

from __future__ import annotations

import pytest
from tests.conftest import api_post, drain_inbox, ko_headers, make_epub, make_sync_key
from tests.test_jobs import ingest_fixture, wc, work_id, worker_key  # noqa: F401 - fixtures

from lectern.routes.api.selection import fold


def test_fold_matches_the_web_app():
    # fuzzy.ts: NFKD, strip U+0300–U+036F, toLowerCase. Ligatures decompose; ø has no
    # decomposition, so it stays (the web app does the same).
    assert fold("Zoë ÅNGSTRÖM") == "zoe angstrom"
    assert fold("Ｆｉｌｅ ﬁsh") == "file fish"
    assert fold("Søren") == "søren"


@pytest.fixture
def lib(client, owner, settings):
    """Five books; Alpha is 40% read and Bravo 99% (finished) on KOReader; Alpha and
    Charlie are on the "Night" shelf."""
    books = [
        ("Alpha", "Zoë Ångström", None),
        ("Bravo", "Zoe Angstrom", None),
        ("Charlie", "Ann Other", "9780000000002"),
        ("Delta", "Ann Other", None),
        ("Echo", "Somebody Else", None),
    ]
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    for i, (title, author, isbn) in enumerate(books):
        make_epub(
            settings.inbox_path / f"{title}.epub",
            title=title,
            authors=[author],
            isbn=isbn,
            padding=i * 13,
        )
    drain_inbox(settings)
    works = {w["title"]: w for w in client.get("/api/library").json()["works"]}
    assert len(works) == 5
    csrf = owner["csrf"]
    key = make_sync_key(client, csrf, "Kindle")
    for title, pct in (("Alpha", 0.4), ("Bravo", 0.99)):
        doc = works[title]["files"][0]["partial_md5"]
        r = client.put(
            "/syncs/progress",
            headers=ko_headers("owner", key),
            json={"document": doc, "progress": "12", "percentage": pct, "device": "Kindle"},
        )
        assert r.status_code == 200, r.text
    shelf = api_post(client, "/api/shelves", csrf, {"name": "Night"}).json()["id"]
    for t in ("Alpha", "Charlie"):
        api_post(client, f"/api/shelves/{shelf}/items", csrf, {"work_id": works[t]["id"]})
    return {"ids": {t: w["id"] for t, w in works.items()}, "shelf": shelf, "csrf": csrf}


def count(client, **params) -> int:
    r = client.get("/api/library/count", params=params)
    assert r.status_code == 200, r.text
    return r.json()["total"]


def test_count_mirrors_the_library_filter(client, lib):
    assert count(client) == 5
    # Case and diacritics fold; the ISBN matches too, as in the web app's filter.
    assert count(client, q="angstrom") == 2
    assert count(client, q="ZOË") == 2
    assert count(client, q="0000000002") == 1
    assert count(client, status="reading") == 1
    assert count(client, status="finished") == 1
    assert count(client, status="unread") == 3
    assert count(client, shelf=lib["shelf"]) == 2
    assert count(client, shelf=lib["shelf"], status="unread") == 1
    assert count(client, q="ann", status="reading") == 0
    assert client.get("/api/library/count", params={"status": "bogus"}).status_code == 422


def titles(client) -> dict[str, dict]:
    return {w["title"]: w for w in client.get("/api/library").json()["works"]}


def test_bulk_edit_with_a_selection_targets_the_view_minus_exclusions(client, lib):
    ids, csrf = lib["ids"], lib["csrf"]
    body = {
        "action": "add_tags",
        "tags": ["unread-pile"],
        "selection": {
            "filter": {"status": "unread"},
            "exclude_ids": [ids["Delta"]],
            "expected_count": 2,
        },
    }
    r = api_post(client, "/api/works/bulk", csrf, body)
    assert r.status_code == 200, r.text
    assert sorted(x["work_id"] for x in r.json()["results"]) == sorted(
        [ids["Charlie"], ids["Echo"]]
    )
    tagged = {t for t, w in titles(client).items() if "unread-pile" in w["tags"]}
    assert tagged == {"Charlie", "Echo"}


def test_a_selection_that_changed_since_it_was_shown_is_refused(client, lib, settings):
    csrf = lib["csrf"]
    make_epub(settings.inbox_path / "Foxtrot.epub", title="Foxtrot", authors=["Ann Other"])
    drain_inbox(settings)
    # The user saw "All 2 selected" for "ann"; a third match has arrived since.
    body = {
        "action": "trash",
        "selection": {"filter": {"q": "ann"}, "expected_count": 2},
    }
    r = api_post(client, "/api/works/bulk", csrf, body)
    assert r.status_code == 409 and r.json()["detail"] == "selection_changed"
    assert len(titles(client)) == 6
    body["selection"]["expected_count"] = 3
    r = api_post(client, "/api/works/bulk", csrf, body)
    assert r.status_code == 200 and all(x["ok"] for x in r.json()["results"])
    assert set(titles(client)) == {"Alpha", "Bravo", "Echo"}


@pytest.mark.parametrize(
    "body",
    [
        {"action": "trash"},
        {"action": "trash", "work_ids": [1], "selection": {"filter": {}}},
        {"action": "trash", "selection": {"filter": {"q": "no such book"}}},
        {"action": "trash", "selection": {"filter": {"sort": "title"}}},
    ],
)
def test_bulk_needs_exactly_one_non_empty_target(client, lib, body):
    r = api_post(client, "/api/works/bulk", lib["csrf"], body)
    assert r.status_code == 422, r.text
    assert len(titles(client)) == 5


def test_send_a_selection_to_a_device(client, lib):
    ids, csrf = lib["ids"], lib["csrf"]
    r = api_post(client, "/api/devices", csrf, {"label": "Kobo"})
    dev = r.json()["device"]["app_password"]["id"]
    sel = {"filter": {"shelf": lib["shelf"]}, "expected_count": 2}
    r = api_post(client, f"/api/device-queues/{dev}/items", csrf, {"selection": sel})
    assert r.status_code == 200, r.text
    got = {x["work_id"]: x["status"] for x in r.json()["results"]}
    assert got == {ids["Alpha"]: "queued", ids["Charlie"]: "queued"}
    pending = client.get(f"/api/device-queues/{dev}").json()["pending"]
    assert {p["work"]["title"] for p in pending} == {"Alpha", "Charlie"}


def test_align_many_reports_each_work(client, owner, settings, tmp_path):
    with_audio = ingest_fixture(settings, tmp_path)
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    make_epub(settings.inbox_path / "Plain.epub", title="Plain", authors=["Nobody"])
    drain_inbox(settings)
    plain = titles(client)["Plain"]["id"]
    csrf = owner["csrf"]
    r = api_post(client, "/api/works/align", csrf, {"work_ids": [with_audio, plain, 99999]})
    assert r.status_code == 200, r.text
    first = {x["work_id"]: x["status"] for x in r.json()["results"]}
    assert first == {with_audio: "queued", plain: "no_inputs", 99999: "not_found"}
    # Never forced: asking again leaves the queued job alone.
    sel = {"filter": {}, "exclude_ids": [plain], "expected_count": 1}
    r = api_post(client, "/api/works/align", csrf, {"selection": sel})
    assert r.json()["results"] == [
        {
            "work_id": with_audio,
            "status": "already_queued",
            "job_id": r.json()["results"][0]["job_id"],
        }
    ]
    jobs = client.get("/api/jobs", params={"work_id": with_audio}).json()["jobs"]
    assert len(jobs) == 1


def test_align_many_keeps_the_out_of_memory_rules(client, owner, work_id, wc):  # noqa: F811
    """Bulk alignment cannot quietly rerun what POST /api/works/{id}/align refuses."""
    from tests.test_jobs import _fail_alignment

    csrf = owner["csrf"]
    _fail_alignment(client, owner, work_id, wc, "small", "out of memory: MemoryError")
    r = api_post(client, "/api/works/align", csrf, {"work_ids": [work_id]})
    (res,) = r.json()["results"]
    assert res["status"] == "queued"
    # retried one size smaller, as the single-work endpoint does
    assert client.get(f"/api/jobs/{res['job_id']}").json()["model"] == "base"
    api_post(client, f"/api/jobs/{res['job_id']}/cancel", csrf)
    _fail_alignment(client, owner, work_id, wc, "tiny", "out of memory: MemoryError")
    r = api_post(client, "/api/works/align", csrf, {"work_ids": [work_id]})
    assert r.json()["results"] == [{"work_id": work_id, "status": "out_of_memory_choose_model"}]
    r = api_post(client, "/api/works/align", csrf, {"work_ids": [work_id], "model": "tiny"})
    assert r.json()["results"][0]["status"] == "queued"
