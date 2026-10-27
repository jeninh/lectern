"""Automatic metadata suggestions (the Fix metadata pass) and library duplicate detection.

Upstreams are faked; nothing leaves the test. Duplicate detection never touches the network.
"""

from __future__ import annotations

import json

import pytest
from tests.conftest import ORIGIN, api_post, drain_inbox, make_epub, put_json
from tests.test_lookup import OL_SEARCH, OL_WORK, FakeUpstream

from lectern import autometa, duplicates, egress, lookup
from lectern.db import session_factory
from lectern.models import AudioTrack, MatchSuggestion, MetadataSuggestion, Work


@pytest.fixture(autouse=True)
def _fresh():
    lookup.clear_cache()
    yield
    lookup.clear_cache()


def enable(client, csrf, on=True):
    r = put_json(client, "/api/library-settings", csrf, {"metadata_lookup_enabled": on})
    assert r.status_code == 200, r.text


def add_book(settings, name: str, title: str, authors: list[str], isbn: str | None = None) -> int:
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    make_epub(settings.inbox_path / f"{name}.epub", title=title, authors=authors, isbn=isbn)
    drain_inbox(settings)
    with session_factory()() as db:
        return db.query(Work).filter_by(title=title).one().id


def strip(work_id: int, *fields: str) -> None:
    """A book whose files carried none of these fields."""
    with session_factory()() as db:
        w = db.get(Work, work_id)
        for f in fields:
            setattr(w, f, None)
        db.commit()


def run(fetch) -> int:
    return autometa.run_pending(session_factory(), fetch)


def items(client) -> dict[int, dict]:
    return {i["work_id"]: i for i in client.get("/api/metadata/suggestions").json()["items"]}


def test_library_pass_suggests_only_missing_fields_and_applies_only_chosen_ones(
    client, owner, settings
):
    csrf = owner["csrf"]
    wid = add_book(settings, "lh", "The Left Hand of Darkness", ["Ursula K. Le Guin"])
    strip(wid, "description", "series", "isbn", "tags")
    with session_factory()() as db:
        db.get(Work, wid).publisher = "My Press"  # present: never suggested
        db.commit()
    enable(client, csrf)
    fake = FakeUpstream()
    r = api_post(client, "/api/metadata/fix", csrf, {"library": True})
    assert r.status_code == 200, r.text
    assert r.json()["counts"] == {"queued": 1}
    assert fake.urls == []  # queued, not fetched in the request
    assert run(fake) == 1
    # By title: Open Library's search, its work record, and Google Books.
    assert any("title=The+Left+Hand" in u for u in fake.urls)
    item = items(client)[wid]
    assert item["status"] == "ready"
    f = item["fields"]
    assert set(f) == {"description", "series", "isbn", "tags"}  # series_index: OL has none
    assert f["description"]["value"] == "Genly Ai is sent to Gethen."
    assert f["description"]["provider"] == "openlibrary"
    assert f["series"]["value"] == "Hainish Cycle"
    assert f["isbn"]["value"] == "9780441478125"
    assert f["tags"]["value"] == ["Science fiction", "Gender"]
    assert item["current"]["description"] is None
    # Suggestions only: nothing written yet.
    with session_factory()() as db:
        assert db.get(Work, wid).description is None

    # Apply two of the four fields; the others are discarded with the suggestion.
    r = api_post(
        client,
        f"/api/metadata/suggestions/{wid}/apply",
        csrf,
        {"version": item["version"], "fields": ["description", "series"]},
    )
    assert r.status_code == 200, r.text
    with session_factory()() as db:
        w = db.get(Work, wid)
        assert w.description == "Genly Ai is sent to Gethen." and w.series == "Hainish Cycle"
        assert w.isbn is None and not w.tags and w.publisher == "My Press"
        assert db.query(MetadataSuggestion).one().status == "applied"
    assert wid not in items(client)
    # An ordinary, undoable edit.
    revs = client.get(f"/api/works/{wid}/revisions").json()
    assert revs["revisions"][0]["changed"] == ["metadata"]


def test_user_edited_fields_are_never_suggested_and_stale_applies_conflict(client, owner, settings):
    csrf = owner["csrf"]
    wid = add_book(settings, "lh", "The Left Hand of Darkness", ["Ursula K. Le Guin"])
    strip(wid, "series", "isbn", "tags")
    # Someone deliberately clears the description in the editor.
    ed = client.get(f"/api/works/{wid}/edit").json()
    meta = {**ed["metadata"], "description": None}
    r = client.patch(
        f"/api/works/{wid}",
        json={"version": ed["version"], "metadata": meta},
        headers={"origin": ORIGIN, "x-csrf-token": csrf},
    )
    assert r.status_code == 200, r.text
    enable(client, csrf)
    api_post(client, "/api/metadata/fix", csrf, {"work_ids": [wid]})
    run(FakeUpstream())
    item = items(client)[wid]
    assert "description" not in item["fields"] and "series" in item["fields"]
    # The work changes after the suggestion was shown: applying must not overwrite blindly.
    with session_factory()() as db:
        db.get(Work, wid).version += 1
        db.commit()
    r = api_post(
        client,
        f"/api/metadata/suggestions/{wid}/apply",
        csrf,
        {"version": item["version"], "fields": ["series"]},
    )
    assert r.status_code == 409 and r.json()["detail"] == "version_conflict", r.text
    r = api_post(
        client,
        f"/api/metadata/suggestions/{wid}/apply",
        csrf,
        {"version": item["version"] + 1, "fields": ["description"]},
    )
    assert r.status_code == 422 and r.json()["detail"] == "field_not_suggested"
    with session_factory()() as db:
        assert db.get(Work, wid).series is None


def test_nothing_goes_out_when_lookups_are_off_or_offline(client, owner, settings):
    csrf = owner["csrf"]
    wid = add_book(settings, "lh", "The Left Hand of Darkness", ["Ursula K. Le Guin"])
    strip(wid, "description")
    fake = FakeUpstream()
    r = api_post(client, "/api/metadata/fix", csrf, {"work_ids": [wid]})
    assert r.status_code == 403 and r.json()["detail"] == "lookup_disabled"
    enable(client, csrf)
    egress.enforce(True)
    r = api_post(client, "/api/metadata/fix", csrf, {"work_ids": [wid]})
    assert r.status_code == 409 and r.json()["detail"] == "lookup_offline"
    # Queued while online, then offline mode comes on before the runner gets to it.
    egress.enforce(False)
    assert api_post(client, "/api/metadata/fix", csrf, {"work_ids": [wid]}).status_code == 200
    egress.enforce(True)
    run(fake)
    assert fake.urls == []
    assert items(client)[wid]["status"] == "error"
    assert items(client)[wid]["error"] == "lookup_offline"
    # Same when the owner turns lookups off.
    egress.enforce(False)
    api_post(client, "/api/metadata/fix", csrf, {"work_ids": [wid]})
    enable(client, csrf, on=False)
    run(fake)
    assert fake.urls == [] and items(client)[wid]["error"] == "lookup_disabled"


def test_bulk_selection_results_rate_limit_and_wrong_book(client, owner, settings, monkeypatch):
    csrf = owner["csrf"]
    a = add_book(settings, "lh", "The Left Hand of Darkness", ["Ursula K. Le Guin"])
    b = add_book(settings, "other", "Some Other Book", ["Someone Else"])
    c = add_book(settings, "full", "Complete Book", ["Writer"])
    strip(a, "description")
    strip(b, "description")
    with session_factory()() as db:
        w = db.get(Work, c)
        w.series, w.series_index, w.isbn, w.tags = "S", 1.0, "9780441478125", ["x"]
        db.commit()
    enable(client, csrf)
    r = api_post(client, "/api/metadata/fix", csrf, {"work_ids": [a, b, c, 999]})
    by = {x["work_id"]: x["status"] for x in r.json()["results"]}
    assert by == {a: "queued", b: "queued", c: "nothing_missing", 999: "not_found"}
    r = api_post(client, "/api/metadata/fix", csrf, {"work_ids": [a]})
    assert r.json()["results"] == [{"work_id": a, "status": "already_queued"}]

    # The providers' global limit holds for bulk runs: over it, the work stays queued.
    monkeypatch.setattr(autometa, "PER_TICK", 10)
    for lim in lookup._provider_limits.values():
        while lim.allow("global"):
            pass
    fake = FakeUpstream()
    assert run(fake) == 0 and fake.urls == []
    assert items(client)[a]["status"] == "queued"
    lookup.clear_cache()  # resets the limit windows too
    run(fake)
    # Every title search answers with the Le Guin record: right for A, wrong for B.
    assert items(client)[a]["status"] == "ready"
    assert items(client)[b]["status"] == "no_match" and items(client)[b]["fields"] == {}
    # Results already cached: the next pass over A sends nothing new.
    n = len(fake.urls)
    api_post(client, f"/api/metadata/suggestions/{a}/dismiss", csrf)
    api_post(client, "/api/metadata/fix", csrf, {"work_ids": [a]})
    run(fake)
    assert len(fake.urls) == n and items(client)[a]["status"] == "ready"
    # A library pass leaves a dismissed work alone.
    api_post(client, f"/api/metadata/suggestions/{a}/dismiss", csrf)
    r = api_post(client, "/api/metadata/fix", csrf, {"library": True})
    assert r.json()["counts"].get("dismissed") == 1 and r.json()["results"] == []


def test_isbn_lookup_and_series_number(client, owner, settings):
    csrf = owner["csrf"]
    wid = add_book(settings, "x", "Totally Different Title", ["Nobody"], isbn="9780441478125")
    strip(wid, "description", "series", "series_index")

    class Upstream(FakeUpstream):
        def __call__(self, url):
            if url.startswith("https://openlibrary.org/search.json"):
                self.urls.append(url)
                doc = {**OL_SEARCH["docs"][0], "series": ["Hainish Cycle (6)"]}
                return json.dumps({"docs": [doc]}).encode()
            if url.endswith(".json") and "works" in url:
                self.urls.append(url)
                return json.dumps(OL_WORK).encode()
            return super().__call__(url)

    enable(client, csrf)
    api_post(client, "/api/metadata/fix", csrf, {"work_ids": [wid]})
    fake = Upstream()
    run(fake)
    # By ISBN only (no title sent), and an ISBN match is trusted whatever the title.
    assert all("Totally" not in u for u in fake.urls)
    f = items(client)[wid]["fields"]
    assert f["series"]["value"] == "Hainish Cycle" and f["series_index"]["value"] == 6.0


# --- duplicates ------------------------------------------------------------------------------


def _work(db, title, author="A. Writer", isbn=None, **kw) -> int:
    w = Work(
        title=title,
        sort_title=title.lower(),
        author=author,
        sort_author=author.lower(),
        isbn=isbn,
        dir_path=f"{author}/{title}/{len(title)}{isbn}{kw.get('series_index')}",
        **kw,
    )
    db.add(w)
    db.flush()
    return w.id


def _track(db, work_id: int, sha: str) -> None:
    db.add(
        AudioTrack(
            work_id=work_id, position=0, rel_path="01.mp3", media_type="audio/mpeg",
            codec="mp3", duration_s=1.0, offset_s=0.0, size=1, sha256=sha,
        )
    )  # fmt: skip


def test_duplicate_scan_by_isbn_title_and_hash_and_ignore_sticks(client, owner):
    from lectern.ingest.matching import sort_title

    with session_factory()() as db:
        i1 = _work(db, "Alpha", isbn="9780441478125")
        i2 = _work(db, "Beta", author="Other Person", isbn="9780441478125")
        t1 = _work(db, "The Dispossessed", "Ursula K. Le Guin")
        t2 = _work(db, "Dispossessed, The", "Le Guin, Ursula K.")
        h1 = _work(db, "Gamma", "One")
        h2 = _work(db, "Delta", "Two")
        _track(db, h1, "a" * 64)
        _track(db, h2, "a" * 64)
        v1 = _work(db, "Saga Volume", "Same", series="Saga", series_index=1.0)
        v2 = _work(db, "Saga Volume", "Same", series="Saga", series_index=2.0)
        n1 = _work(db, "Unrelated", "Someone")
        for w in db.query(Work):
            w.sort_title = sort_title(w.title)
        db.commit()
        assert duplicates.scan(db) == 3
        db.commit()
        pairs = {frozenset((s.work_id, s.candidate_work_id)): s for s in db.query(MatchSuggestion)}
    assert set(pairs) == {frozenset((i1, i2)), frozenset((t1, t2)), frozenset((h1, h2))}
    assert pairs[frozenset((i1, i2))].reason == "same ISBN"
    assert pairs[frozenset((h1, h2))].reason == "same file"
    assert pairs[frozenset((t1, t2))].reason.startswith("title 100%, author 100%")
    assert all(s.origin == "library" for s in pairs.values())
    assert pairs[frozenset((t1, t2))].work_id == t2  # the newer one merges into the older
    assert v1 and v2 and n1  # volumes of a series and the unrelated book are not flagged

    listed = client.get("/api/suggestions").json()["suggestions"]
    assert {s["origin"] for s in listed} == {"library"} and len(listed) == 3
    sid = pairs[frozenset((t1, t2))].id
    assert api_post(client, f"/api/suggestions/{sid}/dismiss", owner["csrf"]).status_code == 200
    # Ignore sticks (in the duplicate filter too): a rescan (the API's, or the hourly sweep's) does not flag it again.
    assert api_post(client, "/api/suggestions/scan", owner["csrf"]).json() == {"found": 0}
    assert len(client.get("/api/suggestions").json()["suggestions"]) == 2
    from lectern.filters import duplicate_ids
    from lectern.models import User

    with session_factory()() as db:
        user = db.query(User).one()
        assert {t1, t2}.isdisjoint(duplicate_ids(db, user))
        assert {i1, i2, h1, h2} <= duplicate_ids(db, user)


def test_duplicate_merge_uses_the_existing_merge_path(client, owner, settings):
    csrf = owner["csrf"]
    a = add_book(settings, "a", "First Book", ["Author One"], isbn="9780441478125")
    b = add_book(settings, "b", "Completely Unlike", ["Author Two"])
    with session_factory()() as db:
        db.get(Work, b).isbn = "9780441478125"
        db.commit()
    assert api_post(client, "/api/suggestions/scan", csrf).json() == {"found": 1}
    (s,) = client.get("/api/suggestions").json()["suggestions"]
    assert (s["work"]["id"], s["candidate"]["id"]) == (b, a)
    r = api_post(client, f"/api/suggestions/{s['id']}/merge", csrf)
    assert r.status_code == 200 and r.json() == {"work_id": a}
    with session_factory()() as db:
        assert db.get(Work, b) is None
        assert len(db.get(Work, a).files) == 2
    assert api_post(client, "/api/suggestions/scan", csrf).json() == {"found": 0}


def test_duplicate_merge_keeps_what_the_owner_set_on_the_newer_copy(client, owner, settings):
    """Regression: merging a library pair once dropped the newer copy's description, tags,
    series, reading position, and finished state."""
    from datetime import datetime, timedelta

    from lectern.models import ReadingState, User, WorkProgress, utcnow

    csrf = owner["csrf"]
    a = add_book(settings, "a", "First Book", ["Author One"], isbn="9780441478125")
    b = add_book(settings, "b", "Completely Unlike", ["Author Two"])
    long_ago = datetime(2025, 1, 2, 3, 4, 5, tzinfo=datetime.now().astimezone().tzinfo)
    with session_factory()() as db:
        uid = db.query(User).one().id
        older, newer = db.get(Work, a), db.get(Work, b)
        older.description, older.tags, older.series, older.publisher = None, None, None, "Kept"
        newer.isbn, newer.description, newer.tags = "9780441478125", "Owner's notes.", ["Fav"]
        newer.series, newer.series_index, newer.publisher = "Saga", 2.0, "Not this one"
        db.add(
            WorkProgress(
                user_id=uid, work_id=b, sentence_n=1, sentence_offset=3, source="web", raw="1:3"
            )
        )
        db.flush()
        state = db.get(ReadingState, (uid, b))
        state.fraction, state.started_at, state.finished_at = 1.0, long_ago, long_ago
        state.last_read_at = long_ago
        db.commit()
    assert api_post(client, "/api/suggestions/scan", csrf).json() == {"found": 1}
    (s,) = client.get("/api/suggestions").json()["suggestions"]
    assert api_post(client, f"/api/suggestions/{s['id']}/merge", csrf).status_code == 200
    with session_factory()() as db:
        assert db.get(Work, b) is None
        w = db.get(Work, a)
        # Empty fields filled from the newer copy; a field the kept copy had stays its own.
        assert (w.description, w.tags, w.series, w.series_index) == (
            "Owner's notes.",
            ["Fav"],
            "Saga",
            2.0,
        )
        assert w.publisher == "Kept" and w.isbn == "9780441478125"
        p = db.query(WorkProgress).filter_by(user_id=uid).one()
        assert (p.work_id, p.sentence_n, p.sentence_offset, p.raw) == (a, 1, 3, "1:3")
        st = db.get(ReadingState, (uid, a))
        assert st is not None and st.fraction == 1.0
        assert st.finished_at == long_ago and st.started_at == long_ago
        assert st.last_read_at == long_ago and st.last_read_at < utcnow() - timedelta(days=1)


def test_duplicate_merge_keeps_the_earliest_start_and_finish_when_both_were_read(
    client, owner, settings
):
    from datetime import datetime, timedelta

    from lectern.models import ReadingState, User

    csrf = owner["csrf"]
    a = add_book(settings, "a", "First Book", ["Author One"], isbn="9780441478125")
    b = add_book(settings, "b", "Completely Unlike", ["Author Two"])
    t0 = datetime(2025, 1, 1, tzinfo=datetime.now().astimezone().tzinfo)
    with session_factory()() as db:
        uid = db.query(User).one().id
        db.get(Work, b).isbn = "9780441478125"
        db.add(ReadingState(user_id=uid, work_id=a, fraction=0.5, last_read_at=t0,
                            started_at=t0 + timedelta(days=5), finished_at=None))  # fmt: skip
        db.add(ReadingState(user_id=uid, work_id=b, fraction=1.0, last_read_at=t0,
                            started_at=t0, finished_at=t0 + timedelta(days=2)))  # fmt: skip
        db.commit()
    api_post(client, "/api/suggestions/scan", csrf)
    (s,) = client.get("/api/suggestions").json()["suggestions"]
    assert api_post(client, f"/api/suggestions/{s['id']}/merge", csrf).status_code == 200
    with session_factory()() as db:
        st = db.get(ReadingState, (uid, a))
        assert st.started_at == t0 and st.finished_at == t0 + timedelta(days=2)
        assert st.fraction == 0.5  # the kept copy's own position stands
