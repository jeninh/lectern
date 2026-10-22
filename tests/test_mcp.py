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

from lectern import imports, mcp_server
from lectern.config import Settings
from lectern.db import session_factory
from lectern.ingest.watcher import InboxWatcher
from lectern.models import AIKey, AuditLog, Import, Notification, Work
from lectern.security.ratelimit import SlidingWindowLimiter

HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}

# What each tool needs, straight from the feature's definition of the scopes.
TOOL_SCOPES = {
    "upload_book": "upload",
    "list_pending_imports": "upload",
    "confirm_import": "file",
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
        self.password = password
        self.csrf = login(self.http, username, password)

    def key(self, *scopes: str, label: str = "Claude") -> str:
        """A new key; scopes beyond the defaults are confirmed with the password."""
        body = {"label": label, **({"scopes": list(scopes)} if scopes else {})}
        if scopes:
            body["password"] = self.password
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
    assert secret.startswith("lak_") and created["key"]["scopes"] == ["upload", "file"]  # default
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
    others = [s for s in ("upload", "file", "read", "organize") if s != needed]
    owner = home["owner"]
    without = MCP(app, owner.key(*others, label="without"))
    ok, err = without.call(tool, **ARGS.get(tool, {}))
    assert not ok and f"forbidden: needs {needed} scope" in err
    with_only = MCP(app, owner.key(needed, label="with"))
    ok, out = with_only.call(tool, **ARGS.get(tool, {}))
    assert ok or "scope" not in out, out
    with session_factory()() as db:
        denied = db.scalars(
            select(AuditLog).where(AuditLog.action == "mcp.call", AuditLog.target == tool)
        ).all()
    assert [a.detail.split(": ", 1)[1] for a in denied][0] == f"denied (needs {needed} scope)"
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
    mcp = MCP(app, home["mia"].key("upload", "file", "read"))
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


def test_upload_and_file_a_pdf_and_a_comic(app, home, settings):
    from tests.test_pdf_comics import make_cbz, make_pdf

    mcp = MCP(app, home["mia"].key("upload", "file", "read"))
    files = {
        "Atlas.pdf": make_pdf(settings.data_dir / "Atlas.pdf", title="Atlas", author="A. Maker"),
        "Strip.cbz": make_cbz(settings.data_dir / "Strip.cbz"),
    }
    labels = {}
    for name, path in files.items():
        out = mcp.ok("upload_book", filename=name,
                     content_base64=base64.b64encode(path.read_bytes()).decode())  # fmt: skip
        assert out["status"] == "ready"
        book = mcp.ok(
            "get_book", book_id=mcp.ok("confirm_import", import_id=out["import_id"])["book"]["id"]
        )
        labels[name] = [(f["kind"], f["format"]) for f in book["formats"]]
    assert labels == {"Atlas.pdf": [("ebook", "PDF")], "Strip.cbz": [("ebook", "CBZ")]}
    recent = mcp.ok("list_recent")["books"]
    assert [b["formats"] for b in recent[:2]] == [["CBZ"], ["PDF"]]
    assert ["EPUB"] in [b["formats"] for b in recent[2:]]


def test_upload_proposes_joining_an_existing_book(app, home, settings):
    mcp = MCP(app, home["owner"].key("upload", "file"))
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


def open_imports() -> list[Import]:
    with session_factory()() as db:
        return list(db.scalars(select(Import).where(Import.status.in_(imports.OPEN))))


def test_unknown_or_foreign_import_ids_are_clear_tool_errors(app, home, settings):
    """Regression: lectern.imports errors used to surface as an opaque internal error."""
    data = make_epub(settings.data_dir / "G.epub", title="G").read_bytes()
    mia = MCP(app, home["mia"].key("upload"))
    theirs = mia.ok(
        "upload_book", filename="G.epub", content_base64=base64.b64encode(data).decode()
    )
    owner = MCP(app, home["owner"].key("upload", "file"))
    for import_id in ("nope", theirs["import_id"]):
        for tool, args in (
            (
                "upload_book",
                {"filename": "G.epub", "content_base64": "UEs=", "import_id": import_id},
            ),
            ("confirm_import", {"import_id": import_id}),
            ("cancel_import", {"import_id": import_id}),
        ):
            ok, err = owner.call(tool, **args)
            assert not ok and err.endswith(
                "not_found (not found, or not visible to this person)"
            ), (
                tool,
                err,
            )
    details = [a.detail for a in audit_rows("mcp.call") if a.actor_user_id != mia_id_of(home)]
    assert not any("internal" in d for d in details)
    assert sum("error not_found" in d for d in details) == 6
    assert [i.public_id for i in open_imports()] == [theirs["import_id"]]  # mia's is untouched


def mia_id_of(home) -> int:
    return home["mia"].http.get("/api/me").json()["user"]["id"]


def test_a_refused_first_upload_leaves_no_import(app, home, settings):
    """Regression: the import was committed before the upload session was refused."""
    mcp = MCP(app, home["owner"].key("upload"))
    ok, err = mcp.call("upload_book", filename="Huge.m4b", content_base64="AAAA",
                       total_size=10**15, sha256="0" * 64)  # fmt: skip
    assert not ok and "too_large" in err
    ok, err = mcp.call("upload_book", filename="X.epub",
                       content_base64=base64.b64encode(b"x" * (mcp_server.CHUNK_BYTES + 1)).decode())  # fmt: skip
    assert not ok and "chunk_too_large" in err
    assert open_imports() == []


def test_a_bad_whole_file_checksum_fails_the_upload(app, home, settings):
    mcp = MCP(app, home["owner"].key("upload"))
    data = make_epub(settings.data_dir / "C.epub", title="C").read_bytes()
    ok, err = mcp.call("upload_book", filename="C.epub", total_size=len(data), sha256="0" * 64,
                       content_base64=base64.b64encode(data).decode())  # fmt: skip
    assert not ok and "checksum_mismatch" in err
    ok, err = mcp.call("upload_book", filename="C.cbr", content_base64="AAAA")
    assert not ok and "unsupported_media_type" in err and "convert it to CBZ" in err
    ok, err = mcp.call("upload_book", filename="C.pdf", content_base64="UEsDBA==")
    assert not ok and "not_pdf" in err  # magic bytes must match the extension
    ok, err = mcp.call("upload_book", filename="C.epub", content_base64="UEsDBA==")
    assert not ok and "not_epub" in err
    assert open_imports() == []  # neither failed one-shot upload left an import behind


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
    r = web.post(
        "/api/ai-keys",
        json={"label": "SDK", "scopes": ["upload", "file", "read"], "password": OWNER_PASSWORD},
        headers=h,
    )
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
            assert denied.is_error and "needs organize scope" in denied.content[0].text

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


# --- editing a key's scopes -----------------------------------------------------------------------


def _edit(person: Person, key_id: int, scopes: list[str], **step_up):
    return api_post(person.http, f"/api/ai-keys/{key_id}/scopes", person.csrf,
                    {"scopes": scopes, **step_up})  # fmt: skip


def _key_row(secret: str) -> AIKey:
    with session_factory()() as db:
        return db.scalar(select(AIKey).where(AIKey.prefix == secret[:12]))


def test_narrowing_applies_on_the_next_request_without_revealing_or_rotating(app, home):
    owner = home["owner"]
    secret = owner.key("upload", "file", "read", "organize")
    before = _key_row(secret)
    mcp = MCP(app, secret)
    assert mcp.call("search_library")[0]
    r = _edit(owner, before.id, ["organize", "upload"])  # no step-up needed to remove
    assert r.status_code == 200, r.text
    assert r.json()["changed"] and r.json()["key"]["scopes"] == ["upload", "organize"]
    assert secret not in r.text and "secret" not in r.json()
    after = _key_row(secret)
    assert (after.key_hash, after.prefix) == (before.key_hash, before.prefix)  # not rotated
    # The same key, the very next message: no scope is cached anywhere.
    ok, err = mcp.call("search_library")
    assert not ok and "forbidden: needs read scope" in err
    ok, err = mcp.call("confirm_import", import_id="nope")
    assert not ok and "forbidden: needs file scope" in err
    assert mcp.call("list_device_queue")[0]  # what it kept still works
    [row] = audit_rows("ai_key.scopes_change")
    assert row.target == "Claude" and row.actor_user_id is not None
    assert row.detail == "before: upload,file,read,organize; after: upload,organize"
    listed = owner.http.get("/api/ai-keys").json()
    assert secret not in json.dumps(listed)


def test_widening_needs_a_fresh_password(app, home):
    owner = home["owner"]
    secret = owner.key("read")
    key_id = _key_row(secret).id
    mcp = MCP(app, secret)
    r = _edit(owner, key_id, ["read", "file"])
    assert r.status_code == 403 and r.json()["detail"] == "step_up_required"
    assert r.json()["methods"] == ["password"]  # no passkey on this http test server
    r = _edit(owner, key_id, ["read", "file"], password="wrong password, sorry")
    assert r.status_code == 403 and r.json()["detail"] == "step_up_failed"
    assert _key_row(secret).scopes == ["read"]
    ok, err = mcp.call("confirm_import", import_id="nope")
    assert not ok and "needs file scope" in err
    [failed] = audit_rows("ai_key.step_up_failed")
    assert failed.detail == "adding file" and OWNER_PASSWORD not in failed.detail
    # A swap adds a scope, so it is a widening too.
    assert _edit(owner, key_id, ["organize"]).json()["detail"] == "step_up_required"
    r = _edit(owner, key_id, ["read", "file"], password=OWNER_PASSWORD)
    assert r.status_code == 200 and r.json()["key"]["scopes"] == ["file", "read"]
    ok, err = mcp.call("confirm_import", import_id="nope")
    assert not ok and "not_found" in err  # past the scope check now
    [row] = audit_rows("ai_key.scopes_change")
    assert row.detail == "before: read; after: file,read; confirmed with password"
    # Unchanged scopes are a no-op: no audit row, no step-up.
    assert _edit(owner, key_id, ["file", "read"]).json()["changed"] is False
    assert len(audit_rows("ai_key.scopes_change")) == 1


def test_only_the_keys_owner_can_edit_it_and_within_their_role(app, home):
    owner, mia, rex = home["owner"], home["mia"], home["rex"]
    key_id = _key_row(mia.key("read")).id
    for other in (owner, rex):  # not even the household owner
        assert _edit(other, key_id, ["organize"]).status_code == 404
    r = mia.http.post(f"/api/ai-keys/{key_id}/scopes", json={"scopes": ["organize"]})
    assert r.status_code == 403  # CSRF
    rex_key = _key_row(rex.key("read"))
    for scopes in (["read", "upload"], ["read", "file"]):
        r = _edit(rex, rex_key.id, scopes, password="rex-correct horse")
        assert r.status_code == 403 and r.json()["detail"] == "readers_cannot_upload"
    assert _edit(mia, key_id, ["read", "nope"]).status_code == 422
    assert _edit(mia, key_id, []).status_code == 422
    api_post(mia.http, f"/api/ai-keys/{key_id}/revoke", mia.csrf)
    assert _edit(mia, key_id, ["organize"]).status_code == 409
    assert _key_row(rex_key.prefix).scopes == ["read"]


def test_an_ai_key_cannot_edit_keys(app, home):
    owner = home["owner"]
    secret = owner.key("read")
    key_id = _key_row(secret).id
    anon = TestClient(app)
    r = anon.post(f"/api/ai-keys/{key_id}/scopes", json={"scopes": ["read", "organize"]},
                  headers={"authorization": f"Bearer {secret}", "origin": ORIGIN})  # fmt: skip
    assert r.status_code in (401, 403)
    assert _key_row(secret).scopes == ["read"]


def _with_passkeys(home, monkeypatch) -> list[str]:
    """Owner and mia each have a passkey; the signature check is stubbed (records who signed,
    and that user verification was required)."""
    from lectern.models import Passkey
    from lectern.security import stepup

    monkeypatch.setattr(Settings, "is_https", property(lambda self: True))
    with session_factory()() as db:
        for name in ("owner", "mia"):
            uid = home[name].http.get("/api/me").json()["user"]["id"]
            db.add(Passkey(user_id=uid, credential_id=f"{name}-cred".encode(), public_key=b"k",
                           sign_count=0, label=name))  # fmt: skip
        db.commit()
    verified: list[str] = []

    class Result:
        new_sign_count = 7

    def fake_verify(settings, body, challenge, passkey, *, require_uv=False):
        assert require_uv  # a step-up needs a PIN or biometric, not just the authenticator
        verified.append(passkey.label)
        if body.get("signature_ok") is False:
            raise ValueError("bad signature")
        return Result()

    monkeypatch.setattr(stepup, "verify_authentication", fake_verify)
    return verified


def _cred(name: str) -> dict:
    return {"rawId": base64.urlsafe_b64encode(f"{name}-cred".encode()).decode().rstrip("=")}


def _challenge(person: Person) -> None:
    assert api_post(person.http, "/api/security/step-up/options", person.csrf).status_code == 200


def test_widening_with_a_passkey_needs_this_persons_fresh_challenge(app, home, monkeypatch):
    verified = _with_passkeys(home, monkeypatch)
    owner, mia = home["owner"], home["mia"]
    key_id = _key_row(owner.key("read")).id
    assert owner.http.get("/api/ai-keys").json()["step_up"] == ["passkey", "password"]

    # No challenge issued: refused.
    r = _edit(owner, key_id, ["read", "organize"], passkey=_cred("owner"))
    assert r.json()["detail"] == "step_up_failed" and verified == []
    # Someone else's passkey, even with this person's challenge: refused.
    _challenge(owner)
    r = _edit(owner, key_id, ["read", "organize"], passkey=_cred("mia"))
    assert r.json()["detail"] == "step_up_failed" and verified == []
    # A failing signature: refused.
    _challenge(owner)
    r = _edit(
        owner, key_id, ["read", "organize"], passkey={**_cred("owner"), "signature_ok": False}
    )
    assert r.json()["detail"] == "step_up_failed" and verified == ["owner"]
    # Mia's challenge cookie does not work in the owner's session.
    _challenge(mia)
    owner.http.cookies.set("lectern_step_up", mia.http.cookies.get("lectern_step_up"))
    r = _edit(owner, key_id, ["read", "organize"], passkey=_cred("owner"))
    assert r.json()["detail"] == "step_up_failed"
    # A sign-in challenge is not a step-up challenge (challenges are tagged by purpose).
    anon = TestClient(app)
    assert anon.post("/login/passkey/options", headers={"origin": ORIGIN}).status_code == 200
    owner.http.cookies.set("lectern_step_up", anon.cookies.get("lectern_wa"))
    r = _edit(owner, key_id, ["read", "organize"], passkey=_cred("owner"))
    assert r.json()["detail"] == "step_up_failed"
    assert verified == ["owner"]  # none of these reached the signature check
    # This person's fresh challenge and passkey: allowed, once.
    owner.http.cookies.delete("lectern_step_up")
    _challenge(owner)
    r = _edit(owner, key_id, ["read", "organize"], passkey=_cred("owner"))
    assert r.status_code == 200 and r.json()["key"]["scopes"] == ["read", "organize"]
    assert audit_rows("ai_key.scopes_change")[0].detail.endswith("confirmed with passkey")
    r = _edit(owner, key_id, ["upload", "read", "organize"], passkey=_cred("owner"))
    assert r.json()["detail"] == "step_up_failed"  # the challenge was used up


def test_a_session_alone_cannot_get_a_wider_key(app, home, monkeypatch):
    """Review finding: a stolen session (no password, no passkey) used to get full scopes by
    creating a new key, or by registering its own passkey and stepping up with it."""
    _with_passkeys(home, monkeypatch)
    owner = home["owner"]
    everything = ["upload", "file", "read", "organize"]
    # 1. Creating a key wider than the defaults needs the step-up too.
    for scopes in (everything, ["read"], ["upload", "file", "organize"]):
        r = api_post(owner.http, "/api/ai-keys", owner.csrf, {"label": "evil", "scopes": scopes})
        assert r.status_code == 403 and r.json()["detail"] == "step_up_required", scopes
    r = api_post(owner.http, "/api/ai-keys", owner.csrf,
                 {"label": "evil", "scopes": everything, "password": "guess"})  # fmt: skip
    assert r.json()["detail"] == "step_up_failed"
    with session_factory()() as db:
        assert db.scalar(select(AIKey)) is None
    assert [a.detail for a in audit_rows("ai_key.step_up_failed")] == [
        "creating with read,organize"
    ]
    # The defaults (adding and filing books) need nothing extra, as before.
    r = api_post(owner.http, "/api/ai-keys", owner.csrf, {"label": "plain"})
    assert r.status_code == 201 and r.json()["key"]["scopes"] == ["upload", "file"]
    r = api_post(owner.http, "/api/ai-keys", owner.csrf,
                 {"label": "ok", "scopes": everything, "password": OWNER_PASSWORD})  # fmt: skip
    assert r.status_code == 201 and r.json()["key"]["scopes"] == everything
    assert audit_rows("ai_key.create")[-1].detail == (
        "upload,file,read,organize; confirmed with password"
    )
    # 2. Registering a passkey (a new step-up factor) needs a step-up itself.
    r = api_post(owner.http, "/api/security/passkeys/options", owner.csrf)
    assert r.status_code == 403 and r.json()["detail"] == "step_up_required"
    r = api_post(owner.http, "/api/security/passkeys/options", owner.csrf, {"password": "guess"})
    assert r.json()["detail"] == "step_up_failed"
    assert len(audit_rows("passkey.step_up_failed")) == 1
    assert "lectern_wa_reg" not in owner.http.cookies  # no registration challenge was issued
    r = api_post(owner.http, "/api/security/passkeys/options", owner.csrf,
                 {"password": OWNER_PASSWORD})  # fmt: skip
    assert r.status_code == 200 and "publicKey" in r.json()
    _challenge(owner)  # or with a passkey the person already has
    r = api_post(owner.http, "/api/security/passkeys/options", owner.csrf,
                 {"passkey": _cred("owner")})  # fmt: skip
    assert r.status_code == 200


def test_a_demoted_reader_can_still_narrow_a_legacy_key(app, home):
    from lectern.models import Role, User

    mia = home["mia"]
    key_id = _key_row(mia.key("upload", "file", "read", "organize")).id
    with session_factory()() as db:
        db.scalar(select(User).where(User.username == "mia")).role = Role.READER
        db.commit()
    # Narrowing never needs to be allowed: it keeps upload/file but removes organize.
    r = _edit(mia, key_id, ["upload", "file", "read"])
    assert r.status_code == 200, r.text
    assert r.json()["key"]["scopes"] == ["upload", "file", "read"]
    # Adding back organize is fine for a reader (with the step-up) ...
    r = _edit(mia, key_id, ["upload", "file", "read", "organize"], password=mia.password)
    assert r.status_code == 200, r.text
    # ... but adding upload or file is not, whatever else the edit does.
    r = _edit(mia, key_id, ["read"])
    assert r.status_code == 200
    for scopes in (["read", "upload"], ["file"]):
        r = _edit(mia, key_id, scopes, password=mia.password)
        assert r.status_code == 403 and r.json()["detail"] == "readers_cannot_upload"
    assert _key_row(_key_row_prefix(key_id)).scopes == ["read"]


def _key_row_prefix(key_id: int) -> str:
    with session_factory()() as db:
        return db.get(AIKey, key_id).prefix


def test_a_scope_edit_during_a_write_is_busy_not_an_error(app, home, settings):
    """While another writer (an AI tool filing a book) holds SQLite's write lock past the busy
    timeout, an edit answers 503 busy with Retry-After, changes nothing, and works on retry."""
    import sqlite3

    owner = home["owner"]
    secret = owner.key("upload", "file", "read")
    key_id = _key_row(secret).id
    writer = sqlite3.connect(settings.db_path, timeout=0)
    writer.execute("BEGIN IMMEDIATE")
    try:
        r = _edit(owner, key_id, ["read"])
        assert r.status_code == 503 and r.json()["detail"] == "busy", r.text
        assert r.headers["retry-after"] == "2"
    finally:
        writer.rollback()
        writer.close()
    assert _key_row(secret).scopes == ["upload", "file", "read"]
    assert _edit(owner, key_id, ["read"]).status_code == 200
    assert _key_row(secret).scopes == ["read"]


def test_a_key_files_only_imports_it_uploaded(app, home, settings):
    """Review finding: a file-only key could confirm any of the person's pending imports."""
    owner = home["owner"]
    uploader = MCP(app, owner.key("upload", label="uploader"))
    filer = MCP(app, owner.key("file", label="filer"))
    data = make_epub(settings.data_dir / "K.epub", title="Kilo").read_bytes()
    pending = uploader.ok("upload_book", filename="K.epub",
                          content_base64=base64.b64encode(data).decode())  # fmt: skip
    ok, err = filer.call("confirm_import", import_id=pending["import_id"])
    assert not ok and "not uploaded by this AI key" in err
    # Nor one the person started in the web app.
    r = api_post(owner.http, "/api/imports", owner.csrf, {"label": "web"})
    assert r.status_code == 201, r.text
    ok, err = filer.call("confirm_import", import_id=r.json()["id"])
    assert not ok and "not uploaded by this AI key" in err
    with session_factory()() as db:
        assert db.scalar(select(Work).where(Work.title == "Kilo")) is None
    # The uploading key files its own once it has file.
    _set_scopes(_secret_of(uploader), ["upload", "file"])
    assert uploader.ok("confirm_import", import_id=pending["import_id"])["created"]


def _secret_of(mcp: MCP) -> str:
    return mcp.key


def _chunked(mcp: MCP, data: bytes, on_last_chunk, monkeypatch) -> tuple[bool, object]:
    """Upload ``data`` in 4 KiB chunks; ``on_last_chunk`` runs after the last chunk is stored
    and before the upload is finalized (the window an in-flight upload must re-check)."""
    from lectern.routes.api import uploads as uploads_api

    monkeypatch.setattr(mcp_server, "CHUNK_BYTES", 4096)
    store = uploads_api._store_chunk

    def store_then(db, auth, settings, upload_id, offset, sha, chunk):
        out = store(db, auth, settings, upload_id, offset, sha, chunk)
        if offset + len(chunk) == len(data):
            on_last_chunk()
        return out

    monkeypatch.setattr(uploads_api, "_store_chunk", store_then)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    first = mcp.ok("upload_book", filename="Late.epub", content_base64=b64(data[:4096]),
                   total_size=len(data), sha256=hashlib.sha256(data).hexdigest())  # fmt: skip
    offset, result = 4096, None
    while offset < len(data):
        result = mcp.call("upload_book", filename="Late.epub", upload_id=first["upload_id"],
                          offset=offset, content_base64=b64(data[offset : offset + 4096]))  # fmt: skip
        offset += 4096
    return result


def _set_scopes(secret: str, scopes: list[str]) -> None:
    with session_factory()() as db:  # what POST /api/ai-keys/{id}/scopes commits
        db.scalar(select(AIKey).where(AIKey.prefix == secret[:12])).scopes = scopes
        db.commit()


def test_an_in_flight_upload_rechecks_upload_at_finalization(app, home, settings, monkeypatch):
    from lectern.models import UploadSession, UploadStatus

    secret = home["owner"].key("upload", "file")
    data = make_epub(settings.data_dir / "Late.epub", title="Late", padding=20000).read_bytes()
    ok, err = _chunked(MCP(app, secret), data, lambda: _set_scopes(secret, ["file"]),
                       monkeypatch)  # fmt: skip
    assert not ok and "forbidden: needs upload scope" in err
    with session_factory()() as db:
        row = db.scalar(select(UploadSession))
        assert row.status == UploadStatus.ACTIVE  # never finalized into the import
    assert [i.status.value for i in open_imports()] == ["uploading"]


@pytest.mark.parametrize("keep_file", [True, False])
def test_an_in_flight_upload_rechecks_file_before_auto_filing(
    app, home, settings, monkeypatch, keep_file
):
    owner = home["owner"]
    assert api_post(owner.http, "/api/me/preferences", owner.csrf,
                    {"auto_file_imports": True}).status_code == 200  # fmt: skip
    secret = owner.key("upload", "file")
    # A certain join: the existing book "Alpha" by "Author Alpha".
    data = make_epub(settings.data_dir / "Late.epub", title="Alpha", authors=["Author Alpha"],
                     padding=20000).read_bytes()  # fmt: skip
    after = ["upload", "file"] if keep_file else ["upload"]
    out = _chunked(MCP(app, secret), data, lambda: _set_scopes(secret, after), monkeypatch)
    ok, result = out
    assert ok, result
    if keep_file:
        assert result["status"] == "filed" and result["auto_filed"]
    else:
        assert result["status"] == "ready" and not result["auto_filed"]
        assert result["proposal"]["action"] == "join"  # waits for the person's review


def test_file_is_rechecked_right_before_auto_filing(app, home, settings, monkeypatch):
    """Review finding: ``file`` was read before detection, so removing it while the upload
    was being analysed still auto-filed the book."""
    owner = home["owner"]
    assert api_post(owner.http, "/api/me/preferences", owner.csrf,
                    {"auto_file_imports": True}).status_code == 200  # fmt: skip
    secret = owner.key("upload", "file")
    eligible = imports.auto_file_eligible

    def after_detection(db, user, imp):  # detection is committed; filing comes next
        _set_scopes(secret, ["upload"])
        return eligible(db, user, imp)

    monkeypatch.setattr(imports, "auto_file_eligible", after_detection)
    data = make_epub(settings.data_dir / "A.epub", title="Alpha", authors=["Author Alpha"],
                     padding=777).read_bytes()  # fmt: skip
    out = MCP(app, secret).ok("upload_book", filename="A.epub",
                              content_base64=base64.b64encode(data).decode())  # fmt: skip
    assert out["status"] == "ready" and not out["auto_filed"]
    assert out["proposal"]["action"] == "join"
    assert not audit_rows("import.auto_file")


def test_migration_0012_file_scope_upgrade_and_narrowing_downgrade(tmp_path):
    import sqlalchemy as sa
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from lectern.db import make_engine
    from lectern.migrate import alembic_config, upgrade_to_head
    from lectern.models import Base

    engine = make_engine(f"sqlite:///{tmp_path / 'm12.db'}")
    cfg = alembic_config(str(engine.url))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0011")

    def insert(keys: dict) -> None:
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            for label, scopes in keys.items():
                conn.execute(
                    sa.text(
                        "INSERT INTO ai_keys (user_id, label, key_hash, prefix, scopes,"
                        " created_at) VALUES (1, :label, :hash, 'lak_x', :scopes, '2026-10-20')"
                    ),
                    {"label": label, "hash": hashlib.sha256(label.encode()).hexdigest(),
                     "scopes": json.dumps(scopes)},
                )  # fmt: skip

    def state() -> dict:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT label, scopes, key_hash, revoked_at FROM ai_keys")
            ).all()
        for r in rows:
            assert r[2] == hashlib.sha256(r[0].encode()).hexdigest()  # never rotated
        return {r[0]: (json.loads(r[1]), r[3] is not None) for r in rows}

    insert({"uploader": ["upload"], "everything": ["upload", "read", "organize"],
            "reader": ["read"], "organizer": ["read", "organize"]})  # fmt: skip
    upgrade_to_head(engine)
    assert state() == {
        "uploader": (["upload", "file"], False),
        "everything": (["upload", "file", "read", "organize"], False),
        "reader": (["read"], False),
        "organizer": (["read", "organize"], False),
    }
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
    # Keys edited after 0012: filing turned off, or file without upload.
    insert({"no-filing": ["upload", "read"], "upload-only": ["upload"],
            "file-only": ["file"], "file-read": ["file", "read"]})  # fmt: skip
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0011")
    # Before 0012 upload meant filing too: a downgrade drops it rather than re-enabling
    # filing, drops file, and revokes a key left with nothing.
    assert state() == {
        "uploader": (["upload"], False),
        "everything": (["upload", "read", "organize"], False),
        "reader": (["read"], False),
        "organizer": (["read", "organize"], False),
        "no-filing": (["read"], False),
        "upload-only": ([], True),
        "file-only": ([], True),
        "file-read": (["read"], False),
    }
    assert "ai_key_id" not in {c["name"] for c in sa.inspect(engine).get_columns("imports")}


def _tree(root: Path) -> dict[str, str]:
    """Every file and folder under ``root``, with each file's SHA-256."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "/"
        for p in sorted(root.rglob("*"))
    }


@pytest.mark.parametrize("title", ["Zulu Unique", "Alpha"])
def test_a_refused_filing_leaves_the_library_as_it_was(app, home, settings, monkeypatch, title):
    """Review finding: when the file scope was removed during filing, the rows and files were
    rolled back but book.json was not: a stale sidecar on the joined book, or an orphan folder
    holding only book.json for a new one."""
    secret = home["owner"].key("upload", "file")
    mcp = MCP(app, secret)
    data = make_epub(settings.data_dir / "Z.epub", title=title, authors=[f"Author {title}"],
                     padding=4242).read_bytes()  # fmt: skip
    out = mcp.ok("upload_book", filename="Z.epub", content_base64=base64.b64encode(data).decode())
    assert out["status"] == "ready"
    assert out["proposal"]["action"] == ("join" if title == "Alpha" else "new")
    before = _tree(settings.library_dir)
    real = imports.file

    def scope_removed_meanwhile(*args, **kw):
        _set_scopes(secret, ["upload"])  # the owner unticks file while this call is filing
        return real(*args, **kw)

    monkeypatch.setattr(imports, "file", scope_removed_meanwhile)
    ok, err = mcp.call("confirm_import", import_id=out["import_id"])
    assert not ok and "needs file scope" in err
    assert _tree(settings.library_dir) == before
    with session_factory()() as db:
        assert db.scalar(select(Work).where(Work.title == "Zulu Unique")) is None
        assert db.scalar(select(Import)).status.value == "ready"


def test_a_failed_step_up_while_the_database_is_locked_is_busy(app, home, settings):
    import sqlite3

    owner = home["owner"]
    key_id = _key_row(owner.key("read")).id
    writer = sqlite3.connect(settings.db_path, timeout=0)
    writer.execute("BEGIN IMMEDIATE")
    try:
        r = _edit(owner, key_id, ["read", "organize"], password="wrong password, sorry")
        assert r.status_code == 503 and r.json()["detail"] == "busy", r.text
        r = api_post(owner.http, "/api/security/passkeys/options", owner.csrf,
                     {"password": "wrong password, sorry"})  # fmt: skip
        assert r.status_code == 503 and r.json()["detail"] == "busy", r.text
    finally:
        writer.rollback()
        writer.close()
    r = _edit(owner, key_id, ["read", "organize"], password="wrong password, sorry")
    assert r.json()["detail"] == "step_up_failed"


def test_a_key_adds_files_only_to_imports_it_opened(app, home, settings, monkeypatch):
    """Review finding: an upload-only key could append files to an import a filing key
    opened, which the filing key would then file."""
    monkeypatch.setattr(mcp_server, "CHUNK_BYTES", 4096)
    owner = home["owner"]
    filer = MCP(app, owner.key("upload", "file", label="filer"))
    other = MCP(app, owner.key("upload", label="other"))
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    small = make_epub(settings.data_dir / "M.epub", title="Mike").read_bytes()
    theirs = filer.ok("upload_book", filename="M.epub", content_base64=b64(small), finish=False)
    big = make_epub(settings.data_dir / "N.epub", title="Nov", padding=20000).read_bytes()
    started = filer.ok("upload_book", filename="N.epub", content_base64=b64(big[:4096]),
                       total_size=len(big), sha256=hashlib.sha256(big).hexdigest(),
                       import_id=theirs["import_id"])  # fmt: skip
    web = api_post(owner.http, "/api/imports", owner.csrf, {"label": "web"}).json()["id"]
    for import_id in (theirs["import_id"], web):
        ok, err = other.call("upload_book", filename="X.epub", content_base64=b64(small),
                             import_id=import_id)  # fmt: skip
        assert not ok and "not opened by this AI key" in err
    # Nor by continuing the filing key's upload session.
    ok, err = other.call("upload_book", filename="N.epub", upload_id=started["upload_id"],
                         offset=4096, content_base64=b64(big[4096:8192]))  # fmt: skip
    assert not ok and "not opened by this AI key" in err
    with session_factory()() as db:
        imp = db.scalar(select(Import).where(Import.public_id == theirs["import_id"]))
        assert [f.filename for f in imp.files] == ["M.epub"]
    # The key that opened it can still add to it.
    assert (
        filer.ok("upload_book", filename="N.epub", upload_id=started["upload_id"])["received_bytes"]
        == 4096
    )
