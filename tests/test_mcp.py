"""AI access: the MCP endpoint at /mcp and AI keys (docs/mcp.md)."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.conftest import (
    ORIGIN,
    OWNER_PASSWORD,
    api_post,
    login,
    make_app_password,
    make_epub,
    make_user,
    put_json,
)

from lectern import mcp_server
from lectern.config import Settings
from lectern.db import session_factory
from lectern.ingest.watcher import InboxWatcher
from lectern.models import AIKey, AuditLog, Notification, Work
from lectern.security.ratelimit import SlidingWindowLimiter

HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}

# What each tool needs, straight from the feature's definition of the scopes.
TOOL_SCOPES = {
    "upload_book": "upload",
    "list_pending_imports": "upload",
    "confirm_import": "upload",
    "cancel_import": "upload",
    "search_library": "read",
    "get_book": "read",
    "list_recent": "read",
    "add_to_shelf": "organize",
    "remove_from_shelf": "organize",
    "send_to_device": "organize",
    "list_device_queue": "organize",
    "start_alignment": "organize",
    "job_status": "organize",
    "edit_metadata": "organize",
}
ARGS = {
    "upload_book": {"filename": "x.epub", "content_base64": base64.b64encode(b"PK").decode()},
    "confirm_import": {"import_id": "nope"},
    "cancel_import": {"import_id": "nope"},
    "get_book": {"book_id": 1},
    "add_to_shelf": {"book_id": 1, "shelf": "Shared"},
    "remove_from_shelf": {"book_id": 1, "shelf": "Shared"},
    "send_to_device": {"book_ids": [1], "device": "Kobo"},
    "start_alignment": {"book_id": 1},
    "job_status": {"book_id": 1},
    "edit_metadata": {"book_id": 1, "changes": {"publisher": "P"}},
}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        base_url=ORIGIN,
        insecure_http=True,
        rate_limit_per_minute=10_000,
    )


class Person:
    def __init__(self, app, username: str, password: str):
        self.http = TestClient(app)
        self.username = username
        self.csrf = login(self.http, username, password)

    def key(self, *scopes: str, label: str = "Claude") -> str:
        body = {"label": label, **({"scopes": list(scopes)} if scopes else {})}
        r = api_post(self.http, "/api/ai-keys", self.csrf, body)
        assert r.status_code == 201, r.text
        return r.json()["secret"]


class MCP:
    """A raw JSON-RPC caller (the SDK client is exercised end to end below)."""

    def __init__(self, app, key: str | None):
        self.http = TestClient(app)
        self.key = key

    def post(self, method: str, params: dict | None = None):
        headers = dict(HEADERS)
        if self.key:
            headers["authorization"] = f"Bearer {self.key}"
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        return self.http.post("/mcp", json=body, headers=headers)

    def tools(self) -> list[dict]:
        r = self.post("tools/list")
        assert r.status_code == 200, r.text
        return r.json()["result"]["tools"]

    def call(self, name: str, **arguments) -> tuple[bool, object]:
        """(ok, parsed result or error text)."""
        r = self.post("tools/call", {"name": name, "arguments": arguments})
        assert r.status_code == 200, r.text
        result = r.json()["result"]
        text = result["content"][0]["text"] if result["content"] else ""
        if result.get("isError"):
            return False, text
        return True, json.loads(text)

    def ok(self, name: str, **arguments):
        ok, out = self.call(name, **arguments)
        assert ok, out
        return out


def ingest(settings: Settings, *titles: str) -> dict[str, int]:
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    for i, t in enumerate(titles):
        make_epub(settings.inbox_path / f"{t}.epub", title=t, authors=[f"Author {t}"], padding=i)
    w = InboxWatcher(settings.inbox_path, settings.library_dir, session_factory(), 0.01)
    w.scan_once()
    w.scan_once()
    with session_factory()() as db:
        return {w.title: w.id for w in db.scalars(select(Work))}


@pytest.fixture
def home(client, owner, app, settings):
    """Owner, member mia, reader rex. Works Alpha, Bravo (on "Shared", granted to rex) and
    Hidden. The owner has an e-reader "Kobo"."""
    ids = ingest(settings, "Alpha", "Bravo", "Hidden")
    csrf = owner["csrf"]
    mia = make_user(client, csrf, "mia", "member")
    rex = make_user(client, csrf, "rex", "reader")
    r = api_post(client, "/api/shelves", csrf, {"name": "Shared"})
    shelf = r.json()["id"]
    for t in ("Alpha", "Bravo"):
        assert api_post(client, f"/api/shelves/{shelf}/items", csrf, {"work_id": ids[t]}).is_success
    r = put_json(client, f"/api/shelves/{shelf}/grants/{rex['id']}", csrf, {"permission": "view"})
    assert r.status_code == 200, r.text
    api_post(client, "/api/shelves", csrf, {"name": "Favourites"})
    make_app_password(client, csrf, "Kobo")
    return {
        "ids": ids,
        "shelf": shelf,
        "owner": Person(app, "owner", OWNER_PASSWORD),
        "mia": Person(app, "mia", mia["password"]),
        "rex": Person(app, "rex", rex["password"]),
    }


def audit_rows(action: str) -> list[AuditLog]:
    with session_factory()() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.action == action)))


# --- keys -----------------------------------------------------------------------------------------


def test_keys_are_shown_once_stored_hashed_least_privilege_and_revocable(app, home):
    owner = home["owner"]
    r = api_post(owner.http, "/api/ai-keys", owner.csrf, {"label": "Claude Desktop"})
    assert r.status_code == 201
    created = r.json()
    secret = created["secret"]
    assert secret.startswith("lak_") and created["key"]["scopes"] == ["upload"]  # default
    assert created["endpoint"] == f"{ORIGIN}/mcp"
    listed = owner.http.get("/api/ai-keys").json()
    assert secret not in json.dumps(listed)
    assert listed["keys"][0]["last_used_at"] is None
    with session_factory()() as db:
        row = db.scalar(select(AIKey))
        assert row.key_hash == hashlib.sha256(secret.encode()).hexdigest()
        assert secret not in json.dumps([row.label, row.prefix, row.scopes, row.key_hash])

    mcp = MCP(app, secret)
    assert mcp.post("tools/list").status_code == 200
    status = owner.http.get(f"/api/ai-keys/{created['key']['id']}").json()
    assert status["connected"] and status["key"]["last_used_at"]

    # Another person cannot see or revoke it.
    mia = home["mia"]
    assert mia.http.get(f"/api/ai-keys/{created['key']['id']}").status_code == 404
    r = api_post(mia.http, f"/api/ai-keys/{created['key']['id']}/revoke", mia.csrf)
    assert r.status_code == 404

    r = api_post(owner.http, f"/api/ai-keys/{created['key']['id']}/revoke", owner.csrf)
    assert r.json()["key"]["revoked_at"]
    assert mcp.post("tools/list").status_code == 401
    assert [a.target for a in audit_rows("ai_key.revoke")] == ["Claude Desktop"]


def test_unknown_malformed_and_revoked_keys_are_rejected(app, home):
    for key in (None, "nope", "lak_" + "x" * 43, "lw_whatever"):
        r = MCP(app, key).post("tools/list")
        assert r.status_code == 401 and "Bearer" in r.headers["www-authenticate"]
    owner = home["owner"]
    secret = owner.key("read")
    mcp = MCP(app, secret)
    assert mcp.ok("search_library")["total"] == 3
    key_id = owner.http.get("/api/ai-keys").json()["keys"][0]["id"]
    api_post(owner.http, f"/api/ai-keys/{key_id}/revoke", owner.csrf)
    assert mcp.post("tools/call", {"name": "search_library", "arguments": {}}).status_code == 401


def test_a_key_stops_working_when_its_user_is_disabled(app, home, client, owner):
    mcp = MCP(app, home["mia"].key("read"))
    assert mcp.post("tools/list").status_code == 200
    mia_id = next(
        u["id"] for u in client.get("/api/admin/users").json()["users"] if u["username"] == "mia"
    )
    assert api_post(client, f"/api/admin/users/{mia_id}/disable", owner["csrf"]).is_success
    assert mcp.post("tools/list").status_code == 401


def test_an_ai_key_opens_nothing_but_mcp(app, home):
    owner = home["owner"]
    secret = owner.key("upload", "read", "organize")
    bearer = {"authorization": f"Bearer {secret}"}
    anon = TestClient(app)
    file_id = anon.get("/api/me").status_code  # not signed in
    assert file_id == 401
    for url in (
        "/api/me",
        "/api/ai-keys",
        "/api/library",
        "/api/admin/users",
        "/library/files/1/download",
        f"/library/works/{home['ids']['Alpha']}/cover.jpg",
        "/opds",
    ):
        r = anon.get(url, headers=bearer)
        assert r.status_code == 401, url
    r = anon.post("/api/ai-keys", json={"label": "x"}, headers={**bearer, "origin": ORIGIN})
    assert r.status_code == 401


def test_readers_cannot_hold_upload_and_default_to_read(app, home):
    rex = home["rex"]
    listed = rex.http.get("/api/ai-keys").json()
    assert listed["scopes"] == ["read", "organize"] and listed["default_scopes"] == ["read"]
    r = api_post(rex.http, "/api/ai-keys", rex.csrf, {"label": "x", "scopes": ["upload"]})
    assert r.status_code == 403
    r = api_post(rex.http, "/api/ai-keys", rex.csrf, {"label": "x"})
    assert r.json()["key"]["scopes"] == ["read"]


def test_labels_with_control_characters_are_refused(home):
    owner = home["owner"]
    r = api_post(owner.http, "/api/ai-keys", owner.csrf, {"label": "Claude\x07"})
    assert r.status_code == 422 and r.json()["errors"][0]["type"] == "control_characters"


# --- the tool surface -----------------------------------------------------------------------------


def test_the_tools_are_exactly_these_and_none_downloads(app, home):
    tools = MCP(app, home["owner"].key("upload", "read", "organize")).tools()
    names = {t["name"] for t in tools}
    assert names == set(TOOL_SCOPES)
    for t in tools:
        text = json.dumps(t).lower()
        assert not any(w in t["name"] for w in ("download", "file", "audio", "export", "cover"))
        for word in ("user", "invite", "setting", "password", "ai_key"):
            assert word not in json.dumps(t["inputSchema"]).lower(), (t["name"], word)
        assert "http://" not in text and "/library/" not in text


def test_no_tool_result_carries_a_download_link(app, home, settings):
    mcp = MCP(app, home["owner"].key("upload", "read", "organize"))
    ids = home["ids"]
    outputs = [
        mcp.ok("search_library"),
        mcp.ok("get_book", book_id=ids["Alpha"]),
        mcp.ok("list_recent"),
        mcp.ok("send_to_device", book_ids=[ids["Alpha"]], device="Kobo"),
        mcp.ok("list_device_queue", device="Kobo"),
    ]
    epub = make_epub(settings.data_dir / "Upload.epub", title="Upload", cover=True)
    outputs.append(
        mcp.ok("upload_book", filename="Upload.epub",
               content_base64=base64.b64encode(epub.read_bytes()).decode())
    )  # fmt: skip
    text = json.dumps(outputs)
    for leak in ("/library/files", "download_url", "/download", "cover.jpg", "read-aloud.epub"):
        assert leak not in text


@pytest.mark.parametrize("tool", sorted(TOOL_SCOPES))
def test_every_tool_requires_its_scope(app, home, tool):
    needed = TOOL_SCOPES[tool]
    others = [s for s in ("upload", "read", "organize") if s != needed]
    owner = home["owner"]
    without = MCP(app, owner.key(*others, label="without"))
    ok, err = without.call(tool, **ARGS.get(tool, {}))
    assert not ok and f"does not have the '{needed}' scope" in err
    with_only = MCP(app, owner.key(needed, label="with"))
    ok, out = with_only.call(tool, **ARGS.get(tool, {}))
    assert ok or "scope" not in out, out
    with session_factory()() as db:
        denied = db.scalars(
            select(AuditLog).where(AuditLog.action == "mcp.call", AuditLog.target == tool)
        ).all()
    assert [a.detail.split(": ", 1)[1] for a in denied][0] == f"denied (needs {needed})"
    assert len(denied) == 2  # one audit row per call, denied or not


def test_readers_see_only_their_shelves(app, home):
    ids = home["ids"]
    rex = MCP(app, home["rex"].key("read", "organize"))
    found = rex.ok("search_library")
    assert {b["title"] for b in found["books"]} == {"Alpha", "Bravo"}
    assert rex.ok("search_library", query="Hidden")["total"] == 0
    ok, err = rex.call("get_book", book_id=ids["Hidden"])
    assert not ok and "not_found" in err
    assert ids["Hidden"] not in {b["id"] for b in rex.ok("list_recent", limit=50)["books"]}
    ok, err = rex.call("send_to_device", book_ids=[ids["Hidden"]], device="Kobo")
    assert not ok and "not_found" in err  # the owner's device is not rex's either
    ok, err = rex.call("add_to_shelf", book_id=ids["Alpha"], shelf="Favourites")
    assert not ok and "not_found" in err  # the owner's shelf is invisible to rex
    ok, err = rex.call("add_to_shelf", book_id=ids["Alpha"], shelf="Shared")
    assert not ok and "View-only" in err
    ok, err = rex.call("edit_metadata", book_id=ids["Alpha"], changes={"publisher": "X"})
    assert not ok and "readers cannot" in err
    ok, err = rex.call("job_status", book_id=ids["Hidden"])
    assert not ok


def test_readers_cannot_upload_even_with_a_tampered_key(app, home, settings):
    secret = home["rex"].key("read")
    with session_factory()() as db:  # someone with database access widens the key by hand
        key = db.scalar(select(AIKey))
        key.scopes = ["upload", "read", "organize"]
        db.commit()
    rex = MCP(app, secret)
    epub = make_epub(settings.data_dir / "R.epub", title="R")
    ok, err = rex.call(
        "upload_book",
        filename="R.epub",
        content_base64=base64.b64encode(epub.read_bytes()).decode(),
    )
    assert not ok and "readers cannot" in err
    for tool in ("list_pending_imports",):
        ok, err = rex.call(tool)
        assert not ok and "readers cannot" in err
    with session_factory()() as db:
        assert db.scalar(select(Work).where(Work.title == "R")) is None


# --- upload and review ----------------------------------------------------------------------------


def test_upload_review_and_file_a_new_book(app, home, settings):
    mcp = MCP(app, home["mia"].key("upload", "read"))
    epub = make_epub(settings.data_dir / "Echo.epub", title="Echo", authors=["E. Writer"],
                     isbn="9780000000002")  # fmt: skip
    out = mcp.ok(
        "upload_book",
        filename="Echo.epub",
        content_base64=base64.b64encode(epub.read_bytes()).decode(),
    )
    assert out["status"] == "ready"
    assert out["detected"]["title"] == "Echo" and out["proposal"]["action"] == "new"
    pending = mcp.ok("list_pending_imports")["imports"]
    assert [p["import_id"] for p in pending] == [out["import_id"]]
    filed = mcp.ok(
        "confirm_import", import_id=out["import_id"], metadata={"title": "Echo (Revised)"}
    )
    assert filed["created"] and filed["book"]["title"] == "Echo (Revised)"
    assert mcp.ok("list_pending_imports")["imports"] == []
    book = mcp.ok("get_book", book_id=filed["book"]["id"])
    assert book["metadata"]["authors"][0]["name"] == "E. Writer"
    assert [f["kind"] for f in book["formats"]] == ["ebook"]


def test_upload_proposes_joining_an_existing_book(app, home, settings):
    mcp = MCP(app, home["owner"].key("upload"))
    epub = make_epub(settings.data_dir / "Alpha2.epub", title="Alpha", authors=["Author Alpha"],
                     padding=999)  # fmt: skip
    out = mcp.ok(
        "upload_book",
        filename="Alpha.epub",
        content_base64=base64.b64encode(epub.read_bytes()).decode(),
    )
    assert out["proposal"]["action"] == "join"
    assert out["proposal"]["work_id"] == home["ids"]["Alpha"]
    filed = mcp.ok("confirm_import", import_id=out["import_id"])
    assert not filed["created"] and filed["book"]["id"] == home["ids"]["Alpha"]


def test_chunked_resumable_upload_and_cancel(app, home, settings, monkeypatch):
    monkeypatch.setattr(mcp_server, "CHUNK_BYTES", 4096)
    mcp = MCP(app, home["owner"].key("upload"))
    data = make_epub(settings.data_dir / "Big.epub", title="Big", padding=20000).read_bytes()
    assert len(data) > 2 * 4096
    whole = hashlib.sha256(data).hexdigest()
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731

    first = mcp.ok("upload_book", filename="Big.epub", content_base64=b64(data[:4096]),
                   total_size=len(data), sha256=whole)  # fmt: skip
    assert first["status"] == "uploading" and first["next_offset"] == 4096
    uid = first["upload_id"]
    # A chunk at the wrong offset is refused and says where to resume.
    ok, err = mcp.call("upload_book", filename="Big.epub", upload_id=uid, offset=0,
                       content_base64=b64(data[4096:8192]))  # fmt: skip
    assert not ok and "offset_mismatch" in err
    # Resuming without content reports progress.
    assert mcp.ok("upload_book", filename="Big.epub", upload_id=uid)["received_bytes"] == 4096
    offset = 4096
    while offset < len(data):
        out = mcp.ok("upload_book", filename="Big.epub", upload_id=uid, offset=offset,
                     content_base64=b64(data[offset : offset + 4096]))  # fmt: skip
        offset += 4096
    assert out["status"] == "ready" and out["detected"]["title"] == "Big"
    assert mcp.ok("cancel_import", import_id=out["import_id"])["status"] == "discarded"
    assert mcp.ok("list_pending_imports")["imports"] == []
    with session_factory()() as db:
        assert db.scalar(select(Work).where(Work.title == "Big")) is None


def test_a_bad_whole_file_checksum_fails_the_upload(app, home, settings):
    mcp = MCP(app, home["owner"].key("upload"))
    data = make_epub(settings.data_dir / "C.epub", title="C").read_bytes()
    ok, err = mcp.call("upload_book", filename="C.epub", total_size=len(data), sha256="0" * 64,
                       content_base64=base64.b64encode(data).decode())  # fmt: skip
    assert not ok and "checksum_mismatch" in err
    ok, err = mcp.call("upload_book", filename="C.pdf", content_base64="AAAA")
    assert not ok and "unsupported_media_type" in err


# --- organize -------------------------------------------------------------------------------------


def test_shelves_devices_metadata_and_alignment(app, home):
    ids = home["ids"]
    mcp = MCP(app, home["owner"].key("read", "organize"))
    assert mcp.ok("add_to_shelf", book_id=ids["Hidden"], shelf="favourites")["on_shelf"]
    assert {"id": home["shelf"], "name": "Shared"} not in mcp.ok("get_book", book_id=ids["Hidden"])[
        "shelves"
    ]
    assert [s["name"] for s in mcp.ok("get_book", book_id=ids["Hidden"])["shelves"]] == [
        "Favourites"
    ]
    assert not mcp.ok("remove_from_shelf", book_id=ids["Hidden"], shelf="Favourites")["on_shelf"]
    assert mcp.ok("get_book", book_id=ids["Hidden"])["shelves"] == []

    sent = mcp.ok("send_to_device", book_ids=[ids["Alpha"], ids["Alpha"], 9999], device="kobo")
    assert [r["status"] for r in sent["results"]] == ["queued", "not_found"]
    assert (
        mcp.ok("send_to_device", book_ids=[ids["Alpha"]], device="Kobo")["results"][0]["status"]
        == "already_queued"
    )
    queue = mcp.ok("list_device_queue", device="Kobo")
    assert [i["book"]["title"] for i in queue["pending"]] == ["Alpha"]
    assert [d["label"] for d in mcp.ok("list_device_queue")["devices"]] == ["Kobo"]
    ok, err = mcp.call("send_to_device", book_ids=[ids["Alpha"]], device="Kindle")
    assert not ok and "Kobo" in err

    book = mcp.ok("get_book", book_id=ids["Bravo"])
    out = mcp.ok("edit_metadata", book_id=ids["Bravo"], expected_version=book["version"],
                 changes={"series": "Phonetic", "series_index": 2, "language": "fr-CA"})  # fmt: skip
    assert out["changed"] == ["metadata"] and out["version"] == book["version"] + 1
    assert out["metadata"]["series"] == "Phonetic" and out["metadata"]["title"] == "Bravo"
    ok, err = mcp.call("edit_metadata", book_id=ids["Bravo"], changes={"title": "Bad\x0bTitle"})
    assert not ok and "control_characters" in err
    ok, err = mcp.call("edit_metadata", book_id=ids["Bravo"], changes={"published": "May 2020"})
    assert not ok and "published" in err
    ok, err = mcp.call("edit_metadata", book_id=ids["Bravo"], expected_version=book["version"],
                       changes={"publisher": "Stale"})  # fmt: skip
    assert not ok and "version_conflict" in err
    assert mcp.ok("get_book", book_id=ids["Bravo"])["metadata"]["publisher"] == "Test Press"

    ok, err = mcp.call("start_alignment", book_id=ids["Alpha"])
    assert not ok and "no_inputs" in err  # an EPUB without an audiobook
    ok, err = mcp.call("job_status")
    assert not ok and "job_id or book_id" in err


# --- rate limits, audit, alerts -------------------------------------------------------------------


def test_each_key_is_rate_limited(app, home):
    app.state.limiters["mcp"] = SlidingWindowLimiter(limit=3, window_seconds=60)
    owner = home["owner"]
    a, b = MCP(app, owner.key("read", label="a")), MCP(app, owner.key("read", label="b"))
    assert [a.post("tools/list").status_code for _ in range(4)] == [200, 200, 200, 429]
    assert b.post("tools/list").status_code == 200  # its own bucket


def test_failed_keys_lock_out_the_address(app, home):
    app.state.limiters["mcp_fail"] = SlidingWindowLimiter(limit=3, window_seconds=60)
    good = MCP(app, home["owner"].key("read"))
    bad = MCP(app, "lak_wrong")
    assert [bad.post("tools/list").status_code for _ in range(3)] == [401] * 3
    # Over the limit even a valid key is refused before any lookup, so keys cannot be guessed.
    assert bad.post("tools/list").status_code == 429
    assert good.post("tools/list").status_code == 429


def test_audit_first_use_and_burst_alerts(app, home):
    app.state.limiters["mcp_burst"] = SlidingWindowLimiter(limit=3, window_seconds=600)
    mia = home["mia"]
    mcp = MCP(app, mia.key("read", label="Claude Code"))
    for _ in range(5):
        mcp.ok("search_library")
    calls = audit_rows("mcp.call")
    assert len(calls) == 5 and {c.target for c in calls} == {"search_library"}
    assert all("Claude Code" in c.detail and c.detail.endswith("ok") for c in calls)
    assert [a.target for a in audit_rows("ai_key.first_use")] == ["Claude Code"]
    assert len(audit_rows("ai_key.burst")) == 1  # once an hour, not per call
    with session_factory()() as db:
        notes = db.scalars(select(Notification).where(Notification.kind.like("ai_key.%"))).all()
        kinds = sorted((n.kind, n.user_id) for n in notes)
    mia_id = audit_rows("ai_key.first_use")[0].actor_user_id
    owner_id = next(n for n in notes if n.kind == "ai_key.burst" and n.user_id != mia_id).user_id
    assert kinds == sorted(
        [("ai_key.connected", mia_id), ("ai_key.burst", mia_id), ("ai_key.burst", owner_id)]
    )


# --- end to end with the official MCP Python SDK client ------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(app, setup_token, settings):
    """The app served by uvicorn on a real socket, with an owner, a book, and an AI key."""
    import httpx
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    web = httpx.Client(base_url=base, headers={"origin": ORIGIN})
    form = {"token": setup_token.token, "username": "owner", "password": OWNER_PASSWORD,
            "password_confirm": OWNER_PASSWORD}  # fmt: skip
    assert web.post("/setup", data=form).status_code in (200, 303)
    csrf = web.get("/api/me").json()["csrf_token"]
    ingest(settings, "Alpha")
    h = {"x-csrf-token": csrf}
    r = web.post("/api/ai-keys", json={"label": "SDK", "scopes": ["upload", "read"]}, headers=h)
    assert r.status_code == 201, r.text
    try:
        yield {"url": f"{base}/mcp", "key": r.json()["secret"], "id": r.json()["key"]["id"],
               "web": web, "csrf": csrf}  # fmt: skip
    finally:
        web.close()
        server.should_exit = True
        thread.join(timeout=20)


def test_end_to_end_with_the_official_python_sdk_client(live, settings):
    import anyio
    import httpx2
    from mcp import ClientSession, MCPError
    from mcp.client.streamable_http import streamable_http_client

    epub = make_epub(settings.data_dir / "Foxtrot.epub", title="Foxtrot", authors=["F. Author"])

    async def session_with(key: str, body):
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}) as http:
            async with streamable_http_client(live["url"], http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    return await body(session)

    async def main():
        async def flow(session):
            init = await session.initialize()
            assert init.server_info.name == "Lectern"
            names = {t.name for t in (await session.list_tools()).tools}
            assert names == set(TOOL_SCOPES)
            found = await session.call_tool("search_library", {"query": "alpha"})
            assert not found.is_error
            assert json.loads(found.content[0].text)["books"][0]["title"] == "Alpha"
            up = await session.call_tool(
                "upload_book",
                {"filename": "Foxtrot.epub",
                 "content_base64": base64.b64encode(epub.read_bytes()).decode()},
            )  # fmt: skip
            assert not up.is_error, up.content[0].text
            pending = json.loads(up.content[0].text)
            assert pending["detected"]["title"] == "Foxtrot"
            filed = await session.call_tool("confirm_import", {"import_id": pending["import_id"]})
            book = json.loads(filed.content[0].text)["book"]
            got = await session.call_tool("get_book", {"book_id": book["id"]})
            assert json.loads(got.content[0].text)["metadata"]["title"] == "Foxtrot"
            denied = await session.call_tool("send_to_device", {"book_ids": [1], "device": "x"})
            assert denied.is_error and "'organize' scope" in denied.content[0].text

        await session_with(live["key"], flow)

        async def rejected(session):
            with pytest.raises(MCPError):
                await session.initialize()

        await session_with("lak_not-a-key", rejected)
        r = live["web"].post(
            f"/api/ai-keys/{live['id']}/revoke", headers={"x-csrf-token": live["csrf"]}
        )
        assert r.status_code == 200
        await session_with(live["key"], rejected)

    anyio.run(main)
