"""Web reader settings (issue #79): a synced default per person and "Just this book" overrides,
private to each person, for works they can see."""

from __future__ import annotations

from fastapi.testclient import TestClient
from tests.conftest import (
    OWNER_PASSWORD,
    api_delete,
    drain_inbox,
    login,
    make_epub,
    make_user,
    put_json,
)


def _books(client, settings) -> dict[str, int]:
    settings.inbox_path.mkdir(parents=True, exist_ok=True)
    for t in ("Dense", "Airy"):
        make_epub(settings.inbox_path / f"{t}.epub", title=t)
    drain_inbox(settings)
    return {w["title"]: w["id"] for w in client.get("/api/library").json()["works"]}


def test_just_this_book_changes_one_book_and_follows_the_person_to_another_browser(
    app, client, owner, settings
):
    ids = _books(client, settings)
    csrf = owner["csrf"]
    dense, airy = ids["Dense"], ids["Airy"]
    assert client.get(f"/api/works/{dense}/reader-settings").json() == {
        "default": None,
        "book": None,
    }

    r = put_json(client, "/api/me/reader-settings", csrf,
                 {"settings": {"theme": "sepia", "size": 110, "font": "dyslexic"}})  # fmt: skip
    assert r.status_code == 200, r.text
    r = put_json(client, f"/api/works/{dense}/reader-settings", csrf,
                 {"settings": {"size": 85, "letterSpacing": 0.05, "heavier": True}})  # fmt: skip
    assert r.status_code == 200, r.text
    # Only what was set is stored: unset fields keep following the default.
    assert r.json()["book"] == {"size": 85, "letterSpacing": 0.05, "heavier": True}

    # Another browser, same person: both survive, and the other book has no override.
    with TestClient(app) as other:
        login(other, "owner", OWNER_PASSWORD)
        mine = other.get(f"/api/works/{dense}/reader-settings").json()
        assert mine == {
            "default": {"theme": "sepia", "size": 110, "font": "dyslexic"},
            "book": {"size": 85, "letterSpacing": 0.05, "heavier": True},
        }
        assert other.get(f"/api/works/{airy}/reader-settings").json()["book"] is None

    # A new default replaces the old one whole and leaves the override alone.
    put_json(client, "/api/me/reader-settings", csrf, {"settings": {"theme": "contrast"}})
    got = client.get(f"/api/works/{dense}/reader-settings").json()
    assert got["default"] == {"theme": "contrast"}
    assert got["book"]["size"] == 85

    # Resetting the book: it follows the default again.
    r = api_delete(client, f"/api/works/{dense}/reader-settings", csrf)
    assert r.status_code == 200 and r.json()["book"] is None
    assert client.get(f"/api/works/{dense}/reader-settings").json()["book"] is None


def test_settings_are_private_to_each_person_and_need_access_to_the_work(client, owner, settings):
    ids = _books(client, settings)
    dense = ids["Dense"]
    put_json(client, "/api/me/reader-settings", owner["csrf"], {"settings": {"theme": "dark"}})
    put_json(client, f"/api/works/{dense}/reader-settings", owner["csrf"],
             {"settings": {"size": 90}})  # fmt: skip
    mia = make_user(client, owner["csrf"], "mia", "member")
    rex = make_user(client, owner["csrf"], "rex", "reader")

    mia_csrf = login(client, "mia", mia["password"])
    assert client.get(f"/api/works/{dense}/reader-settings").json() == {
        "default": None,
        "book": None,
    }
    put_json(client, f"/api/works/{dense}/reader-settings", mia_csrf, {"settings": {"size": 150}})
    api_delete(client, f"/api/works/{dense}/reader-settings", mia_csrf)

    # The reader has no shelf with this work: as if it did not exist.
    rex_csrf = login(client, "rex", rex["password"])
    assert client.get(f"/api/works/{dense}/reader-settings").status_code == 404
    r = put_json(client, f"/api/works/{dense}/reader-settings", rex_csrf, {"settings": {}})
    assert r.status_code == 404
    assert api_delete(client, f"/api/works/{dense}/reader-settings", rex_csrf).status_code == 404

    # Mia's writes and delete never touched the owner's.
    login(client, "owner", OWNER_PASSWORD)
    assert client.get(f"/api/works/{dense}/reader-settings").json() == {
        "default": {"theme": "dark"},
        "book": {"size": 90},
    }


def test_only_known_settings_in_range_are_stored(client, owner):
    csrf = owner["csrf"]
    for bad in (
        {"theme": "neon"},
        {"size": 1000},
        {"letterSpacing": -0.1},
        {"font": "https://fonts.example/x.woff2"},
        {"colour": "red"},
    ):
        r = put_json(client, "/api/me/reader-settings", csrf, {"settings": bad})
        assert r.status_code == 422, bad
    assert client.get("/api/me").status_code == 200
    r = put_json(client, "/api/me/reader-settings", csrf, {"settings": {"font": "lexend"}})
    assert r.json() == {"default": {"font": "lexend"}}
    # Writes need the CSRF token like every other write.
    r = client.put("/api/me/reader-settings", json={"settings": {}})
    assert r.status_code == 403
