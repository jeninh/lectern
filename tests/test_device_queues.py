"""Per-device queues: API, OPDS feeds, credential scoping, delivery on download, visibility,
roles, and concurrency (docs/device-queues.md).

Expected values come from how the household was built here, never from the code under test.
"""

from __future__ import annotations

import base64
import hashlib
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lxml import etree
from sqlalchemy import func, select
from tests.conftest import (
    ORIGIN,
    OWNER_PASSWORD,
    api_delete,
    api_post,
    ko_headers,
    login,
    make_epub,
    make_sync_key,
    make_user,
    put_json,
)

from lectern import device_queue
from lectern.config import Settings
from lectern.db import session_factory
from lectern.ingest.watcher import InboxWatcher
from lectern.models import (
    AuditLog,
    BookFile,
    DeviceQueueItem,
    DownloadEvent,
    FileKind,
    QueueStatus,
    User,
    Work,
)

ATOM = "{http://www.w3.org/2005/Atom}"
OS = "{http://a9.com/-/spec/opensearch/1.1/}"
ACQ_REL = "http://opds-spec.org/acquisition"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # Many catalogue requests per test: lift the per-IP Basic-auth limit (tested separately).
    return Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        base_url=ORIGIN,
        insecure_http=True,
        rate_limit_per_minute=10_000,
    )


def basic(username: str, secret: str) -> dict:
    return {"authorization": "Basic " + base64.b64encode(f"{username}:{secret}".encode()).decode()}


class Person:
    """A signed-in browser for one user, plus their OPDS app passwords."""

    def __init__(self, app, username: str, password: str):
        self.http = TestClient(app)
        self.username = username
        self.csrf = login(self.http, username, password)
        self.devices: dict[str, dict] = {}

    def add_device(self, label: str) -> dict:
        r = api_post(self.http, "/api/devices", self.csrf, {"label": label})
        assert r.status_code == 201, r.text
        pw = r.json()["device"]["app_password"]
        dev = {"id": pw["id"], "headers": basic(self.username, r.json()["opds_secret"])}
        self.devices[label] = dev
        return dev

    def queue(self, device_id: int, *work_ids: int):
        return api_post(
            self.http,
            f"/api/device-queues/{device_id}/items",
            self.csrf,
            {"work_ids": list(work_ids)},
        )


@pytest.fixture
def home(client, owner, app, settings):
    """Owner (Kobo, Phone), member mia (Kindle), reader rex (Boox). Four EPUB works; "Shared"
    (owner's shelf: Alpha, Bravo) is shared with rex; one work has no EPUB at all."""
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    titles = ["Alpha", "Bravo", "Charlie", "Delta"]
    for i, t in enumerate(titles):
        make_epub(
            settings.inbox_path / f"{t}.epub", title=t, authors=[f"Author {t}"], padding=i * 11
        )
    w = InboxWatcher(settings.inbox_path, settings.library_dir, session_factory(), 0.01)
    w.scan_once()
    w.scan_once()
    with session_factory()() as db:
        ids = {wk.title: wk.id for wk in db.scalars(select(Work))}
        files = {
            wk.title: db.scalar(select(BookFile).where(BookFile.work_id == wk.id))
            for wk in db.scalars(select(Work))
        }
        audio_only = Work(
            title="Echo", sort_title="echo", author="A", sort_author="a", dir_path="A/Echo"
        )
        db.add(audio_only)
        db.commit()
        ids["Echo"] = audio_only.id
    assert set(ids) == {*titles, "Echo"}
    mia = make_user(client, owner["csrf"], "mia", "member")
    rex = make_user(client, owner["csrf"], "rex", "reader")
    shelf = api_post(client, "/api/shelves", owner["csrf"], {"name": "Shared"}).json()
    for t in ("Alpha", "Bravo"):
        api_post(client, f"/api/shelves/{shelf['id']}/items", owner["csrf"], {"work_id": ids[t]})
    r = put_json(
        client,
        f"/api/shelves/{shelf['id']}/grants/{rex['id']}",
        owner["csrf"],
        {"permission": "view"},
    )
    assert r.status_code == 200, r.text
    people = {
        "owner": Person(app, "owner", OWNER_PASSWORD),
        "mia": Person(app, "mia", mia["password"]),
        "rex": Person(app, "rex", rex["password"]),
    }
    people["owner"].add_device("Kobo")
    people["owner"].add_device("Phone")
    people["mia"].add_device("Kindle")
    people["rex"].add_device("Boox")
    return {
        "ids": ids,
        "files": {t: (f.id, f.sha256) for t, f in files.items()},
        "shelf": shelf["id"],
        "users": {"mia": mia, "rex": rex},
        **people,
    }


def feed(http: TestClient, path: str, headers: dict) -> etree._Element:
    r = http.get(path, headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/atom+xml;profile=opds-catalog")
    return etree.fromstring(r.content)


def titles(root: etree._Element) -> list[str]:
    return [e.findtext(f"{ATOM}title") for e in root.iter(f"{ATOM}entry")]


def statuses(resp) -> dict[int, str]:
    assert resp.status_code == 200, resp.text
    return {r["work_id"]: r["status"] for r in resp.json()["results"]}


def item_status(device_id: int, work_id: int) -> QueueStatus | None:
    with session_factory()() as db:
        return db.scalar(
            select(DeviceQueueItem.status).where(
                DeviceQueueItem.app_password_id == device_id, DeviceQueueItem.work_id == work_id
            )
        )


# --- the catalogue -------------------------------------------------------------------------------


def test_root_lists_continue_up_next_and_this_devices_queue_first(home):
    owner, ids = home["owner"], home["ids"]
    kobo, phone = owner.devices["Kobo"], owner.devices["Phone"]
    assert statuses(owner.queue(kobo["id"], ids["Bravo"], ids["Alpha"])) == {
        ids["Bravo"]: "queued",
        ids["Alpha"]: "queued",
    }
    root = feed(owner.http, "/opds", kobo["headers"])
    assert titles(root) == [
        "Continue reading",
        "Up next",
        "Queued for this device",
        "Shelves",
        "All books",
        "Recently added",
    ]
    content = {
        e.findtext(f"{ATOM}title"): e.findtext(f"{ATOM}content") for e in root.iter(f"{ATOM}entry")
    }
    assert content["Queued for this device"] == "2 books sent to Kobo"
    # the same person's other device has its own, empty queue
    assert feed(owner.http, "/opds", phone["headers"]).xpath(
        "//a:entry[a:title='Queued for this device']/a:content/text()", namespaces={"a": ATOM[1:-1]}
    ) == ["Nothing sent to Phone"]
    # queued order, scoped to the credential
    assert titles(feed(owner.http, "/opds/queue", kobo["headers"])) == ["Bravo", "Alpha"]
    assert titles(feed(owner.http, "/opds/queue", phone["headers"])) == []
    mia, rex = home["mia"], home["rex"]
    assert titles(feed(mia.http, "/opds/queue", mia.devices["Kindle"]["headers"])) == []
    assert titles(feed(rex.http, "/opds/queue", rex.devices["Boox"]["headers"])) == []
    # unauthenticated: the Basic challenge, as for every catalogue route
    r = owner.http.get("/opds/queue", headers={"authorization": ""})
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")


def test_queue_entries_are_the_libraries_entries(home, settings):
    """Same entry id, acquisition link, and bytes as in /opds/all, so a client that knows the
    book (and KOReader's document hash of the file) sees the same book."""
    owner, ids = home["owner"], home["ids"]
    kobo = owner.devices["Kobo"]
    owner.queue(kobo["id"], ids["Charlie"])
    q = feed(owner.http, "/opds/queue", kobo["headers"])
    every = feed(owner.http, "/opds/all", kobo["headers"])
    (entry,) = q.iter(f"{ATOM}entry")
    same = next(e for e in every.iter(f"{ATOM}entry") if e.findtext(f"{ATOM}title") == "Charlie")
    assert etree.tostring(entry) == etree.tostring(same)
    assert entry.findtext(f"{ATOM}id") == f"urn:lectern:work:{ids['Charlie']}"
    (acq,) = [ln for ln in entry.iter(f"{ATOM}link") if ln.get("rel") == ACQ_REL]
    file_id, sha = home["files"]["Charlie"]
    assert acq.get("href") == f"/opds/files/{file_id}/download"
    assert acq.get("type") == "application/epub+zip"
    r = owner.http.get(acq.get("href"), headers=kobo["headers"])
    assert r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == sha
    assert r.headers["etag"] == f'"{sha}"' and r.headers["accept-ranges"] == "bytes"
    assert (
        # the header Starlette's FileResponse sent before (RFC 5987, for the spaces)
        r.headers["content-disposition"]
        == "attachment; filename*=utf-8''Author%20Charlie%20-%20Charlie.epub"
    )
    assert q.findtext(f"{ATOM}id") == f"urn:lectern:queue:{kobo['id']}"


def test_queue_feed_pages_like_the_rest_of_the_catalogue(home):
    owner = home["owner"]
    kobo = owner.devices["Kobo"]
    with session_factory()() as db:
        new = []
        for i in range(53):
            w = Work(
                title=f"Vol {i:02d}", sort_title=f"vol {i:02d}", author="S", sort_author="s",
                dir_path=f"S/Vol {i:02d}",
            )  # fmt: skip
            db.add(w)
            db.flush()
            db.add(
                BookFile(
                    work_id=w.id, kind=FileKind.EBOOK, rel_path="book.epub",
                    media_type="application/epub+zip", size=1, sha256=f"{i:064x}",
                )
            )  # fmt: skip
            new.append(w.id)
        db.commit()
    assert set(statuses(owner.queue(kobo["id"], *new)).values()) == {"queued"}
    p1 = feed(owner.http, "/opds/queue", kobo["headers"])
    assert titles(p1) == [f"Vol {i:02d}" for i in range(50)]
    assert p1.findtext(f"{OS}totalResults") == "53" and p1.findtext(f"{OS}startIndex") == "1"
    rels = {ln.get("rel"): ln.get("href") for ln in p1.findall(f"{ATOM}link")}
    assert rels["next"] == "/opds/queue?page=2" and "previous" not in rels
    p2 = feed(owner.http, rels["next"], kobo["headers"])
    assert titles(p2) == ["Vol 50", "Vol 51", "Vol 52"]
    rels2 = {ln.get("rel"): ln.get("href") for ln in p2.findall(f"{ATOM}link")}
    assert rels2["previous"] == "/opds/queue?page=1" and "next" not in rels2


def test_continue_and_up_next_follow_progress_and_shelves(home):
    rex, ids = home["rex"], home["ids"]
    boox = rex.devices["Boox"]
    # nothing started: the shared shelf is all up next, in shelf order
    assert titles(feed(rex.http, "/opds/continue", boox["headers"])) == []
    assert titles(feed(rex.http, "/opds/up-next", boox["headers"])) == ["Alpha", "Bravo"]
    # KOReader reports progress on Bravo
    key = make_sync_key(rex.http, rex.csrf, "Boox sync")
    doc = rex.http.get(f"/api/works/{ids['Bravo']}").json()["files"][0]["partial_md5"]
    r = rex.http.put(
        "/syncs/progress",
        json={"document": doc, "progress": "/body/p[1]", "percentage": 0.3, "device": "Boox"},
        headers=ko_headers("rex", key),
    )
    assert r.status_code == 200, r.text
    assert titles(feed(rex.http, "/opds/continue", boox["headers"])) == ["Bravo"]
    assert titles(feed(rex.http, "/opds/up-next", boox["headers"])) == ["Alpha"]
    # the web reader's canonical position counts as started too, and is newer
    r = put_json(
        rex.http,
        f"/api/works/{ids['Alpha']}/progress",
        rex.csrf,
        {"source": "web", "sentence_n": 0, "device": "Laptop"},
    )
    assert r.status_code == 200, r.text
    assert titles(feed(rex.http, "/opds/continue", boox["headers"])) == ["Alpha", "Bravo"]
    assert titles(feed(rex.http, "/opds/up-next", boox["headers"])) == []
    # same list and order as the web app's Continue
    api = [w["id"] for w in rex.http.get("/api/continue?limit=50").json()["items"]]
    want = [ids[t] for t in titles(feed(rex.http, "/opds/continue", boox["headers"]))]
    assert api == want
    # a member without shelves has nothing up next
    mia = home["mia"]
    assert titles(feed(mia.http, "/opds/up-next", mia.devices["Kindle"]["headers"])) == []


# --- enqueue, remove, requeue, order ---------------------------------------------------------------


def test_enqueue_is_idempotent_and_requeue_goes_to_the_end(home):
    owner, ids = home["owner"], home["ids"]
    kobo = owner.devices["Kobo"]
    a, b = ids["Alpha"], ids["Bravo"]
    assert statuses(owner.queue(kobo["id"], a, a, b)) == {a: "queued", b: "queued"}
    assert statuses(owner.queue(kobo["id"], a)) == {a: "already_queued"}
    q = owner.http.get(f"/api/device-queues/{kobo['id']}").json()
    assert [i["work"]["title"] for i in q["pending"]] == ["Alpha", "Bravo"]
    assert q["device"]["pending"] == 2 and q["pending"][0]["queued_by"] == "owner"
    item_a = q["pending"][0]["id"]
    r = api_delete(owner.http, f"/api/device-queues/{kobo['id']}/items/{item_a}", owner.csrf)
    assert r.status_code == 200 and r.json()["removed"] is True
    r = api_delete(owner.http, f"/api/device-queues/{kobo['id']}/items/{item_a}", owner.csrf)
    assert r.status_code == 200 and r.json()["removed"] is False  # already removed: no-op
    q = owner.http.get(f"/api/device-queues/{kobo['id']}").json()
    assert [i["work"]["title"] for i in q["pending"]] == ["Bravo"]
    assert [(i["work"]["title"], i["status"]) for i in q["history"]] == [("Alpha", "removed")]
    assert q["history"][0]["removed_at"] and q["history"][0]["removed_by"] == "owner"
    assert statuses(owner.queue(kobo["id"], a)) == {a: "requeued"}
    q = owner.http.get(f"/api/device-queues/{kobo['id']}").json()
    assert [i["work"]["title"] for i in q["pending"]] == ["Bravo", "Alpha"]
    assert q["history"] == [] and q["pending"][1]["removed_at"] is None
    with session_factory()() as db:  # one row per device and work, whatever happened
        assert db.scalar(select(func.count()).select_from(DeviceQueueItem)) == 2
        actions = [r.action for r in db.scalars(select(AuditLog).order_by(AuditLog.id))]
    assert actions.count("queue.add") == 3 and actions.count("queue.remove") == 1
    acts = [e["action"] for e in owner.http.get("/api/activity").json()["events"]]
    assert "queue.add" in acts and "queue.remove" in acts


def test_reorder_needs_the_current_queue(home):
    owner, ids = home["owner"], home["ids"]
    kobo = owner.devices["Kobo"]
    owner.queue(kobo["id"], ids["Alpha"], ids["Bravo"], ids["Charlie"])
    items = {
        i["work"]["title"]: i["id"]
        for i in owner.http.get(f"/api/device-queues/{kobo['id']}").json()["pending"]
    }
    order = [items["Charlie"], items["Alpha"], items["Bravo"]]
    r = api_post(
        owner.http, f"/api/device-queues/{kobo['id']}/order", owner.csrf, {"item_ids": order}
    )
    assert r.status_code == 200, r.text
    assert titles(feed(owner.http, "/opds/queue", kobo["headers"])) == ["Charlie", "Alpha", "Bravo"]
    stale = [items["Alpha"], items["Charlie"]]
    r = api_post(
        owner.http, f"/api/device-queues/{kobo['id']}/order", owner.csrf, {"item_ids": stale}
    )
    assert r.status_code == 409 and r.json()["detail"] == "queue_changed"
    dup = [items["Alpha"], items["Alpha"], items["Bravo"]]
    r = api_post(
        owner.http, f"/api/device-queues/{kobo['id']}/order", owner.csrf, {"item_ids": dup}
    )
    assert r.status_code == 409
    assert titles(feed(owner.http, "/opds/queue", kobo["headers"])) == ["Charlie", "Alpha", "Bravo"]


# --- delivery ------------------------------------------------------------------------------------


def test_delivered_only_when_that_device_downloads_the_whole_epub(home):
    owner, ids = home["owner"], home["ids"]
    kobo, phone = owner.devices["Kobo"], owner.devices["Phone"]
    a = ids["Alpha"]
    file_id, sha = home["files"]["Alpha"]
    url = f"/opds/files/{file_id}/download"
    owner.queue(kobo["id"], a)

    def downloads() -> int:
        with session_factory()() as db:
            return db.scalar(select(func.count()).select_from(DownloadEvent)) or 0

    # another device of the same person downloading it delivers nothing to the Kobo
    assert owner.http.get(url, headers=phone["headers"]).status_code == 200
    # nor does the owner's browser
    assert owner.http.get(f"/library/files/{file_id}/download").status_code == 200
    assert item_status(kobo["id"], a) == QueueStatus.PENDING
    # probes from the Kobo itself: a partial range, a conditional request, a HEAD
    r = owner.http.get(url, headers={**kobo["headers"], "range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100
    r = owner.http.get(url, headers={**kobo["headers"], "range": "bytes=100-"})
    assert r.status_code == 206
    before = downloads()
    r = owner.http.get(url, headers={**kobo["headers"], "if-none-match": f'"{sha}"'})
    assert r.status_code == 304 and r.content == b""
    assert downloads() == before  # a 304 is not a download either
    assert owner.http.head(url, headers=kobo["headers"]).status_code == 405
    assert item_status(kobo["id"], a) == QueueStatus.PENDING
    assert titles(feed(owner.http, "/opds/queue", kobo["headers"])) == ["Alpha"]
    # the whole file, to the Kobo: delivered, once
    r = owner.http.get(url, headers=kobo["headers"])
    assert r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == sha
    assert item_status(kobo["id"], a) == QueueStatus.DELIVERED
    assert owner.http.get(url, headers=kobo["headers"]).status_code == 200
    with session_factory()() as db:
        item = db.scalar(select(DeviceQueueItem))
        assert item.delivered_at is not None and item.delivered_file_id == file_id
        delivered = db.scalars(select(AuditLog).where(AuditLog.action == "queue.delivered")).all()
    assert len(delivered) == 1 and delivered[0].detail == "device Kobo"
    assert titles(feed(owner.http, "/opds/queue", kobo["headers"])) == []
    hist = owner.http.get(f"/api/device-queues/{kobo['id']}").json()["history"]
    assert [(i["work"]["title"], i["status"]) for i in hist] == [("Alpha", "delivered")]


def test_a_range_covering_the_whole_file_delivers(home):
    owner, ids = home["owner"], home["ids"]
    kobo = owner.devices["Kobo"]
    file_id, sha = home["files"]["Bravo"]
    owner.queue(kobo["id"], ids["Bravo"])
    r = owner.http.get(
        f"/opds/files/{file_id}/download", headers={**kobo["headers"], "range": "bytes=0-"}
    )
    assert r.status_code == 206 and hashlib.sha256(r.content).hexdigest() == sha
    assert item_status(kobo["id"], ids["Bravo"]) == QueueStatus.DELIVERED


def test_a_refused_download_does_not_deliver(home):
    owner, ids = home["owner"], home["ids"]
    kobo = owner.devices["Kobo"]
    r = api_post(
        owner.http, f"/api/devices/app-passwords/{kobo['id']}/download-cap", owner.csrf, {"cap": 1}
    )
    assert r.status_code == 200
    owner.queue(kobo["id"], ids["Delta"])
    first = owner.http.get(
        f"/opds/files/{home['files']['Charlie'][0]}/download", headers=kobo["headers"]
    )
    assert first.status_code == 200
    r = owner.http.get(f"/opds/files/{home['files']['Delta'][0]}/download", headers=kobo["headers"])
    assert r.status_code == 429
    assert item_status(kobo["id"], ids["Delta"]) == QueueStatus.PENDING


def test_a_transfer_started_before_a_requeue_does_not_deliver_it(home):
    """Regression: the delivery matched any pending row of the work, so a download that began
    while the book was queued, then removed and queued again, delivered the new queueing."""
    owner, ids = home["owner"], home["ids"]
    kobo = owner.devices["Kobo"]
    a = ids["Alpha"]
    file_id = home["files"]["Alpha"][0]
    owner.queue(kobo["id"], a)
    r = owner.http.get(f"/opds/files/{file_id}/download", headers=kobo["headers"])
    assert r.status_code == 200  # delivered: the transfer we will replay below is "old"
    assert statuses(owner.queue(kobo["id"], a)) == {a: "requeued"}
    with session_factory()() as db:
        current = device_queue.pending_item(db, kobo["id"], a)
    stale = (current[0], current[1].replace(year=current[1].year - 1))
    assert not device_queue.mark_delivered(kobo["id"], file_id, None, stale)
    # another work's file, or another device, cannot deliver the item either
    other = home["files"]["Bravo"][0]
    assert not device_queue.mark_delivered(kobo["id"], other, None, current)
    assert not device_queue.mark_delivered(owner.devices["Phone"]["id"], file_id, None, current)
    assert item_status(kobo["id"], a) == QueueStatus.PENDING
    assert device_queue.mark_delivered(kobo["id"], file_id, None, current)
    assert item_status(kobo["id"], a) == QueueStatus.DELIVERED


def test_a_wildcard_or_weak_if_none_match_is_a_free_304(home):
    """Regression: only an exact strong ETag skipped the cap; ``*`` or ``W/"sha"`` was counted
    as a download and then answered 304 by the file response, with no bytes sent."""
    owner, ids = home["owner"], home["ids"]
    kobo = owner.devices["Kobo"]
    file_id, sha = home["files"]["Charlie"]
    owner.queue(kobo["id"], ids["Charlie"])
    for inm in ("*", f'W/"{sha}"', f'"x", "{sha}"'):
        r = owner.http.get(
            f"/opds/files/{file_id}/download", headers={**kobo["headers"], "if-none-match": inm}
        )
        assert r.status_code == 304
    with session_factory()() as db:
        assert db.scalar(select(func.count()).select_from(DownloadEvent)) == 0
    assert item_status(kobo["id"], ids["Charlie"]) == QueueStatus.PENDING


def test_an_abandoned_transfer_never_completes():
    """The client went away after the first block: the body generator is closed there."""
    from lectern.http_range import _completed

    done: list[bool] = []
    blocks = _completed(iter([b"a" * 10, b"b" * 10]), 20, lambda: done.append(True))
    assert next(blocks) == b"a" * 10
    blocks.close()
    assert done == []
    assert list(_completed(iter([b"a" * 10, b"b" * 10]), 20, lambda: done.append(True)))
    assert done == [True]


def test_concurrent_enqueues_and_deliveries_land_once(home):
    owner, ids = home["owner"], home["ids"]
    kobo = owner.devices["Kobo"]
    a = ids["Alpha"]
    file_id = home["files"]["Alpha"][0]
    barrier = threading.Barrier(8)
    results: list[str] = []
    lock = threading.Lock()

    def add():
        with session_factory()() as db:
            actor = db.scalar(select(User).where(User.username == "owner"))
            t = device_queue.target(db, actor, kobo["id"])
            barrier.wait()
            (res,) = device_queue.enqueue(db, actor, t, [a], None)
        with lock:
            results.append(res["status"])

    threads = [threading.Thread(target=add) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["already_queued"] * 7 + ["queued"]
    delivered: list[bool] = []
    barrier = threading.Barrier(8)
    with session_factory()() as db:
        item = device_queue.pending_item(db, kobo["id"], a)

    def deliver():
        barrier.wait()
        ok = device_queue.mark_delivered(kobo["id"], file_id, None, item)
        with lock:
            delivered.append(ok)

    threads = [threading.Thread(target=deliver) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(delivered) == [False] * 7 + [True]
    with session_factory()() as db:
        assert db.scalar(select(func.count()).select_from(DeviceQueueItem)) == 1
        n = db.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "queue.delivered")
        )
    assert n == 1


# --- who may queue what to whom ---------------------------------------------------------------------


def test_roles_and_visibility(home):
    owner, mia, rex, ids = home["owner"], home["mia"], home["rex"], home["ids"]
    kobo, kindle, boox = owner.devices["Kobo"], mia.devices["Kindle"], rex.devices["Boox"]
    a, c = ids["Alpha"], ids["Charlie"]
    # targets: your own devices; the owner sees everyone's, own first
    mine = mia.http.get("/api/device-queues").json()["devices"]
    assert [(d["label"], d["own"]) for d in mine] == [("Kindle", True)]
    everyone = owner.http.get("/api/device-queues").json()["devices"]
    assert [d["label"] for d in everyone][:2] == ["Phone", "Kobo"]
    assert {(d["label"], d["user"]["username"]) for d in everyone[2:]} == {
        ("Kindle", "mia"),
        ("Boox", "rex"),
    }
    # a member cannot see, queue to, or edit someone else's device
    assert mia.http.get(f"/api/device-queues/{kobo['id']}").status_code == 404
    assert mia.queue(kobo["id"], a).status_code == 404
    assert rex.queue(kindle["id"], a).status_code == 404
    # a reader queues only what is shared with them, to their own device
    assert statuses(rex.queue(boox["id"], a, c, 999_999)) == {
        a: "queued",
        c: "not_found",
        999_999: "not_found",
    }
    # the owner may queue to the reader's device, but only what the reader can see
    assert statuses(owner.queue(boox["id"], ids["Bravo"], c)) == {
        ids["Bravo"]: "queued",
        c: "not_visible_to_device",
    }
    assert titles(feed(rex.http, "/opds/queue", boox["headers"])) == ["Alpha", "Bravo"]
    # no EPUB, nothing to send to an e-reader
    assert statuses(owner.queue(kobo["id"], ids["Echo"])) == {ids["Echo"]: "no_ebook"}
    # the reader may take the owner's item off their own queue; the member may not touch it
    items = rex.http.get(f"/api/device-queues/{boox['id']}").json()["pending"]
    bravo = next(i for i in items if i["work"]["title"] == "Bravo")
    assert items[1]["queued_by"] == "owner"
    path = f"/api/device-queues/{boox['id']}/items/{bravo['id']}"
    assert api_delete(mia.http, path, mia.csrf).status_code == 404
    assert api_delete(rex.http, path, rex.csrf).json()["removed"] is True
    # an item id from another device is not found through this one
    alpha = next(i for i in items if i["work"]["title"] == "Alpha")
    r = api_delete(owner.http, f"/api/device-queues/{kobo['id']}/items/{alpha['id']}", owner.csrf)
    assert r.status_code == 404
    # writes need the CSRF token and a same-origin request
    r = rex.http.post(f"/api/device-queues/{boox['id']}/items", json={"work_ids": [a]})
    assert r.status_code == 403


def test_unsharing_hides_queued_works_and_revoking_closes_the_queue(home):
    owner, rex, ids = home["owner"], home["rex"], home["ids"]
    boox = rex.devices["Boox"]
    a = ids["Alpha"]
    rex.queue(boox["id"], a)
    r = api_delete(owner.http, f"/api/shelves/{home['shelf']}/items/{a}", owner.csrf)
    assert r.status_code == 204
    assert titles(feed(rex.http, "/opds/queue", boox["headers"])) == []
    assert rex.http.get(f"/api/device-queues/{boox['id']}").json()["pending"] == []
    file_id = home["files"]["Alpha"][0]
    assert (
        rex.http.get(f"/opds/files/{file_id}/download", headers=boox["headers"]).status_code == 404
    )
    assert item_status(boox["id"], a) == QueueStatus.PENDING  # kept, just not visible
    # nor can rex probe the hidden work's id through the send dialog's "already queued"
    got = rex.http.get(f"/api/device-queues?work_id={a}").json()["devices"]
    assert [d["queued"] for d in got] == [False]
    api_post(owner.http, f"/api/shelves/{home['shelf']}/items", owner.csrf, {"work_id": a})
    assert titles(feed(rex.http, "/opds/queue", boox["headers"])) == ["Alpha"]
    # a revoked device: its catalogue closes and nobody can queue to it
    device_id = rex.http.get("/api/devices").json()["devices"][0]["id"]
    api_post(rex.http, f"/api/devices/{device_id}/revoke", rex.csrf)
    assert rex.http.get("/opds/queue", headers=boox["headers"]).status_code == 401
    assert rex.queue(boox["id"], ids["Bravo"]).status_code == 404
    assert rex.http.get("/api/device-queues").json()["devices"] == []


def test_queue_feed_obeys_the_basic_auth_rate_limit(home, app):
    owner = home["owner"]
    kobo = owner.devices["Kobo"]
    app.state.limiters["basic"].limit = 3
    codes = [owner.http.get("/opds/queue", headers=kobo["headers"]).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_0008_downgrades_to_0007_and_upgrades(tmp_path):
    import sqlalchemy as sa
    from alembic import command

    from lectern.db import make_engine
    from lectern.migrate import alembic_config, upgrade_to_head

    engine = make_engine(f"sqlite:///{tmp_path / 'm8.db'}")
    upgrade_to_head(engine)
    assert "device_queue_items" in sa.inspect(engine).get_table_names()
    cfg = alembic_config(str(engine.url))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0007")
    tables = sa.inspect(engine).get_table_names()
    assert "device_queue_items" not in tables
    assert "instance_settings" in tables  # 0007 (add & edit books) is left alone
    upgrade_to_head(engine)
    assert "device_queue_items" in sa.inspect(engine).get_table_names()


def test_queue_and_root_feeds_parse_with_legacy_control_characters(home, monkeypatch):
    """Rows written before PR #6 validated metadata (and device labels, which were never
    validated) may hold XML 1.0-illegal controls; the root, queue, and a paged search must
    still be well-formed XML. Regression for the raw ``&q=`` in next/previous links too."""
    owner, ids = home["owner"], home["ids"]
    kobo = owner.devices["Kobo"]
    from lectern.models import AppPassword

    with session_factory()() as db:
        db.get(AppPassword, kobo["id"]).label = "Ko\x01bo <&>"
        w = db.get(Work, ids["Alpha"])
        w.title, w.author, w.description = "Al\x0bpha & co", "A\x1futhor", "bell\x07 <b>"
        db.commit()
    owner.queue(kobo["id"], ids["Alpha"])
    root = feed(owner.http, "/opds", kobo["headers"])
    queue = [e for e in root.iter(f"{ATOM}entry") if e.findtext(f"{ATOM}id") == "urn:lectern:queue"]
    assert queue[0].findtext(f"{ATOM}content") == "1 book sent to Kobo <&>"
    assert titles(feed(owner.http, "/opds/queue", kobo["headers"])) == ["Alpha & co"]
    monkeypatch.setattr("lectern.routes.opds.PAGE_SIZE", 1)
    got = feed(owner.http, "/opds/search?q=author", kobo["headers"])
    assert int(got.findtext(f"{OS}totalResults")) >= 2
    links = {e.get("rel"): e.get("href") for e in got.iter(f"{ATOM}link")}
    assert links["next"] == "/opds/search?page=2&q=author"


# --- following a shelf (issue #68) ---------------------------------------------------------------


def follow(person: Person, device_id: int, shelf_id: int | None, **opts):
    r = api_post(
        person.http,
        f"/api/devices/app-passwords/{device_id}/shelf-sync",
        person.csrf,
        {"shelf_id": shelf_id, **opts},
    )
    return r


def pending_titles(person: Person, device_id: int) -> list[str]:
    r = person.http.get(f"/api/device-queues/{device_id}")
    assert r.status_code == 200, r.text
    return [i["work"]["title"] for i in r.json()["pending"]]


def plugin_status(person: Person, dev: dict) -> dict:
    r = person.http.get("/koplugin/status", headers=dev["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def test_a_followed_shelf_keeps_the_device_stocked_and_cleans_up(home):
    owner, ids = home["owner"], home["ids"]
    kobo, phone = owner.devices["Kobo"], owner.devices["Phone"]
    top = api_post(owner.http, "/api/shelves", owner.csrf, {"name": "Kobo books"}).json()
    sub = api_post(
        owner.http, "/api/shelves", owner.csrf, {"name": "Later", "parent_id": top["id"]}
    ).json()
    for shelf, title in ((top, "Alpha"), (sub, "Charlie"), (top, "Echo")):
        api_post(
            owner.http, f"/api/shelves/{shelf['id']}/items", owner.csrf, {"work_id": ids[title]}
        )

    r = follow(owner, kobo["id"], top["id"], cleanup=True, auto_download=True)
    assert r.status_code == 200, r.text
    ap = r.json()["app_password"]
    assert (ap["follow_shelf_id"], ap["follow_cleanup"], ap["auto_download"]) == (
        top["id"],
        True,
        True,
    )
    # The shelf and the one nested in it, books with a file only (Echo is audio only), queued
    # at once; the same person's other device follows nothing.
    assert sorted(pending_titles(owner, kobo["id"])) == ["Alpha", "Charlie"]
    assert pending_titles(owner, phone["id"]) == []
    # A book sent by hand sits next to them and is never cleaned up.
    assert statuses(owner.queue(kobo["id"], ids["Delta"])) == {ids["Delta"]: "queued"}

    # A book put on the shelf reaches the device's next read of its queue (OPDS here).
    api_post(owner.http, f"/api/shelves/{top['id']}/items", owner.csrf, {"work_id": ids["Bravo"]})
    assert sorted(titles(feed(owner.http, "/opds/queue", kobo["headers"]))) == [
        "Alpha",
        "Bravo",
        "Charlie",
        "Delta",
    ]
    # Alpha reaches the device; a person takes Bravo off the queue.
    file_id = home["files"]["Alpha"][0]
    assert (
        owner.http.get(f"/opds/files/{file_id}/download", headers=kobo["headers"]).status_code
        == 200
    )
    assert item_status(kobo["id"], ids["Alpha"]) == QueueStatus.DELIVERED
    bravo = next(
        i["id"]
        for i in owner.http.get(f"/api/device-queues/{kobo['id']}").json()["pending"]
        if i["work"]["title"] == "Bravo"
    )
    api_delete(owner.http, f"/api/device-queues/{kobo['id']}/items/{bravo}", owner.csrf)

    # Alpha (delivered) and Charlie (still waiting) leave the shelf: both are dequeued and the
    # plugin is told to delete its copies; Delta, sent by hand, stays; Bravo, removed by a
    # person, is not queued again by the shelf.
    for shelf, title in ((top, "Alpha"), (sub, "Charlie")):
        r = api_delete(owner.http, f"/api/shelves/{shelf['id']}/items/{ids[title]}", owner.csrf)
        assert r.status_code == 204
    st = plugin_status(owner, kobo)
    assert sorted(st["remove"]) == sorted([ids["Alpha"], ids["Charlie"]])
    assert [i["title"] for i in st["queue"]["items"]] == ["Delta"]
    assert st["auto_download"] is True
    assert st["follow"] == {"shelf": "Kobo books", "cleanup": True}
    assert item_status(kobo["id"], ids["Bravo"]) == QueueStatus.REMOVED

    # Alpha comes back: queued again, and no longer to be deleted.
    api_post(owner.http, f"/api/shelves/{top['id']}/items", owner.csrf, {"work_id": ids["Alpha"]})
    st = plugin_status(owner, kobo)
    assert st["remove"] == [ids["Charlie"]]
    assert sorted(i["title"] for i in st["queue"]["items"]) == ["Alpha", "Delta"]

    # Clean-up off: a book leaving the shelf stays queued, and nothing is to be deleted.
    assert follow(owner, kobo["id"], top["id"], cleanup=False).status_code == 200
    api_delete(owner.http, f"/api/shelves/{top['id']}/items/{ids['Alpha']}", owner.csrf)
    st = plugin_status(owner, kobo)
    assert st["remove"] == [] and st["auto_download"] is False
    assert sorted(i["title"] for i in st["queue"]["items"]) == ["Alpha", "Delta"]

    # Deleting the shelf ends the follow; unfollowing leaves the queue as it is.
    api_delete(owner.http, f"/api/shelves/{sub['id']}", owner.csrf)
    assert follow(owner, kobo["id"], None).json()["app_password"]["follow_shelf_id"] is None
    assert plugin_status(owner, kobo)["follow"] is None
    with session_factory()() as db:
        actions = db.scalars(select(AuditLog.action)).all()
    assert "device.shelf_sync" in actions and "queue.follow" in actions


def test_a_smart_shelf_rule_change_takes_effect_at_the_next_sync(home):
    owner, ids = home["owner"], home["ids"]
    kobo = owner.devices["Kobo"]
    smart = api_post(
        owner.http, "/api/shelves", owner.csrf, {"name": "Smart", "filter": 'author:"Author Bravo"'}
    ).json()
    follow(owner, kobo["id"], smart["id"], cleanup=True)
    assert pending_titles(owner, kobo["id"]) == ["Bravo"]
    r = owner.http.patch(
        f"/api/shelves/{smart['id']}",
        json={"filter": 'author:"Author Delta"'},
        headers={"origin": ORIGIN, "x-csrf-token": owner.csrf},
    )
    assert r.status_code == 200, r.text
    assert titles(feed(owner.http, "/opds/queue", kobo["headers"])) == ["Delta"]
    assert plugin_status(owner, kobo)["remove"] == [ids["Bravo"]]


def test_shelf_sync_respects_visibility_and_ownership(home):
    owner, mia, rex, ids = home["owner"], home["mia"], home["rex"], home["ids"]
    boox, kobo = rex.devices["Boox"], owner.devices["Kobo"]
    private = api_post(owner.http, "/api/shelves", owner.csrf, {"name": "Private"}).json()
    # A shelf the device's user cannot see does not exist for them; another person's device
    # is not theirs to set, owner included (as with the download cap).
    assert follow(rex, boox["id"], private["id"]).status_code == 404
    assert follow(mia, boox["id"], home["shelf"]).status_code == 404
    assert follow(owner, boox["id"], home["shelf"]).status_code == 404
    # A smart shelf rex sees matches only what rex may see: the shared books.
    smart = api_post(
        owner.http, "/api/shelves", owner.csrf, {"name": "All", "filter": "format:epub"}
    ).json()
    put_json(
        owner.http,
        f"/api/shelves/{smart['id']}/grants/{home['users']['rex']['id']}",
        owner.csrf,
        {"permission": "view"},
    )
    assert follow(rex, boox["id"], smart["id"], cleanup=True).status_code == 200
    assert sorted(pending_titles(rex, boox["id"])) == ["Alpha", "Bravo"]
    # Unshared: the books leave rex's view, so the follow dequeues them and the plugin is told.
    api_delete(
        owner.http, f"/api/shelves/{home['shelf']}/grants/{home['users']['rex']['id']}", owner.csrf
    )
    st = plugin_status(rex, boox)
    assert st["queue"]["items"] == [] and sorted(st["remove"]) == sorted(
        [ids["Alpha"], ids["Bravo"]]
    )
    # The followed shelf is hidden from rex now: its other settings can still be changed
    # (keeping it), but no other hidden shelf can be picked.
    api_delete(
        owner.http, f"/api/shelves/{smart['id']}/grants/{home['users']['rex']['id']}", owner.csrf
    )
    r = follow(rex, boox["id"], smart["id"], cleanup=True, auto_download=True)
    assert r.status_code == 200 and r.json()["app_password"]["auto_download"] is True
    assert follow(rex, boox["id"], private["id"]).status_code == 404
    # The owner's own device is unaffected by rex's follow.
    assert pending_titles(owner, kobo["id"]) == []
    # A revoked device can no longer be set.
    device_id = rex.http.get("/api/devices").json()["devices"][0]["id"]
    api_post(rex.http, f"/api/devices/{device_id}/revoke", rex.csrf)
    assert follow(rex, boox["id"], None).status_code == 409


def test_a_large_shelf_is_queued_a_batch_at_a_time(home, monkeypatch):
    owner, ids = home["owner"], home["ids"]
    kobo = owner.devices["Kobo"]
    monkeypatch.setattr(device_queue, "FOLLOW_WINDOW", 2)
    shelf = api_post(owner.http, "/api/shelves", owner.csrf, {"name": "Big"}).json()
    for t in ("Delta", "Alpha", "Charlie"):
        api_post(owner.http, f"/api/shelves/{shelf['id']}/items", owner.csrf, {"work_id": ids[t]})
    follow(owner, kobo["id"], shelf["id"])
    # The first two in title order wait; the third follows once one of them is delivered.
    assert pending_titles(owner, kobo["id"]) == ["Alpha", "Charlie"]
    file_id = home["files"]["Alpha"][0]
    owner.http.get(f"/opds/files/{file_id}/download", headers=kobo["headers"])
    assert titles(feed(owner.http, "/opds/queue", kobo["headers"])) == ["Charlie", "Delta"]


def test_sending_a_followed_book_by_hand_keeps_it_through_clean_up(home):
    owner, ids = home["owner"], home["ids"]
    kobo = owner.devices["Kobo"]
    shelf = api_post(owner.http, "/api/shelves", owner.csrf, {"name": "Follow"}).json()
    api_post(owner.http, f"/api/shelves/{shelf['id']}/items", owner.csrf, {"work_id": ids["Alpha"]})
    follow(owner, kobo["id"], shelf["id"], cleanup=True)
    assert statuses(owner.queue(kobo["id"], ids["Alpha"])) == {ids["Alpha"]: "already_queued"}
    api_delete(owner.http, f"/api/shelves/{shelf['id']}/items/{ids['Alpha']}", owner.csrf)
    st = plugin_status(owner, kobo)
    assert st["remove"] == [] and [i["title"] for i in st["queue"]["items"]] == ["Alpha"]
