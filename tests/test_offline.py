"""Offline downloads for the installed web app (docs/offline.md): what a download holds, the
reconnect check, positions replayed from the offline queue, and the public PWA files."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from tests.conftest import (
    ORIGIN,
    OWNER_PASSWORD,
    api_post,
    drain_inbox,
    login,
    make_epub,
    make_user,
    put_json,
)
from tests.test_readaloud import seed_magi

from lectern.db import session_factory
from lectern.jobs import variant_path
from lectern.models import AudioTrack, AuditLog, DownloadEvent, Work
from lectern.routes.spa import APP_DIR


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(UTC)


def _add_epub(settings, title: str) -> int:
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    make_epub(settings.inbox_path / f"{title}.epub", title=title)
    drain_inbox(settings)
    with session_factory()() as db:
        return db.scalar(select(Work.id).where(Work.title == title))


def test_manifest_lists_the_files_audio_qualities_and_read_along(client, owner, settings, tmp_path):
    work_id = seed_magi(settings, tmp_path)
    m = client.get(f"/api/works/{work_id}/offline").json()
    assert [f["kind"] for f in m["files"]] == ["epub"]
    assert m["files"][0]["url"] == f"/library/works/{work_id}/book.epub?offline=1"
    track = m["audio"]["tracks"][0]
    assert track["full"]["url"] == f"/library/tracks/{track['id']}/stream"
    assert track["low"] is None and m["audio"]["low_bytes"] is None
    assert m["audio"]["full_bytes"] == track["full"]["size"] > 0
    assert m["readalong"] is True

    # A data-saver copy appears in the manifest with its own size; the revision is unchanged
    # (the copy plays the same audio).
    with session_factory()() as db:
        work = db.get(Work, work_id)
        t = db.scalar(select(AudioTrack).where(AudioTrack.work_id == work_id))
        low = variant_path(settings.library_dir, work, t.sha256, "aac")
    low.parent.mkdir(parents=True, exist_ok=True)
    low.write_bytes(b"\0" * 1234)
    m2 = client.get(f"/api/works/{work_id}/offline").json()
    assert m2["audio"]["tracks"][0]["low"] == {
        "url": f"/library/tracks/{track['id']}/stream?variant=low",
        "size": 1234,
        "media_type": "audio/mp4",
    }
    assert m2["audio"]["low_bytes"] == 1234
    assert m2["revision"] == m["revision"]


def test_check_reports_ok_changed_and_gone(client, owner, settings, tmp_path):
    magi = seed_magi(settings, tmp_path)
    other = _add_epub(settings, "Another Book")
    revs = {w: client.get(f"/api/works/{w}/offline").json()["revision"] for w in (magi, other)}
    check = lambda: {  # noqa: E731
        r["id"]: r["status"]
        for r in api_post(
            client,
            "/api/offline/check",
            owner["csrf"],
            {"works": [{"id": w, "revision": revs[w]} for w in (magi, other)] + [{"id": 999}]},
        ).json()["works"]
    }
    assert check() == {magi: "ok", other: "ok", 999: "gone"}

    # Anything a download holds changing (here: the sentence list and audio) is "changed".
    with session_factory()() as db:
        db.query(AudioTrack).filter(AudioTrack.work_id == magi).delete()
        db.commit()
    assert check()[magi] == "changed"

    # Trashed: gone, so the device deletes its copy.
    r = api_post(client, f"/api/works/{other}/trash", owner["csrf"])
    assert r.status_code == 200, r.text
    assert check()[other] == "gone"


def test_a_reader_without_the_shelf_gets_404_and_gone(client, owner, settings, tmp_path):
    magi = seed_magi(settings, tmp_path)
    rita = make_user(client, owner["csrf"], "rita", "reader")
    shelf = api_post(client, "/api/shelves", owner["csrf"], {"name": "Rita's"}).json()
    api_post(client, f"/api/shelves/{shelf['id']}/items", owner["csrf"], {"work_id": magi})
    r = put_json(
        client,
        f"/api/shelves/{shelf['id']}/grants/{rita['id']}",
        owner["csrf"],
        {"permission": "view"},
    )
    assert r.status_code == 200, r.text
    rev = client.get(f"/api/works/{magi}/offline").json()["revision"]

    client.cookies.clear()
    csrf = login(client, "rita", "rita-correct horse")
    assert client.get(f"/api/works/{magi}/offline").status_code == 200
    body = {"works": [{"id": magi, "revision": rev}]}
    assert api_post(client, "/api/offline/check", csrf, body).json()["works"][0]["status"] == "ok"

    # The owner takes the book off Rita's shelf: her device must delete it.
    client.cookies.clear()
    owner_csrf = login(client, "owner", OWNER_PASSWORD)
    r = client.delete(
        f"/api/shelves/{shelf['id']}/items/{magi}",
        headers={"origin": ORIGIN, "x-csrf-token": owner_csrf},
    )
    assert r.status_code == 204
    client.cookies.clear()
    csrf = login(client, "rita", "rita-correct horse")
    assert client.get(f"/api/works/{magi}/offline").status_code == 404
    assert api_post(client, "/api/offline/check", csrf, body).json()["works"][0]["status"] == "gone"
    assert client.get(f"/library/works/{magi}/book.epub?offline=1").status_code == 404


def test_offline_copies_are_counted_audited_and_capped(client, owner, settings, tmp_path):
    magi = seed_magi(settings, tmp_path)
    second = _add_epub(settings, "Second Book")
    r = api_post(client, f"/api/works/{magi}/offline", owner["csrf"], {"parts": ["epub"]})
    assert r.status_code == 200 and r.json()["work_id"] == magi
    settings.download_cap_per_hour = 1
    r = client.get(f"/library/works/{magi}/book.epub?offline=1")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    with session_factory()() as db:
        actions = [
            (a.action, a.detail)
            for a in db.scalars(select(AuditLog).where(AuditLog.action.like("download%")))
        ]
        assert ("download.offline", "epub") in actions
        assert ("download", "offline") in actions
        assert db.scalar(select(DownloadEvent).where(~DownloadEvent.blocked)) is not None
    # Over the hourly cap: refused like any other download, with the reason for the app.
    r = client.get(f"/library/works/{second}/book.epub?offline=1")
    assert r.status_code == 429 and r.json()["detail"] == "download_cap"


@pytest.fixture
def online_position(client, owner, settings, tmp_path):
    """A work with a position saved online by another device; returns (work id, its time)."""
    work_id = seed_magi(settings, tmp_path)
    r = put_json(
        client,
        f"/api/works/{work_id}/progress",
        owner["csrf"],
        {"source": "audio", "device": "Kobo", "audio_s": 30.0},
    )
    assert r.status_code == 200, r.text
    at = datetime.fromisoformat(r.json()["received_at"].replace("Z", "+00:00"))
    return work_id, at


def _replay(client, csrf, work_id, *, recorded, sent=None, base=None, keep=False, expect=None, n=5):
    sent = sent or _now()
    body = {
        "source": "web",
        "device": "Safari on iPhone",
        "sentence_n": n,
        "offline": {
            "recorded_at": _iso(recorded),
            "sent_at": _iso(sent),
            "base": _iso(base) if base else None,
            "keep": keep,
            "expect": expect,
        },
    }
    return put_json(client, f"/api/works/{work_id}/progress", csrf, body)


def test_replay_recorded_before_another_devices_save_is_a_conflict(client, owner, online_position):
    work_id, at = online_position
    r = _replay(client, owner["csrf"], work_id, recorded=at - timedelta(minutes=10))
    assert r.status_code == 409
    body = r.json()
    assert body["detail"] == "progress_conflict"
    assert body["server"]["device"] == "Kobo" and body["server"]["source"] == "audio"
    # The server's position is untouched.
    assert client.get(f"/api/works/{work_id}/progress").json()["device"] == "Kobo"
    # The person chose this device's position over the one the prompt showed.
    shown = body["server"]["received_at"]
    r = _replay(
        client,
        owner["csrf"],
        work_id,
        recorded=at - timedelta(minutes=10),
        keep=True,
        expect=shown,
    )
    assert r.status_code == 200
    assert r.json()["device"] == "Safari on iPhone" and r.json()["position"]["sentence_n"] == 5


def test_keep_never_overwrites_a_save_made_after_the_prompt(client, owner, online_position):
    """Compare-and-set: "keep this device's" only replaces the position the prompt showed."""
    work_id, at = online_position
    recorded = at - timedelta(minutes=10)
    r = _replay(client, owner["csrf"], work_id, recorded=recorded)
    assert r.status_code == 409
    shown = r.json()["server"]["received_at"]
    # While the prompt is open, the Kobo reads on.
    later = put_json(
        client,
        f"/api/works/{work_id}/progress",
        owner["csrf"],
        {"source": "audio", "device": "Kobo", "audio_s": 60.0},
    )
    assert later.status_code == 200
    r = _replay(client, owner["csrf"], work_id, recorded=recorded, keep=True, expect=shown)
    assert r.status_code == 409, r.text
    assert r.json()["server"]["received_at"] == later.json()["received_at"] != shown
    assert client.get(f"/api/works/{work_id}/progress").json()["audio_s"] == 60.0
    # keep without saying what it replaces is refused too; asked again, it goes through.
    r = _replay(client, owner["csrf"], work_id, recorded=recorded, keep=True)
    assert r.status_code == 409
    r = _replay(
        client,
        owner["csrf"],
        work_id,
        recorded=recorded,
        keep=True,
        expect=later.json()["received_at"],
    )
    assert r.status_code == 200 and r.json()["device"] == "Safari on iPhone"


def test_replay_recorded_after_wins_like_an_online_write(client, owner, online_position):
    work_id, at = online_position
    r = _replay(client, owner["csrf"], work_id, recorded=_now())
    assert r.status_code == 200 and r.json()["device"] == "Safari on iPhone"


def test_replay_wins_when_this_device_had_seen_the_servers_position(client, owner, online_position):
    work_id, at = online_position
    # Recorded "before" on a slow clock, but the device had already seen the Kobo's save.
    r = _replay(client, owner["csrf"], work_id, recorded=at - timedelta(minutes=5), base=at)
    assert r.status_code == 200 and r.json()["device"] == "Safari on iPhone"


def test_replay_corrects_the_device_clock(client, owner, online_position):
    work_id, at = online_position
    now = _now()
    # Device clock one hour slow: it recorded 2 s after the Kobo's save (real time).
    slow = timedelta(hours=1)
    r = _replay(
        client, owner["csrf"], work_id, recorded=at + timedelta(seconds=2) - slow, sent=now - slow
    )
    assert r.status_code == 200, r.text
    # Device clock one hour fast: it recorded 20 s *before* the Kobo's save, which a naive
    # comparison would call later.
    _put = put_json(
        client,
        f"/api/works/{work_id}/progress",
        owner["csrf"],
        {"source": "audio", "device": "Kobo", "audio_s": 40.0},
    )
    at2 = datetime.fromisoformat(_put.json()["received_at"].replace("Z", "+00:00"))
    now = _now()
    r = _replay(
        client,
        owner["csrf"],
        work_id,
        recorded=at2 - timedelta(seconds=20) + slow,
        sent=now + slow,
    )
    assert r.status_code == 409
    recorded = datetime.fromisoformat(r.json()["recorded_at"].replace("Z", "+00:00"))
    assert abs((recorded - (at2 - timedelta(seconds=20))).total_seconds()) < 5


def test_replay_needs_aware_times(client, owner, online_position):
    work_id, _ = online_position
    body = {
        "source": "web",
        "sentence_n": 1,
        "offline": {"recorded_at": "2026-10-21T10:00:00", "sent_at": "2026-10-21T10:00:00"},
    }
    assert (
        put_json(client, f"/api/works/{work_id}/progress", owner["csrf"], body).status_code == 422
    )


def test_page_replay_conflict(client, owner, settings):
    from tests.test_pdf_comics import make_pdf  # noqa: PLC0415

    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    make_pdf(settings.inbox_path / "notes.pdf", pages=8, title="Notes")
    drain_inbox(settings)
    with session_factory()() as db:
        from lectern.models import BookFile

        file_id = db.scalar(select(BookFile.id).where(BookFile.media_type == "application/pdf"))
    url = f"/api/files/{file_id}/progress"
    r = put_json(client, url, owner["csrf"], {"page": 2, "pages": 8, "device": "Kobo"})
    assert r.status_code == 200, r.text
    at = datetime.fromisoformat(r.json()["updated_at"].replace("Z", "+00:00"))
    old = {"recorded_at": _iso(at - timedelta(minutes=3)), "sent_at": _iso(_now())}
    r = put_json(client, url, owner["csrf"], {"page": 6, "pages": 8, "offline": old})
    assert r.status_code == 409 and r.json()["server"]["page"] == 2
    shown = r.json()["server"]["updated_at"]
    # Another save lands while the prompt is open: keeping against the old one is a new 409.
    newer = put_json(client, url, owner["csrf"], {"page": 3, "pages": 8, "device": "Kobo"})
    keep = {**old, "keep": True, "expect": shown}
    r = put_json(client, url, owner["csrf"], {"page": 6, "pages": 8, "offline": keep})
    assert r.status_code == 409 and r.json()["server"]["page"] == 3
    keep["expect"] = newer.json()["updated_at"]
    r = put_json(client, url, owner["csrf"], {"page": 6, "pages": 8, "offline": keep})
    assert r.status_code == 200 and r.json()["page"] == 6


needs_build = pytest.mark.skipif(
    not (APP_DIR / "sw.js").is_file(), reason="web app not built (npm --prefix frontend run build)"
)


@needs_build
def test_pwa_files_are_public_and_the_app_is_not(client):
    sw = client.get("/app/sw.js")
    assert sw.status_code == 200
    assert sw.headers["content-type"].startswith("application/javascript")
    assert sw.headers["cache-control"] == "no-cache"
    assert sw.headers["service-worker-allowed"] == "/app"
    man = client.get("/app/manifest.webmanifest")
    assert man.headers["content-type"].startswith("application/manifest+json")
    assert man.json()["scope"] == "/" and man.json()["start_url"] == "/app"
    shell = client.get("/app/shell.html")
    assert shell.status_code == 200 and '<div id="root">' in shell.text
    assert client.get("/app/pwa/icon-512.png").headers["content-type"] == "image/png"
    # Only image files in pwa/ (a traversal attempt falls through to the signed-in app).
    for bad in ("/app/pwa/..%2Fsw.js", "/app/pwa/missing.png", "/app/pwa/index.html"):
        assert client.get(bad).status_code in (401, 404), bad
    # The app itself and every API still need the session.
    assert client.get("/app/downloads", follow_redirects=False).status_code in (303, 401)
    assert client.get("/api/me").status_code == 401


def test_sign_out_clears_site_data_and_book_frames_are_allowed(client, owner):
    csp = client.get("/api/me").headers["content-security-policy"]
    assert "frame-ancestors 'self'" in csp
    r = client.post(
        "/logout",
        data={"csrf_token": owner["csrf"]},
        headers={"origin": ORIGIN},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["clear-site-data"] == '"cache", "storage"'


def test_signing_in_ends_the_session_already_on_this_browser(client, owner):
    """A sign-out made offline never reached the server; the next person signing in on that
    phone must not leave the previous session usable (docs/offline.md#signing-out)."""
    from lectern.security.sessions import COOKIE_NAME  # noqa: PLC0415

    reader = make_user(client, owner["csrf"], "guest", "reader")
    previous = client.cookies.get(COOKIE_NAME)
    login(client, "guest", reader["password"])
    assert client.cookies.get(COOKIE_NAME) != previous
    assert client.get("/api/me").json()["user"]["username"] == "guest"
    client.cookies.set(COOKIE_NAME, previous)
    assert client.get("/api/me").status_code == 401


def test_me_bounds_how_long_the_app_works_offline(client, owner, settings):
    me = client.get("/api/me").json()
    now = datetime.fromisoformat(me["offline"]["now"].replace("Z", "+00:00"))
    until = datetime.fromisoformat(me["offline"]["until"].replace("Z", "+00:00"))
    assert abs((now - _now()).total_seconds()) < 60
    assert until - now == timedelta(seconds=settings.offline_max_age_seconds)
    # Never past the session itself.
    settings.offline_max_age_seconds = settings.session_ttl_seconds * 2
    me = client.get("/api/me").json()
    until = datetime.fromisoformat(me["offline"]["until"].replace("Z", "+00:00"))
    assert until - now <= timedelta(seconds=settings.session_ttl_seconds)


def _app_sign_out(client, csrf: str):
    return client.post(
        "/logout",
        data={"csrf_token": csrf},
        headers={"origin": ORIGIN, "x-lectern-app": "signout"},
        follow_redirects=False,
    )


def test_app_sign_out_confirms_only_an_ended_session(client, owner):
    """The installed app clears its pending sign-out only on 200 {"signed_out": true}
    (docs/offline.md#signing-out); every other outcome leaves the session as it was."""
    from lectern.security.sessions import COOKIE_NAME  # noqa: PLC0415

    cookie = client.cookies.get(COOKIE_NAME)
    # A token naming no session, or none, while a session is live on this browser: the server
    # cannot say it ended, so it does not confirm (and touches nothing).
    for token in ("not-this-sessions-token", ""):
        r = _app_sign_out(client, token)
        assert r.status_code == 409 and r.json()["detail"] == "signout_token_missing"
        assert client.get("/api/me").status_code == 200
    # Cross-site: refused, not confirmed.
    r = client.post(
        "/logout",
        data={"csrf_token": owner["csrf"]},
        headers={"origin": "https://evil.example", "x-lectern-app": "signout"},
        follow_redirects=False,
    )
    assert r.status_code == 403 and client.get("/api/me").status_code == 200
    # The real token: ended, confirmed, cookie cleared, no Clear-Site-Data.
    r = _app_sign_out(client, owner["csrf"])
    assert r.status_code == 200 and r.json() == {"signed_out": True}
    assert "clear-site-data" not in r.headers
    client.cookies.set(COOKIE_NAME, cookie)
    assert client.get("/api/me").status_code == 401
    # Retried after it already worked (the answer was lost): still confirmed.
    assert _app_sign_out(client, owner["csrf"]).json() == {"signed_out": True}


def test_app_sign_out_ends_the_named_session_not_a_newer_one(client, owner):
    """By the time the retry gets through, someone else may have signed in on the phone: the
    session the app signed out of ends, the newer one stays."""
    from lectern.security.sessions import COOKIE_NAME  # noqa: PLC0415

    old_cookie, old_csrf = client.cookies.get(COOKIE_NAME), owner["csrf"]
    guest = make_user(client, owner["csrf"], "guest", "reader")
    # The old session is still live elsewhere (another browser holding its cookie).
    from fastapi.testclient import TestClient  # noqa: PLC0415

    other = TestClient(client.app)
    other.cookies.set(COOKIE_NAME, old_cookie)
    client.cookies.clear()
    login(client, "guest", guest["password"])
    r = _app_sign_out(client, old_csrf)
    assert r.status_code == 200 and r.json() == {"signed_out": True}
    assert other.get("/api/me").status_code == 401
    assert client.get("/api/me").json()["user"]["username"] == "guest"


def test_app_sign_out_without_a_live_session_here_is_confirmed(client, owner):
    from lectern.security.sessions import COOKIE_NAME  # noqa: PLC0415

    client.cookies.delete(COOKIE_NAME)
    r = _app_sign_out(client, "")
    assert r.status_code == 200 and r.json() == {"signed_out": True}
