"""Server-side multi-select: "All N in this view" resolves the Library's filter on the server
(lectern.routes.api.selection), for bulk edits, trash, send to device, and alignment.

Expected sets come from how each library was built here, never from the code under test.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from tests.conftest import (
    api_post,
    drain_inbox,
    ko_headers,
    login,
    make_epub,
    make_sync_key,
    make_user,
    put_json,
)
from tests.test_jobs import ingest_fixture, wc, work_id, worker_key  # noqa: F401 - fixtures

from lectern.routes.api.selection import fold, view_token


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


def view(client, **f) -> dict:
    """What the web app sends for "all of this view": the filter and the view's token."""
    r = client.get("/api/library/count", params=f)
    assert r.status_code == 200, r.text
    return {"filter": f, "view_token": r.json()["view_token"]}


def titles(client) -> dict[str, dict]:
    return {w["title"]: w for w in client.get("/api/library").json()["works"]}


def test_bulk_edit_with_a_selection_targets_the_view_minus_exclusions(client, lib):
    ids, csrf = lib["ids"], lib["csrf"]
    body = {
        "action": "add_tags",
        "tags": ["unread-pile"],
        "selection": {**view(client, status="unread"), "exclude_ids": [ids["Delta"]]},
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
    seen = view(client, q="ann")  # "All 2 selected": Charlie and Delta
    make_epub(settings.inbox_path / "Foxtrot.epub", title="Foxtrot", authors=["Ann Other"])
    drain_inbox(settings)
    body = {"action": "trash", "selection": {**seen, "expected_count": 2}}
    r = api_post(client, "/api/works/bulk", csrf, body)
    assert r.status_code == 409 and r.json()["detail"] == "selection_changed"
    assert len(titles(client)) == 6
    body["selection"] = {**view(client, q="ann"), "expected_count": 3}
    r = api_post(client, "/api/works/bulk", csrf, body)
    assert r.status_code == 200 and all(x["ok"] for x in r.json()["results"])
    assert set(titles(client)) == {"Alpha", "Bravo", "Echo"}


def test_the_view_token_binds_which_works_not_how_many(client, lib, settings):
    """One work leaves the view and another arrives: still 2, but not the 2 the user saw."""
    ids, csrf = lib["ids"], lib["csrf"]
    seen = view(client, q="ann")  # Charlie and Delta
    r = api_post(client, "/api/works/bulk", csrf, {"work_ids": [ids["Delta"]],
                 "action": "set_authors", "authors": [{"name": "Someone New"}]})  # fmt: skip
    assert r.json()["results"][0]["ok"]
    make_epub(settings.inbox_path / "Foxtrot.epub", title="Foxtrot", authors=["Ann Other"])
    drain_inbox(settings)
    assert count(client, q="ann") == 2  # Charlie and Foxtrot
    body = {"action": "trash", "selection": {**seen, "expected_count": 2}}
    r = api_post(client, "/api/works/bulk", csrf, body)
    assert r.status_code == 409 and r.json()["detail"] == "selection_changed"
    assert {"Charlie", "Delta", "Foxtrot"} <= set(titles(client))
    # The same works in another order (a different sort) are the same view.
    assert view(client, q="ann")["view_token"] == view(client, q="ANN")["view_token"]


def test_a_shelf_the_user_cannot_see_is_not_a_filter_for_them(client, app, lib):
    """Count and bulk resolve answer 404 for a shelf the caller has no access to, as
    GET /api/shelves/{id} does: neither its membership nor an edit through it leaks."""
    csrf, night, ids = lib["csrf"], lib["shelf"], lib["ids"]
    mia = make_user(client, csrf, "mia", "member")
    rex = make_user(client, csrf, "rex", "reader")
    other = api_post(client, "/api/shelves", csrf, {"name": "Rex's"}).json()["id"]
    for t in ("Alpha", "Delta"):
        api_post(client, f"/api/shelves/{other}/items", csrf, {"work_id": ids[t]})
    r = put_json(client, f"/api/shelves/{other}/grants/{rex['id']}", csrf, {"permission": "view"})
    assert r.status_code == 200, r.text
    for person in (mia, rex):
        http = TestClient(app)
        pcsrf = login(http, person["username"], person["password"])
        assert http.get(f"/api/shelves/{night}").status_code == 404
        for params in ({"shelf": night}, {"shelf": night, "q": "alpha"}, {"shelf": 99999}):
            r = http.get("/api/library/count", params=params)
            assert r.status_code == 404, (person["username"], params, r.text)
        sel = {"filter": {"shelf": night}, "view_token": "x" * 32}
        r = api_post(http, "/api/works/bulk", pcsrf,
                     {"action": "add_tags", "tags": ["leak"], "selection": sel})  # fmt: skip
        assert r.status_code in (403, 404), r.text  # readers cannot bulk edit at all
        dev = api_post(http, "/api/devices", pcsrf, {"label": "Kobo"}).json()
        r = api_post(http, f"/api/device-queues/{dev['device']['app_password']['id']}/items",
                     pcsrf, {"selection": sel})  # fmt: skip
        assert r.status_code == 404, r.text
        http.close()
    assert not any("leak" in w["tags"] for w in titles(client).values())
    # The reader's own shelf works as a filter for them.
    http = TestClient(app)
    login(http, "rex", rex["password"])
    assert http.get("/api/library/count", params={"shelf": other}).json()["total"] == 2
    http.close()


def test_each_endpoint_caps_a_selection_like_its_work_ids(client, lib, monkeypatch):
    csrf = lib["csrf"]
    monkeypatch.setattr("lectern.routes.api.editor.BULK_MAX", 4)
    r = api_post(client, "/api/works/bulk", csrf,
                 {"action": "add_tags", "tags": ["x"], "selection": view(client)})  # fmt: skip
    assert r.status_code == 422 and r.json()["detail"] == "selection_too_large"
    assert r.json()["limit"] == 4
    assert not any("x" in w["tags"] for w in titles(client).values())
    monkeypatch.setattr("lectern.device_queue.MAX_BATCH", 4)
    dev = api_post(client, "/api/devices", csrf, {"label": "Kobo"}).json()
    r = api_post(client, f"/api/device-queues/{dev['device']['app_password']['id']}/items",
                 csrf, {"selection": view(client)})  # fmt: skip
    assert r.status_code == 422 and r.json()["limit"] == 4
    monkeypatch.setattr("lectern.routes.api.align.ALIGN_MANY_MAX", 4)
    r = api_post(client, "/api/works/align", csrf, {"selection": view(client)})
    assert r.status_code == 422 and r.json()["limit"] == 4
    assert client.get("/api/jobs").json()["jobs"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"action": "trash"},
        {"action": "trash", "work_ids": [1], "selection": {"filter": {}, "view_token": "x"}},
        {"action": "trash", "selection": {"filter": {"q": "nope"}, "view_token": view_token([])}},
        {"action": "trash", "selection": {"filter": {"sort": "title"}, "view_token": "x"}},
        {"action": "trash", "selection": {"filter": {}}},  # no view_token
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
    sel = {**view(client, shelf=lib["shelf"]), "expected_count": 2}
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
    sel = {**view(client), "exclude_ids": [plain], "expected_count": 1}
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


def test_align_many_does_not_rerun_a_current_alignment(client, owner, work_id, wc):  # noqa: F811
    from tests.test_jobs import claim, lease, put_result, request_align, sentence_map_body

    job = request_align(client, owner, work_id).json()
    c = claim(wc)
    body, _ = sentence_map_body(work_id)
    assert put_result(wc, job["id"], lease(c), body).status_code == 200
    r = api_post(client, "/api/works/align", owner["csrf"], {"work_ids": [work_id]})
    assert r.json()["results"] == [{"work_id": work_id, "status": "already_aligned"}]
    assert [j["id"] for j in client.get("/api/jobs").json()["jobs"]] == [job["id"]]
